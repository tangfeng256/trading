from __future__ import annotations

import csv
import json
from datetime import datetime
from pathlib import Path
from typing import Any


class AuditLogger:
    def __init__(self, base_dir: str = "runs", run_id: str | None = None) -> None:
        self.run_id = run_id or datetime.now().strftime("%Y%m%d_%H%M%S")
        self.run_dir = Path(base_dir) / self.run_id
        self.run_dir.mkdir(parents=True, exist_ok=True)
        self._files: dict[str, tuple[Any, csv.DictWriter]] = {}

    def decision(self, **row: Any) -> None:
        self._write("decisions.csv", ["timestamp", "symbol", "approved", "reason", "score", "features"], row)

    def trade(self, **row: Any) -> None:
        self._write("trades.csv", ["timestamp", "symbol", "event", "quantity", "price", "pnl", "reason"], row)

    def order(self, **row: Any) -> None:
        self._write("orders.csv", ["timestamp", "symbol", "event", "order_id", "side", "quantity", "price", "status", "reason", "parent_id"], row)

    def position(self, **row: Any) -> None:
        self._write("positions.csv", ["timestamp", "symbol", "state", "quantity", "avg_price", "stop_price", "tp1_price", "tp2_price", "realized_pnl"], row)

    def ensure_outputs(self) -> None:
        self._ensure("decisions.csv", ["timestamp", "symbol", "approved", "reason", "score", "features"])
        self._ensure("orders.csv", ["timestamp", "symbol", "event", "order_id", "side", "quantity", "price", "status", "reason", "parent_id"])
        self._ensure("trades.csv", ["timestamp", "symbol", "event", "quantity", "price", "pnl", "reason"])

    def summary(self, data: dict[str, Any]) -> None:
        (self.run_dir / "summary.json").write_text(json.dumps(data, indent=2, default=str), encoding="utf-8")

    def close(self) -> None:
        for handle, _ in self._files.values():
            handle.close()
        self._files.clear()

    def _write(self, name: str, columns: list[str], row: dict[str, Any]) -> None:
        self._ensure(name, columns)
        clean = {key: (json.dumps(value, default=str) if isinstance(value, (dict, list)) else value) for key, value in row.items()}
        self._files[name][1].writerow(clean)
        self._files[name][0].flush()

    def _ensure(self, name: str, columns: list[str]) -> None:
        if name in self._files:
            return
        handle = (self.run_dir / name).open("w", newline="", encoding="utf-8")
        writer = csv.DictWriter(handle, fieldnames=columns, extrasaction="ignore")
        writer.writeheader()
        handle.flush()
        self._files[name] = (handle, writer)
