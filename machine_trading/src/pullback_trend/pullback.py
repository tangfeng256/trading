from __future__ import annotations

import pandas as pd

from .config import StrategyConfig


def detect_pullback(history: pd.DataFrame, config: StrategyConfig) -> tuple[bool, list[str], float, dict[str, float]]:
    lookback = max(config.pullback_lookback, config.stabilization_lookback + 2)
    if len(history) < lookback + 20:
        return False, ["insufficient_history"], 0.0, {}
    recent = history.tail(lookback)
    prior_high = history.iloc[-config.prior_breakout_lookback : -lookback]["high"].max()
    pullback_low = float(recent["low"].min())
    last = recent.iloc[-1]
    high_before_pullback_raw = history.iloc[-config.prior_breakout_lookback : -config.pullback_lookback]["high"].max()
    if pd.isna(high_before_pullback_raw) or high_before_pullback_raw <= 0:
        return False, ["prior_range_unavailable"], 0.0, {}
    high_before_pullback = float(high_before_pullback_raw)
    depth = (high_before_pullback - pullback_low) / high_before_pullback

    reasons: list[str] = []
    score = 0.0
    if 0.001 <= depth <= 0.025:
        score += 0.25
    else:
        reasons.append("pullback_depth_invalid")
    if recent["volume"].tail(4).is_monotonic_decreasing or recent["volume"].iloc[-1] <= recent["volume"].head(3).mean():
        score += 0.2
    else:
        reasons.append("volume_not_declining")
    support_candidates = [last["ema9"], last["ema20"], last["vwap_calc"], prior_high]
    near_support = any(abs(pullback_low - level) / last["close"] * 10_000 <= config.support_tolerance_bps for level in support_candidates if pd.notna(level))
    if near_support:
        score += 0.25
    else:
        reasons.append("not_near_support")
    if pullback_low > min(last["ema20"], last["vwap_calc"]) * 0.995:
        score += 0.15
    else:
        reasons.append("aggressive_breakdown")
    if _stabilizing(recent, config):
        score += 0.15
    else:
        reasons.append("not_stabilized")
    features = {"pullback_low": pullback_low, "prior_breakout": float(prior_high), "depth": float(depth)}
    hard_rejects = {"aggressive_breakdown", "pullback_depth_invalid", "insufficient_history"}
    acceptable = score >= 0.55 and not any(reason in hard_rejects for reason in reasons)
    return acceptable, reasons or ["pullback_ok"], score, features


def _stabilizing(recent: pd.DataFrame, config: StrategyConfig) -> bool:
    candles = recent.tail(config.stabilization_lookback)
    ranges = (candles["high"] - candles["low"]).tolist()
    close_locations = ((candles["close"] - candles["low"]) / (candles["high"] - candles["low"]).replace(0, pd.NA)).fillna(0.5)
    return ranges[-1] <= max(ranges[0], 0.01) and close_locations.iloc[-1] >= 0.5


def continuation_trigger(history: pd.DataFrame, config: StrategyConfig) -> tuple[bool, str]:
    if len(history) < config.breakout_lookback + 1:
        return False, "insufficient_trigger_history"
    last = history.iloc[-1]
    previous = history.iloc[-config.breakout_lookback - 1 : -1]
    if last["close"] > last["ema9"] and last["high"] > previous["high"].max():
        return True, "break_stabilization_high"
    if last["close"] > last["ema9"] and last["close"] > last["vwap_calc"]:
        return True, "reclaim_ema9_vwap"
    return False, "no_continuation_trigger"
