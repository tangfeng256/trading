from __future__ import annotations

from pathlib import Path

import pandas as pd


def run_replay(run_dir: str | Path) -> Path:
    run_path = Path(run_dir)
    events = []
    for name, event in [("decisions.csv", "decision"), ("orders.csv", "order"), ("trades.csv", "trade")]:
        path = run_path / name
        if not path.exists():
            continue
        frame = pd.read_csv(path)
        if "timestamp" not in frame.columns:
            continue
        frame["event"] = event
        events.append(frame)
    timeline = pd.concat(events, ignore_index=True).sort_values("timestamp") if events else pd.DataFrame(columns=["timestamp", "event"])
    out = run_path / "replay_timeline.csv"
    timeline.to_csv(out, index=False)
    return out
