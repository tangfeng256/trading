from __future__ import annotations

import csv
import json
from dataclasses import asdict, is_dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


CSV_HEADER_SUPERSETS = {
    "decisions": [
        "strategy",
        "symbol",
        "timestamp",
        "phase",
        "passed",
        "approved",
        "reason",
        "mid",
        "spread",
        "delta",
        "absorption_score",
        "exhaustion_score",
        "trigger_score",
        "score",
    ],
}


class MultiStrategyLogger:
    def __init__(self, root: str | Path = "runs", run_id: str | None = None) -> None:
        run_id = run_id or datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
        self.run_dir = Path(root) / run_id
        self.run_dir.mkdir(parents=True, exist_ok=True)
        self._headers: dict[str, list[str]] = {}
        self._headers_written: set[str] = set()
        (self.run_dir / "events.jsonl").touch()

    def event(self, event_type: str, payload: dict[str, Any]) -> None:
        row = {"logged_at": datetime.now(timezone.utc).isoformat(), "type": event_type, **payload}
        with (self.run_dir / "events.jsonl").open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(_safe(row), sort_keys=True) + "\n")

    def csv(self, name: str, row: dict[str, Any]) -> None:
        path = self.run_dir / f"{name}.csv"
        clean = {key: _safe(value) for key, value in row.items()}
        headers = self._headers.setdefault(name, list(CSV_HEADER_SUPERSETS.get(name, clean.keys())))
        for key in clean:
            if key not in headers:
                headers.append(key)
        need_header = name not in self._headers_written
        if need_header:
            need_header = not (path.exists() and path.stat().st_size > 0)
            self._headers_written.add(name)
        with path.open("a", newline="", encoding="utf-8") as handle:
            writer = csv.DictWriter(handle, fieldnames=headers, extrasaction="ignore")
            if need_header:
                writer.writeheader()
            writer.writerow(clean)

    def finalize(self, summary: dict[str, Any]) -> None:
        (self.run_dir / "summary.json").write_text(json.dumps(_safe(summary), indent=2), encoding="utf-8")


def _safe(value: Any) -> Any:
    if is_dataclass(value):
        return _safe(asdict(value))
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, dict):
        return {str(k): _safe(v) for k, v in value.items()}
    if isinstance(value, set):
        return [_safe(v) for v in sorted(value, key=str)]
    if isinstance(value, (list, tuple)):
        return [_safe(v) for v in value]
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return str(value)
