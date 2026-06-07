from __future__ import annotations

import csv
import json
from dataclasses import asdict, is_dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from .utils_time import now_utc


class RunLogger:
    CSV_FIELDS: dict[str, list[str]] = {
        "decisions": [
            "timestamp", "symbol", "phase", "passed", "reason", "mid", "spread",
            "delta", "absorption_score", "exhaustion_score", "trigger_score",
        ],
        "features": [],
        "depth_snapshots": [],
        "tape": [],
        "signals": [],
        "orders": [],
        "fills": [],
        "positions": [],
        "trades": [],
    }

    def __init__(self, root: str | Path = "runs", run_id: str | None = None) -> None:
        run_id = run_id or now_utc().strftime("%Y%m%d_%H%M%S")
        self.run_dir = Path(root) / run_id
        self.run_dir.mkdir(parents=True, exist_ok=True)
        self._csv_headers: dict[str, list[str]] = {}
        (self.run_dir / "events.jsonl").touch()

    def _json_default(self, obj: Any) -> Any:
        if isinstance(obj, datetime):
            return obj.isoformat()
        if is_dataclass(obj):
            return asdict(obj)
        return str(obj)

    def event(self, event_type: str, payload: dict[str, Any]) -> None:
        row = {"timestamp": now_utc().isoformat(), "event_type": event_type, **payload}
        with (self.run_dir / "events.jsonl").open("a", encoding="utf-8") as f:
            f.write(json.dumps(row, default=self._json_default, sort_keys=True) + "\n")

    def csv(self, name: str, row: dict[str, Any]) -> None:
        path = self.run_dir / f"{name}.csv"
        normalized = {k: self._json_default(v) for k, v in row.items()}
        headers = self._csv_headers.get(name)
        if headers is None:
            configured = self.CSV_FIELDS.get(name, [])
            headers = configured or list(normalized.keys())
            for key in normalized:
                if key not in headers:
                    headers.append(key)
            self._csv_headers[name] = headers
        exists = path.exists() and path.stat().st_size > 0
        with path.open("a", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=headers, extrasaction="ignore")
            if not exists:
                writer.writeheader()
            writer.writerow(normalized)

    def decision(self, row: dict[str, Any]) -> None:
        self.csv("decisions", row)
        self.event("decision", row)

    def finalize(self, summary: dict[str, Any]) -> None:
        (self.run_dir / "summary.json").write_text(
            json.dumps(summary, indent=2, default=self._json_default, sort_keys=True),
            encoding="utf-8",
        )
