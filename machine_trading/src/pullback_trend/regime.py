from __future__ import annotations

import pandas as pd

from .config import StrategyConfig


def market_regime_ok(market_row: pd.Series | None, config: StrategyConfig) -> tuple[bool, str, float]:
    if market_row is None:
        return False, "market_regime_unavailable", 0.0
    if market_row["close"] <= market_row["vwap_calc"]:
        return False, "market_below_vwap", 0.1
    if market_row["ema9"] < market_row["ema20"]:
        return False, "market_ema_not_aligned", 0.2
    atr_bps = market_row["atr14"] / market_row["close"] * 10_000
    if atr_bps < config.atr_compression_floor_bps:
        return False, "market_atr_compressed", 0.2
    if market_row["rvol"] < config.min_market_rvol:
        return False, "market_volume_low", 0.3
    return True, "market_regime_ok", 1.0
