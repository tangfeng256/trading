from __future__ import annotations

import csv
import json
from pathlib import Path
from typing import Any


def _read_csv(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    with path.open(newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def replay_run(run_dir: str | Path, chart: bool = False) -> dict[str, Any]:
    run_path = Path(run_dir)
    decisions = _read_csv(run_path / "decisions.csv")
    features = _read_csv(run_path / "features.csv")
    signals = _read_csv(run_path / "signals.csv")
    timeline_path = run_path / "replay_timeline.csv"
    fieldnames = [
        "timestamp", "symbol", "phase", "passed", "reason", "mid",
        "absorption_score", "exhaustion_score", "trigger_score",
    ]
    with timeline_path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        for row in decisions:
            writer.writerow(row)
    chart_path = None
    if chart:
        chart_path = _write_chart(run_path, features, signals)
    summary = {
        "run_dir": str(run_path),
        "decisions": len(decisions),
        "features": len(features),
        "signals": len(signals),
        "timeline": str(timeline_path),
        "chart": str(chart_path) if chart_path else None,
    }
    (run_path / "replay_summary.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")
    return summary


def _write_chart(run_path: Path, features: list[dict], signals: list[dict]) -> Path | None:
    try:
        import matplotlib.pyplot as plt
    except ImportError:
        return None
    xs = list(range(len(features)))
    mids = [float(row["mid"]) for row in features if row.get("mid")]
    if not xs or not mids:
        return None
    plt.figure(figsize=(12, 5))
    plt.plot(xs[: len(mids)], mids, label="mid")
    for signal in signals:
        if signal.get("absorption_level"):
            plt.axhline(float(signal["absorption_level"]), color="orange", linestyle="--", label="absorption")
        if signal.get("stop_price"):
            plt.axhline(float(signal["stop_price"]), color="red", linestyle=":", label="stop")
    plt.legend()
    path = run_path / "replay_chart.png"
    plt.tight_layout()
    plt.savefig(path)
    plt.close()
    return path
