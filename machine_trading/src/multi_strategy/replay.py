from __future__ import annotations

import csv
import json
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


@dataclass
class PositionState:
    quantity: int = 0
    avg_price: float = 0.0
    realized_pnl: float = 0.0

    def apply_fill(self, side: str, quantity: int, price: float) -> tuple[float, int]:
        if quantity <= 0:
            return 0.0, 0
        if side == "BUY":
            total_cost = self.avg_price * self.quantity + price * quantity
            self.quantity += quantity
            self.avg_price = total_cost / self.quantity if self.quantity else 0.0
            return 0.0, 0
        close_qty = min(quantity, self.quantity)
        oversell_qty = max(0, quantity - self.quantity)
        pnl = (price - self.avg_price) * close_qty
        self.realized_pnl += pnl
        self.quantity -= quantity
        if self.quantity <= 0:
            self.quantity = 0
            self.avg_price = 0.0
        return pnl, oversell_qty

    def apply_cover(self, quantity: int, price: float) -> tuple[float, int]:
        """Apply a BUY-to-cover fill against a short position."""
        if quantity <= 0:
            return 0.0, 0
        close_qty = min(quantity, self.quantity)
        overshoot = max(0, quantity - self.quantity)
        pnl = (self.avg_price - price) * close_qty
        self.realized_pnl += pnl
        self.quantity -= quantity
        if self.quantity <= 0:
            self.quantity = 0
            self.avg_price = 0.0
        return pnl, overshoot


@dataclass
class ReplayState:
    positions: dict[tuple[str, str, str], PositionState] = field(default_factory=dict)
    order_counts: Counter[str] = field(default_factory=Counter)
    fill_counts: Counter[str] = field(default_factory=Counter)
    risk_rejects: Counter[str] = field(default_factory=Counter)
    strategy_skips: Counter[str] = field(default_factory=Counter)
    approved_decisions: Counter[str] = field(default_factory=Counter)
    warnings: list[str] = field(default_factory=list)


def replay_run(run_dir: str | Path) -> dict[str, Any]:
    run_path = Path(run_dir)
    if not run_path.exists():
        raise FileNotFoundError(f"run folder does not exist: {run_path}")

    state = ReplayState()
    _seed_account_positions(run_path / "events.jsonl", state)
    timeline = _timeline_rows(run_path, state)
    timeline.sort(key=lambda row: (row["timestamp"], row["source"], row.get("order_id", "")))

    _write_csv(run_path / "replay_timeline.csv", timeline)
    summary = _summary(run_path, state, timeline)
    (run_path / "replay_summary.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")
    return summary


def _timeline_rows(run_path: Path, state: ReplayState) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    orders_by_id: dict[str, list[dict[str, str]]] = defaultdict(list)
    for order in _read_csv(run_path / "orders.csv"):
        order_id = order.get("order_id", "")
        if order_id:
            state.order_counts[order_id] += 1
            orders_by_id[order_id].append(order)
        rows.append(_timeline_order(order))

    for order_id, count in state.order_counts.items():
        if count > 1:
            state.warnings.append(f"order_id {order_id} was submitted {count} times")

    for fill in _read_csv(run_path / "fills.csv"):
        order_id = fill.get("order_id", "")
        if order_id:
            state.fill_counts[order_id] += 1
        side = _fill_side(fill, orders_by_id.get(order_id, []))
        quantity = _int(fill.get("quantity"))
        price = _float(fill.get("price"))
        strategy = fill.get("strategy", "")
        symbol = fill.get("symbol", "")
        short_key = (strategy, symbol, "SHORT")
        long_key = (strategy, symbol, "LONG")
        if side == "BUY" and short_key in state.positions and state.positions[short_key].quantity > 0:
            position = state.positions[short_key]
            pnl, overshoot = position.apply_cover(quantity, price)
            if overshoot:
                state.warnings.append(f"buy fill exceeded short inventory: {strategy} {symbol} order={order_id} overshoot_qty={overshoot}")
            rows.append(_timeline_fill(fill, side, pnl, position))
        else:
            position = state.positions.setdefault(long_key, PositionState())
            pnl, oversell_qty = position.apply_fill(side, quantity, price)
            if oversell_qty:
                state.warnings.append(f"sell fill exceeded long inventory: {strategy} {symbol} order={order_id} oversell_qty={oversell_qty}")
            rows.append(_timeline_fill(fill, side, pnl, position))

    _read_events(run_path / "events.jsonl", state, rows)
    _read_decisions(run_path / "decisions.csv", state)
    _append_position_warnings(state)
    return rows


def _timeline_order(order: dict[str, str]) -> dict[str, Any]:
    return {
        "timestamp": _timestamp(order.get("timestamp")),
        "source": "orders",
        "event": "order",
        "strategy": order.get("strategy", ""),
        "symbol": order.get("symbol", ""),
        "order_id": order.get("order_id", ""),
        "role": order.get("role", ""),
        "side": order.get("action", ""),
        "quantity": order.get("quantity", ""),
        "price": order.get("limit_price") or order.get("stop_price") or "",
        "position_qty": "",
        "realized_pnl": "",
        "details": f"dry_run={order.get('dry_run', '')}",
    }


def _timeline_fill(fill: dict[str, str], side: str, pnl: float, position: PositionState) -> dict[str, Any]:
    return {
        "timestamp": _timestamp(fill.get("timestamp")),
        "source": "fills",
        "event": "fill",
        "strategy": fill.get("strategy", ""),
        "symbol": fill.get("symbol", ""),
        "order_id": fill.get("order_id", ""),
        "role": fill.get("role", ""),
        "side": side,
        "quantity": _int(fill.get("quantity")),
        "price": _float(fill.get("price")),
        "position_qty": position.quantity,
        "realized_pnl": round(pnl, 4),
        "details": "",
    }


def _read_events(path: Path, state: ReplayState, rows: list[dict[str, Any]]) -> None:
    if not path.exists():
        return
    with path.open(encoding="utf-8") as handle:
        for line in handle:
            if not line.strip():
                continue
            event = json.loads(line)
            event_type = str(event.get("type", ""))
            if event_type == "risk_reject":
                state.risk_rejects[str(event.get("reason", ""))] += 1
            elif event_type == "strategy_skip_locked":
                state.strategy_skips[str(event.get("strategy", ""))] += 1
            elif event_type == "depth_subscription_limit":
                l1_only = ", ".join(event.get("l1_only", []) or [])
                selected = ", ".join(event.get("selected", []) or [])
                state.warnings.append(f"L2 depth selected for {selected or 'none'}; L1 only for {l1_only or 'none'}")
            if event_type in {
                "risk_reject", "strategy_skip_locked", "depth_subscription_limit", "session_started",
                "forced_flatten_submitted", "stop_breach_flatten_submitted", "stale_entry_order_locks_expired",
            }:
                rows.append(
                    {
                        "timestamp": _timestamp(event.get("logged_at") or event.get("time")),
                        "source": "events",
                        "event": event_type,
                        "strategy": event.get("strategy", ""),
                        "symbol": event.get("symbol", ""),
                        "order_id": "",
                        "role": "",
                        "side": "",
                        "quantity": "",
                        "price": "",
                        "position_qty": "",
                        "realized_pnl": "",
                        "details": _event_details(event),
                    }
                )


def _read_decisions(path: Path, state: ReplayState) -> None:
    for row in _read_csv(path):
        strategy = row.get("strategy", "")
        # DictReader stores extra columns under a None key as a list; flatten before checking.
        flat: list[Any] = []
        for value in row.values():
            if isinstance(value, list):
                flat.extend(value)
            else:
                flat.append(value)
        if _is_true(row.get("approved")) or _is_true(row.get("passed")) or any(_is_true(v) for v in flat):
            state.approved_decisions[strategy] += 1


def _append_position_warnings(state: ReplayState) -> None:
    for (strategy, symbol, side), position in sorted(state.positions.items()):
        if position.quantity:
            label = "short" if side == "SHORT" else "long"
            state.warnings.append(f"open {label} exposure remains: {strategy} {symbol} qty={position.quantity} avg={position.avg_price:.4f}")


def _summary(run_path: Path, state: ReplayState, timeline: list[dict[str, Any]]) -> dict[str, Any]:
    positions = []
    total_realized = 0.0
    for (strategy, symbol, side), position in sorted(state.positions.items()):
        total_realized += position.realized_pnl
        positions.append(
            {
                "strategy": strategy,
                "symbol": symbol,
                "side": side,
                "quantity": position.quantity,
                "avg_price": round(position.avg_price, 4),
                "realized_pnl": round(position.realized_pnl, 4),
            }
        )
    return {
        "run_dir": str(run_path),
        "timeline_rows": len(timeline),
        "orders": sum(state.order_counts.values()),
        "fills": sum(state.fill_counts.values()),
        "duplicate_order_ids": {key: value for key, value in sorted(state.order_counts.items()) if value > 1},
        "approved_decisions": dict(sorted(state.approved_decisions.items())),
        "risk_rejects": dict(sorted(state.risk_rejects.items())),
        "strategy_skips": dict(sorted(state.strategy_skips.items())),
        "positions": positions,
        "total_realized_pnl": round(total_realized, 4),
        "warnings": state.warnings,
        "outputs": {
            "timeline": str(run_path / "replay_timeline.csv"),
            "summary": str(run_path / "replay_summary.json"),
        },
    }


def _seed_account_positions(path: Path, state: ReplayState) -> None:
    """Pre-populate account positions from account_positions_synced events so
    that fill P&L tracking is correct for positions opened before session start."""
    if not path.exists():
        return
    with path.open(encoding="utf-8") as handle:
        for line in handle:
            if not line.strip():
                continue
            event = json.loads(line)
            if event.get("type") != "account_positions_synced":
                continue
            for pos in event.get("positions", []):
                symbol = str(pos.get("symbol", ""))
                quantity = int(float(pos.get("quantity", 0) or 0))
                avg_price = float(pos.get("avg_price", 0.0) or 0.0)
                side_label = str(pos.get("side", "LONG"))
                if not symbol or quantity <= 0:
                    continue
                key = ("account", symbol, side_label)
                if key not in state.positions:
                    state.positions[key] = PositionState(quantity=quantity, avg_price=avg_price)


def _read_csv(path: Path) -> list[dict[str, str]]:
    if not path.exists() or path.stat().st_size == 0:
        return []
    with path.open(newline="", encoding="utf-8") as handle:
        return list(csv.DictReader(handle))


def _write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    headers = [
        "timestamp",
        "source",
        "event",
        "strategy",
        "symbol",
        "order_id",
        "role",
        "side",
        "quantity",
        "price",
        "position_qty",
        "realized_pnl",
        "details",
    ]
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=headers)
        writer.writeheader()
        writer.writerows(rows)


def _fill_side(fill: dict[str, str], orders: list[dict[str, str]]) -> str:
    action = next((order.get("action", "") for order in reversed(orders) if order.get("action")), "")
    if action:
        return action
    role = fill.get("role", "")
    return "BUY" if role == "entry" else "SELL"


def _event_details(event: dict[str, Any]) -> str:
    event_type = event.get("type", "")
    if event_type == "risk_reject":
        return f"reason={event.get('reason', '')}"
    if event_type == "strategy_skip_locked":
        return f"owner={event.get('owner', '')}"
    if event_type == "depth_subscription_limit":
        return f"selected={event.get('selected', [])}; l1_only={event.get('l1_only', [])}"
    if event_type == "session_started":
        return f"mode={event.get('mode', '')}; strategies={event.get('strategies', [])}; symbols={event.get('symbols', [])}"
    if event_type in {"forced_flatten_submitted", "stop_breach_flatten_submitted"}:
        return f"reason={event.get('reason', '')} qty={event.get('quantity', '')}"
    if event_type == "stale_entry_order_locks_expired":
        symbols = event.get("symbols", [])
        return f"symbols={','.join(symbols) if isinstance(symbols, list) else symbols}"
    return ""


def _timestamp(value: Any) -> str:
    if not value:
        return ""
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).astimezone(timezone.utc).isoformat()
    except ValueError:
        return str(value)


def _int(value: Any) -> int:
    try:
        return int(float(value or 0))
    except (TypeError, ValueError):
        return 0


def _float(value: Any) -> float:
    try:
        return float(value or 0.0)
    except (TypeError, ValueError):
        return 0.0


def _is_true(value: Any) -> bool:
    return str(value).strip().lower() in {"true", "1", "yes"}
