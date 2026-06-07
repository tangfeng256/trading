from __future__ import annotations

import csv
import json
from dataclasses import asdict, is_dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, Iterable, Optional


CSV_HEADERS = {
    "decisions.csv": ["time", "symbol", "phase", "passed", "reason", "features"],
    "signals.csv": ["time", "symbol", "entry_ref", "stop_ref", "target_ref", "confidence", "reason_codes", "features"],
    "orders.csv": ["time", "symbol", "event", "order_id", "side", "quantity", "price", "status", "reason"],
    "fills.csv": ["time", "symbol", "order_id", "side", "quantity", "price", "commission"],
    "positions.csv": ["time", "symbol", "quantity", "avg_price", "stop", "target", "realized_pnl", "reason"],
    "bars.csv": ["time", "symbol", "open", "high", "low", "close", "volume"],
    "book_snapshots.csv": ["time", "symbol", "bids", "asks"],
}


class AuditLogger:
    def __init__(self, base_dir: str = "runs", run_id: Optional[str] = None, write_book_snapshots: bool = True) -> None:
        timestamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
        self.run_dir = Path(base_dir) / (run_id or timestamp)
        self.run_dir.mkdir(parents=True, exist_ok=True)
        self.write_book_snapshots = write_book_snapshots
        self._init_csvs()

    def decision(self, symbol: str, phase: str, passed: bool, reason: str, features: Dict) -> None:
        self._append("decisions.csv", {
            "time": _now(),
            "symbol": symbol,
            "phase": phase,
            "passed": passed,
            "reason": reason,
            "features": _json(features),
        })
        self.event("decision", {"symbol": symbol, "phase": phase, "passed": passed, "reason": reason, "features": features})

    def signal(self, signal) -> None:
        self._append("signals.csv", {
            "time": _iso(signal.timestamp),
            "symbol": signal.symbol,
            "entry_ref": signal.entry_ref,
            "stop_ref": signal.stop_ref,
            "target_ref": signal.target_ref,
            "confidence": signal.confidence,
            "reason_codes": "|".join(signal.reason_codes),
            "features": _json(signal.features),
        })
        self.event("signal", asdict(signal))

    def order(self, **row) -> None:
        row.setdefault("time", _now())
        self._append("orders.csv", row)
        self.event("order", row)

    def fill(self, **row) -> None:
        row.setdefault("time", _now())
        self._append("fills.csv", row)
        self.event("fill", row)

    def position(self, **row) -> None:
        row.setdefault("time", _now())
        self._append("positions.csv", row)
        self.event("position", row)

    def bar(self, bar) -> None:
        self._append("bars.csv", {
            "time": _iso(bar.timestamp),
            "symbol": bar.symbol,
            "open": bar.open,
            "high": bar.high,
            "low": bar.low,
            "close": bar.close,
            "volume": bar.volume,
        })

    def book(self, book) -> None:
        if not self.write_book_snapshots:
            return
        self._append("book_snapshots.csv", {
            "time": _iso(book.timestamp),
            "symbol": book.symbol,
            "bids": _json(book.bids),
            "asks": _json(book.asks),
        })

    def event(self, event_type: str, payload: Dict) -> None:
        with (self.run_dir / "events.jsonl").open("a", encoding="ascii", newline="") as handle:
            handle.write(json.dumps({"type": event_type, "payload": _safe(payload)}, separators=(",", ":")) + "\n")

    def _init_csvs(self) -> None:
        for name, headers in CSV_HEADERS.items():
            with (self.run_dir / name).open("w", encoding="ascii", newline="") as handle:
                csv.DictWriter(handle, fieldnames=headers).writeheader()
        (self.run_dir / "events.jsonl").write_text("", encoding="ascii")

    def _append(self, name: str, row: Dict) -> None:
        headers = CSV_HEADERS[name]
        clean = {header: _safe(row.get(header, "")) for header in headers}
        with (self.run_dir / name).open("a", encoding="ascii", newline="") as handle:
            csv.DictWriter(handle, fieldnames=headers).writerow(clean)


def _now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _iso(value) -> str:
    return value.isoformat() if hasattr(value, "isoformat") else str(value)


def _json(value) -> str:
    return json.dumps(_safe(value), separators=(",", ":"))


def _safe(value):
    if is_dataclass(value):
        return _safe(asdict(value))
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, dict):
        return {str(k): _safe(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_safe(v) for v in value]
    if isinstance(value, (str, int, float, bool)) or value is None:
        if isinstance(value, str):
            return value.encode("ascii", "ignore").decode("ascii")
        return value
    return str(value).encode("ascii", "ignore").decode("ascii")
