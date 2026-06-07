from __future__ import annotations

from pathlib import Path

import pandas as pd

from .indicators import add_indicators


def load_bars(paths: str | Path | list[str | Path]) -> pd.DataFrame:
    if isinstance(paths, (str, Path)):
        p = Path(paths)
        if p.is_dir():
            csv_files = sorted(p.glob("*.csv"))
            if not csv_files:
                raise ValueError(f"No CSV files found in {p}")
            return pd.concat([load_bars_csv(f) for f in csv_files], ignore_index=True).sort_values("timestamp").reset_index(drop=True)
        return load_bars_csv(p)
    frames = [load_bars_csv(Path(p)) for p in paths]
    return pd.concat(frames, ignore_index=True).sort_values("timestamp").reset_index(drop=True)


def load_bars_csv(path: str | Path, default_symbol: str | None = None) -> pd.DataFrame:
    frame = pd.read_csv(path)
    if "timestamp" not in frame.columns:
        raise ValueError("bars CSV must include timestamp")
    if "symbol" not in frame.columns:
        if not default_symbol:
            default_symbol = Path(path).stem.split("_", 1)[0].upper()
        frame["symbol"] = default_symbol
    frame["timestamp"] = pd.to_datetime(frame["timestamp"], utc=True)
    if "vwap" not in frame.columns:
        frame["vwap"] = pd.NA
    required = ["symbol", "timestamp", "open", "high", "low", "close", "volume", "vwap"]
    missing = [name for name in required if name not in frame.columns]
    if missing:
        raise ValueError(f"bars CSV missing columns: {missing}")
    frames = []
    for _, group in frame.sort_values(["symbol", "timestamp"]).groupby("symbol", sort=False):
        frames.append(add_indicators(group.reset_index(drop=True)))
    return pd.concat(frames, ignore_index=True).sort_values("timestamp").reset_index(drop=True)
