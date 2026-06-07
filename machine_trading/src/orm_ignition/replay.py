from __future__ import annotations

from pathlib import Path
from typing import Optional

import pandas as pd


def run_replay(run_dir: str | Path, chart: bool = False) -> Path:
    path = Path(run_dir)
    bars = _read(path / "bars.csv")
    signals = _read(path / "signals.csv")
    orders = _read(path / "orders.csv")
    fills = _read(path / "fills.csv")
    timeline = pd.concat(
        [
            _tag(bars, "bar"),
            _tag(signals, "signal"),
            _tag(orders, "order"),
            _tag(fills, "fill"),
        ],
        ignore_index=True,
    ).sort_values("time")
    out = path / "replay_timeline.csv"
    timeline.to_csv(out, index=False)
    if chart:
        _chart(path, bars, signals, fills)
    return out


def _read(path: Path) -> pd.DataFrame:
    if not path.exists():
        return pd.DataFrame()
    return pd.read_csv(path)


def _tag(df: pd.DataFrame, event: str) -> pd.DataFrame:
    if df.empty:
        return pd.DataFrame(columns=["time", "event"])
    out = df.copy()
    out["event"] = event
    return out


def _chart(path: Path, bars: pd.DataFrame, signals: pd.DataFrame, fills: pd.DataFrame) -> Optional[Path]:
    if bars.empty:
        return None
    import matplotlib.pyplot as plt

    for symbol, group in bars.groupby("symbol"):
        group = group.copy()
        group["time"] = pd.to_datetime(group["time"])
        plt.figure(figsize=(12, 6))
        plt.plot(group["time"], group["close"], label="close")
        sym_signals = signals[signals["symbol"] == symbol] if not signals.empty else pd.DataFrame()
        if not sym_signals.empty:
            plt.scatter(pd.to_datetime(sym_signals["time"]), sym_signals["entry_ref"], marker="^", label="signals")
        plt.title(symbol)
        plt.legend()
        out = path / f"replay_{symbol}.png"
        plt.savefig(out)
        plt.close()
    return path
