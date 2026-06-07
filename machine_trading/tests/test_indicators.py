from __future__ import annotations

import pandas as pd

from pullback_trend.indicators import add_indicators, atr, ema, vwap


def test_indicators_add_required_columns():
    frame = pd.DataFrame({"open": [10, 11, 12], "high": [11, 12, 13], "low": [9, 10, 11], "close": [10, 12, 12], "volume": [100, 200, 300]})
    out = add_indicators(frame)
    assert {"ema9", "ema20", "ema50", "vwap_calc", "atr14", "rvol", "rolling_high_20", "rolling_low_20"} <= set(out.columns)
    assert out["vwap_calc"].iloc[-1] == vwap(frame).iloc[-1]
    assert ema(frame["close"], 9).iloc[-1] > 10
    assert atr(frame, 14).iloc[-1] > 0
