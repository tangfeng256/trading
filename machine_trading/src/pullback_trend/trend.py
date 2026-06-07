from __future__ import annotations

import pandas as pd

from .config import StrategyConfig


def qualify_trend(history: pd.DataFrame, config: StrategyConfig) -> tuple[bool, list[str], float]:
    row = history.iloc[-1]
    reasons: list[str] = []
    score = 0.0
    if row["close"] > row["vwap_calc"]:
        score += 0.25
    else:
        reasons.append("below_vwap")
    if row["ema9"] > row["ema20"]:
        score += 0.25
    else:
        reasons.append("ema9_not_above_ema20")
    if row["ema20"] >= row["ema50"]:
        score += 0.2
    else:
        reasons.append("ema20_below_ema50")
    if row["rvol"] >= config.min_rvol:
        score += 0.3
    else:
        reasons.append("relative_volume_too_low")
    return not reasons, reasons or ["trend_ok"], score
