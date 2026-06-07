from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .broker import SharedBroker


@dataclass
class PositionRecord:
    """Persistent snapshot of one open position and its exit plan."""
    strategy: str
    symbol: str
    quantity: int
    avg_price: float
    initial_stop: float | None = None
    current_stop: float | None = None
    stop_order_id: str | None = None
    high_watermark: float | None = None


class PositionStateStore:
    """Writes broker position state to a single JSON file after every heartbeat
    and on shutdown.  On restart, load() returns the last-known positions so
    reconcile_on_startup() can cross-check them against live IBKR data.

    The file is stored at <log_root>/position_state.json, which is stable
    across runs (unlike per-run directories).  Writes are atomic: we write to
    a .tmp file first and then rename, so a mid-write crash leaves the previous
    state intact.
    """

    FILENAME = "position_state.json"

    def __init__(self, root: str | Path) -> None:
        self.path = Path(root) / self.FILENAME
        self._tmp = self.path.with_suffix(".tmp")
        Path(root).mkdir(parents=True, exist_ok=True)

    # ------------------------------------------------------------------
    # write
    # ------------------------------------------------------------------

    def save(self, broker: "SharedBroker") -> None:
        records: list[dict[str, Any]] = []
        for (strategy, symbol), qty in broker.long_positions.items():
            if qty <= 0:
                continue
            key = (strategy, symbol)
            records.append({
                "strategy": strategy,
                "symbol": symbol,
                "quantity": qty,
                "avg_price": broker.long_avg_prices.get(key, 0.0),
                "initial_stop": broker.initial_stop_prices.get(key),
                "current_stop": broker.current_stop_prices.get(key),
                "stop_order_id": broker.stop_orders_by_position.get(key),
                "high_watermark": broker.high_watermarks.get(key),
            })
        cooldowns = {
            sym: until.isoformat()
            for sym, until in broker.symbol_cooldowns.items()
        }
        data: dict[str, Any] = {
            "saved_at": datetime.now(timezone.utc).isoformat(),
            "positions": records,
            "cooldowns": cooldowns,
        }
        self._tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
        self._tmp.replace(self.path)

    # ------------------------------------------------------------------
    # read
    # ------------------------------------------------------------------

    def load(self) -> tuple[list[PositionRecord], dict[str, datetime]]:
        if not self.path.exists():
            return [], {}
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return [], {}
        records: list[PositionRecord] = []
        for r in raw.get("positions", []):
            try:
                records.append(PositionRecord(
                    strategy=r["strategy"],
                    symbol=r["symbol"],
                    quantity=int(r["quantity"]),
                    avg_price=float(r["avg_price"]),
                    initial_stop=float(r["initial_stop"]) if r.get("initial_stop") is not None else None,
                    current_stop=float(r["current_stop"]) if r.get("current_stop") is not None else None,
                    stop_order_id=r.get("stop_order_id"),
                    high_watermark=float(r["high_watermark"]) if r.get("high_watermark") is not None else None,
                ))
            except (KeyError, TypeError, ValueError):
                continue
        cooldowns: dict[str, datetime] = {}
        for sym, iso in raw.get("cooldowns", {}).items():
            try:
                cooldowns[sym] = datetime.fromisoformat(iso)
            except ValueError:
                pass
        return records, cooldowns

    def clear(self) -> None:
        if self.path.exists():
            self.path.unlink()
