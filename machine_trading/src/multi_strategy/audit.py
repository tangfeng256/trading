from __future__ import annotations

import csv
import json
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo


@dataclass
class DecisionRecord:
    source: str
    strategy: str
    symbol: str
    timestamp: str
    phase: str
    approved: bool
    reason: str
    score: str


@dataclass
class BlockRecord:
    source: str
    strategy: str
    symbol: str
    timestamp: str
    reason: str
    phase: str = ""


def audit_run(run_dir: str | Path, *, trading_timezone: str = "America/New_York", write_outputs: bool = True) -> dict[str, Any]:
    run_path = Path(run_dir)
    if not run_path.exists():
        raise FileNotFoundError(f"run folder does not exist: {run_path}")

    decisions = _read_decisions(run_path)
    events = _read_events(run_path / "events.jsonl")
    event_blocks = _blocks_from_events(events)
    decision_blocks = _blocking_decisions(decisions)
    blocks = [*event_blocks, *decision_blocks]
    candidates = [record for record in decisions if record.approved]
    candidate_rows = _candidate_rows(candidates, blocks)
    order_files = _count_named_csvs(run_path, "orders.csv")
    fill_files = _count_named_csvs(run_path, "fills.csv")
    market_data = {
        "dom_ticks": _count_csv_rows_and_symbols(run_path / "dom_ticks.csv"),
        "depth_snapshots": _count_csv_rows_and_symbols(run_path / "depth_snapshots.csv"),
    }
    window = _time_window(decisions, events, trading_timezone)
    strategy_stats = _strategy_stats(decisions)
    top_reasons = Counter(record.reason for record in decisions if record.reason)
    warnings = _operational_warnings(run_path, decisions, events, market_data)

    orders = sum(item["rows"] for item in order_files)
    fills = sum(item["rows"] for item in fill_files)
    assessment = _assessment(orders, fills, candidates, blocks, warnings)
    summary = {
        "run_dir": str(run_path),
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "window": window,
        "assessment": assessment,
        "files": {
            "summary_json_exists": (run_path / "summary.json").exists(),
            "combined_decision_rows": _count_csv_rows(run_path / "decisions.csv"),
            "decision_rows": len(decisions),
            "event_rows": len(events),
            "orders": orders,
            "fills": fills,
            "order_files": order_files,
            "fill_files": fill_files,
            "market_data": market_data,
        },
        "strategies": strategy_stats,
        "top_reasons": dict(top_reasons.most_common(20)),
        "risk_rejects": dict(Counter(block.reason for block in blocks if block.reason).most_common(20)),
        "signal_candidates": [row for row in candidate_rows[:100]],
        "blocking_rejections": [_block_row(block) for block in blocks[:100]],
        "operational_warnings": warnings,
        "outputs": {
            "summary": str(run_path / "daily_audit_summary.json"),
            "candidates": str(run_path / "daily_audit_candidates.csv"),
        },
    }

    if write_outputs:
        _write_json(run_path / "daily_audit_summary.json", summary)
        _write_candidates(run_path / "daily_audit_candidates.csv", candidate_rows)
    return summary


def _read_decisions(run_path: Path) -> list[DecisionRecord]:
    records: list[DecisionRecord] = []
    for path in sorted(run_path.rglob("decisions.csv")):
        if path.name.startswith("daily_audit"):
            continue
        strategy_hint = _strategy_from_path(path.relative_to(run_path))
        records.extend(_read_decision_file(path, run_path, strategy_hint))
    return records


def _read_decision_file(path: Path, run_path: Path, strategy_hint: str) -> list[DecisionRecord]:
    if not path.exists() or path.stat().st_size == 0:
        return []
    source = str(path.relative_to(run_path))
    rows: list[DecisionRecord] = []
    with path.open(newline="", encoding="utf-8") as handle:
        reader = csv.DictReader(handle)
        for row in reader:
            extra = row.get(None) or []
            strategy = str(row.get("strategy") or strategy_hint or "").strip()
            symbol = str(row.get("symbol") or "").strip()
            timestamp = _normalize_timestamp(row.get("timestamp") or row.get("time") or row.get("logged_at") or "")
            phase = str(row.get("phase") or "").strip()
            reason = str(row.get("reason") or "").strip()
            approved = _is_true(row.get("approved")) or _is_true(row.get("passed")) or any(_is_true(value) for value in extra)
            score = str(row.get("score") or _last_number(extra) or "").strip()
            rows.append(DecisionRecord(source, strategy, symbol, timestamp, phase, approved, reason, score))
    return rows


def _read_events(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    events = []
    with path.open(encoding="utf-8") as handle:
        for line in handle:
            if not line.strip():
                continue
            try:
                events.append(json.loads(line))
            except json.JSONDecodeError:
                events.append({"type": "parse_error", "raw": line.strip()})
    return events


def _blocks_from_events(events: list[dict[str, Any]]) -> list[BlockRecord]:
    blocks = []
    seen_entry_blocks: set[tuple[str, str, str]] = set()
    for event in events:
        event_type = event.get("type")
        if event_type not in {"risk_reject", "strategy_skip_entry_blocked"}:
            continue
        strategy = str(event.get("strategy") or "")
        symbol = str(event.get("symbol") or "")
        reason = str(event.get("reason") or "")
        if event_type == "strategy_skip_entry_blocked":
            key = (strategy, symbol, reason)
            if key in seen_entry_blocks:
                continue
            seen_entry_blocks.add(key)
        blocks.append(
            BlockRecord(
                source="events.jsonl",
                strategy=strategy,
                symbol=symbol,
                timestamp=_normalize_timestamp(event.get("logged_at") or event.get("time") or ""),
                reason=reason,
            )
        )
    return blocks


def _blocking_decisions(decisions: list[DecisionRecord]) -> list[BlockRecord]:
    blocks = []
    for record in decisions:
        if record.approved:
            continue
        reason = record.reason.lower()
        phase = record.phase.lower()
        if phase == "risk" or any(token in reason for token in ("risk", "slippage", "stop_too_wide", "stop_too_small")):
            blocks.append(BlockRecord(record.source, record.strategy, record.symbol, record.timestamp, record.reason, record.phase))
    return blocks


def _candidate_rows(candidates: list[DecisionRecord], blocks: list[BlockRecord]) -> list[dict[str, Any]]:
    rows = []
    used_blocks: set[int] = set()
    for candidate in candidates:
        block_index, block = _match_block(candidate, blocks, used_blocks)
        if block_index is not None:
            used_blocks.add(block_index)
        rows.append(
            {
                "strategy": candidate.strategy,
                "symbol": candidate.symbol,
                "timestamp": candidate.timestamp or (block.timestamp if block else ""),
                "source": candidate.source,
                "decision_reason": candidate.reason,
                "score": candidate.score,
                "blocked_by": block.source if block else "",
                "blocked_reason": block.reason if block else "",
                "blocked_at": block.timestamp if block else "",
            }
        )
    for index, block in enumerate(blocks):
        if index in used_blocks:
            continue
        rows.append(
            {
                "strategy": block.strategy,
                "symbol": block.symbol,
                "timestamp": block.timestamp,
                "source": "",
                "decision_reason": "",
                "score": "",
                "blocked_by": block.source,
                "blocked_reason": block.reason,
                "blocked_at": block.timestamp,
            }
        )
    return rows


def _match_block(candidate: DecisionRecord, blocks: list[BlockRecord], used: set[int]) -> tuple[int | None, BlockRecord | None]:
    fallback: tuple[int | None, BlockRecord | None] = (None, None)
    candidate_ts = _parse_timestamp(candidate.timestamp)
    for index, block in enumerate(blocks):
        if index in used:
            continue
        if block.strategy != candidate.strategy or block.symbol != candidate.symbol:
            continue
        if fallback[1] is None:
            fallback = (index, block)
        block_ts = _parse_timestamp(block.timestamp)
        if candidate_ts and block_ts and abs((block_ts - candidate_ts).total_seconds()) <= 180:
            return index, block
    return fallback


def _strategy_stats(decisions: list[DecisionRecord]) -> dict[str, Any]:
    strategies: dict[str, Any] = {}
    grouped: dict[str, list[DecisionRecord]] = defaultdict(list)
    for record in decisions:
        grouped[record.strategy or "unknown"].append(record)
    for strategy, rows in sorted(grouped.items()):
        by_reason = Counter(record.reason for record in rows if record.reason)
        by_symbol: dict[str, Any] = {}
        symbol_groups: dict[str, list[DecisionRecord]] = defaultdict(list)
        for record in rows:
            symbol_groups[record.symbol or "unknown"].append(record)
        for symbol, symbol_rows in sorted(symbol_groups.items()):
            by_symbol[symbol] = {
                "decisions": len(symbol_rows),
                "approved": sum(1 for record in symbol_rows if record.approved),
                "top_reasons": dict(Counter(record.reason for record in symbol_rows if record.reason).most_common(5)),
            }
        strategies[strategy] = {
            "decisions": len(rows),
            "approved": sum(1 for record in rows if record.approved),
            "top_reasons": dict(by_reason.most_common(10)),
            "symbols": by_symbol,
        }
    return strategies


def _assessment(
    orders: int,
    fills: int,
    candidates: list[DecisionRecord],
    blocks: list[BlockRecord],
    warnings: list[str],
) -> dict[str, Any]:
    status = "traded" if fills else "orders_without_fills" if orders else "no_trades"
    if fills:
        explanation = "fills were recorded; evaluate realized P&L and execution quality"
    elif candidates and blocks:
        explanation = "signals were produced, but risk or execution gates blocked order submission"
    elif candidates:
        explanation = "signals were produced, but no matching order or risk rejection was found"
    elif blocks:
        explanation = "strategy entry paths were operationally blocked before an order could be submitted"
    else:
        explanation = "no strategy produced a tradeable signal in the captured window"
    return {
        "status": status,
        "primary_explanation": explanation,
        "orders": orders,
        "fills": fills,
        "approved_signals": len(candidates),
        "blocking_rejections": len(blocks),
        "replay_recommended": bool(not fills and (candidates or blocks or warnings)),
        "replay_questions": _replay_questions(candidates, blocks, warnings),
    }


def _replay_questions(candidates: list[DecisionRecord], blocks: list[BlockRecord], warnings: list[str]) -> list[str]:
    questions = []
    if candidates:
        questions.append("Would the same captured inputs reproduce the same signal decisions?")
    if blocks:
        questions.append("Were the risk gates correct, and what would have happened with adjusted thresholds?")
    if warnings:
        questions.append("Did data quality or run-finalization issues affect decision quality?")
    return questions


def _operational_warnings(
    run_path: Path,
    decisions: list[DecisionRecord],
    events: list[dict[str, Any]],
    market_data: dict[str, dict[str, Any]],
) -> list[str]:
    warnings = []
    if not (run_path / "summary.json").exists():
        warnings.append("summary.json is missing; the run may not have finalized cleanly")
    if not decisions:
        warnings.append("no decisions.csv rows were found")
    malformed = _malformed_combined_rows(run_path / "decisions.csv")
    if malformed:
        warnings.append(f"combined decisions.csv has {malformed} rows with extra unnamed columns; future runs should use normalized headers")
    depth_events = [event for event in events if event.get("type") == "depth_subscription_limit"]
    for event in depth_events:
        l1_only = ", ".join(event.get("l1_only", []) or [])
        selected = ", ".join(event.get("selected", []) or [])
        warnings.append(f"L2 depth selected for {selected or 'none'}; L1 only for {l1_only or 'none'}")
    depth_blocks = sorted({str(event.get("symbol") or "") for event in events if event.get("type") == "depth_strategy_symbol_blocked" and event.get("symbol")})
    if depth_blocks:
        warnings.append(f"IBKR depth permissions blocked L2 strategies for {', '.join(depth_blocks)}")
    unmanaged = sorted({str(event.get("symbol") or "") for event in events if event.get("type") == "unmanaged_position_quarantined" and event.get("symbol")})
    if unmanaged:
        warnings.append(f"unmanaged account positions quarantined entries for {', '.join(unmanaged)}")
    if market_data["dom_ticks"]["rows"] == 0:
        warnings.append("no DOM ticks were captured")
    if market_data["depth_snapshots"]["rows"] == 0:
        warnings.append("no depth snapshots were captured")
    return warnings


def _time_window(decisions: list[DecisionRecord], events: list[dict[str, Any]], trading_timezone: str) -> dict[str, str]:
    timestamps: list[datetime] = []
    for record in decisions:
        parsed = _parse_timestamp(record.timestamp)
        if parsed:
            timestamps.append(parsed)
    for event in events:
        parsed = _parse_timestamp(event.get("logged_at") or event.get("time") or "")
        if parsed:
            timestamps.append(parsed)
    if not timestamps:
        return {"start_utc": "", "end_utc": "", "start_local": "", "end_local": "", "timezone": trading_timezone}
    start = min(timestamps).astimezone(timezone.utc)
    end = max(timestamps).astimezone(timezone.utc)
    tz = ZoneInfo(trading_timezone)
    return {
        "start_utc": start.isoformat(),
        "end_utc": end.isoformat(),
        "start_local": start.astimezone(tz).isoformat(),
        "end_local": end.astimezone(tz).isoformat(),
        "timezone": trading_timezone,
    }


def _count_named_csvs(run_path: Path, name: str) -> list[dict[str, Any]]:
    files = []
    for path in sorted(run_path.rglob(name)):
        if path.name.startswith("daily_audit") or path.name.startswith("replay_"):
            continue
        files.append({"path": str(path.relative_to(run_path)), "rows": _count_csv_rows(path)})
    return files


def _count_csv_rows_and_symbols(path: Path) -> dict[str, Any]:
    if not path.exists() or path.stat().st_size == 0:
        return {"rows": 0, "symbols": []}
    symbols: set[str] = set()
    rows = 0
    with path.open(newline="", encoding="utf-8") as handle:
        reader = csv.DictReader(handle)
        for row in reader:
            rows += 1
            symbol = row.get("symbol")
            if symbol:
                symbols.add(symbol)
    return {"rows": rows, "symbols": sorted(symbols)}


def _count_csv_rows(path: Path) -> int:
    if not path.exists() or path.stat().st_size == 0:
        return 0
    with path.open(newline="", encoding="utf-8") as handle:
        reader = csv.reader(handle)
        try:
            next(reader)
        except StopIteration:
            return 0
        return sum(1 for _ in reader)


def _malformed_combined_rows(path: Path) -> int:
    if not path.exists() or path.stat().st_size == 0:
        return 0
    malformed = 0
    with path.open(newline="", encoding="utf-8") as handle:
        for row in csv.DictReader(handle):
            if row.get(None):
                malformed += 1
    return malformed


def _strategy_from_path(relative: Path) -> str:
    parts = set(relative.parts)
    for strategy in ("absorption", "pullback", "opening_range"):
        if strategy in parts:
            return strategy
    return ""


def _block_row(block: BlockRecord) -> dict[str, str]:
    return {
        "source": block.source,
        "strategy": block.strategy,
        "symbol": block.symbol,
        "timestamp": block.timestamp,
        "phase": block.phase,
        "reason": block.reason,
    }


def _write_json(path: Path, data: dict[str, Any]) -> None:
    path.write_text(json.dumps(data, indent=2, sort_keys=True), encoding="utf-8")


def _write_candidates(path: Path, rows: list[dict[str, Any]]) -> None:
    headers = ["strategy", "symbol", "timestamp", "source", "decision_reason", "score", "blocked_by", "blocked_reason", "blocked_at"]
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=headers)
        writer.writeheader()
        writer.writerows(rows)


def _normalize_timestamp(value: Any) -> str:
    parsed = _parse_timestamp(value)
    return parsed.astimezone(timezone.utc).isoformat() if parsed else str(value or "")


def _parse_timestamp(value: Any) -> datetime | None:
    if not value:
        return None
    if isinstance(value, datetime):
        timestamp = value
    else:
        try:
            timestamp = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        except ValueError:
            return None
    if timestamp.tzinfo is None:
        timestamp = timestamp.replace(tzinfo=timezone.utc)
    return timestamp


def _is_true(value: Any) -> bool:
    return str(value).strip().lower() in {"true", "1", "yes"}


def _last_number(values: list[Any]) -> str:
    for value in reversed(values):
        text = str(value).strip()
        if not text:
            continue
        try:
            float(text)
        except ValueError:
            continue
        return text
    return ""
