from __future__ import annotations

from datetime import date, datetime
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd
import pytest

from pullback_trend.config import StrategyConfig
from pullback_trend.indicators import add_indicators
from pullback_trend.models import Quote
from pullback_trend.pullback import detect_pullback
from pullback_trend.regime import market_regime_ok
from pullback_trend.signal_engine import SignalEngine
from pullback_trend.trend import qualify_trend


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def _trend_row(**overrides) -> pd.DataFrame:
    defaults = {
        "close": 100.0, "ema9": 101.0, "ema20": 100.0, "ema50": 99.0,
        "vwap_calc": 99.5, "rvol": 2.0, "atr14": 0.5,
        "high": 101.0, "low": 99.0, "volume": 200_000,
    }
    defaults.update(overrides)
    return pd.DataFrame([defaults])


def _market_row(**overrides) -> pd.Series:
    defaults = {
        "close": 102.0, "vwap_calc": 100.0, "ema9": 102.0,
        "ema20": 100.0, "atr14": 0.5,
    }
    defaults.update(overrides)
    return pd.Series(defaults)


def _make_history(n: int = 35, pullback_pct: float = 0.012) -> pd.DataFrame:
    """Trend up for n-6 bars then pull back pullback_pct with declining volume."""
    trend_n = n - 6
    trend_prices = np.linspace(90.0, 100.0, trend_n)
    pb_prices = np.linspace(100.0, 100.0 * (1 - pullback_pct), 6)
    prices = np.concatenate([trend_prices, pb_prices])
    volumes = [200_000] * trend_n + [120_000, 100_000, 80_000, 60_000, 50_000, 40_000]
    ts = pd.date_range("2026-06-02 14:00:00", periods=n, freq="1min", tz="UTC")
    frame = pd.DataFrame({
        "symbol": ["NVDA"] * n, "timestamp": ts,
        "open": prices * 0.999, "high": prices * 1.003,
        "low": prices * 0.997, "close": prices,
        "volume": volumes,
    })
    return add_indicators(frame)


# ---------------------------------------------------------------------------
# detect_pullback
# ---------------------------------------------------------------------------

def test_detect_pullback_rejects_with_insufficient_history():
    ok, reasons, score, _ = detect_pullback(add_indicators(pd.DataFrame({
        "open": [100.0] * 10, "high": [101.0] * 10, "low": [99.0] * 10,
        "close": [100.0] * 10, "volume": [100_000] * 10,
    })), StrategyConfig())
    assert not ok
    assert "insufficient_history" in reasons
    assert score == 0.0


def test_detect_pullback_rejects_when_depth_too_large():
    n = 35
    up = np.linspace(90.0, 100.0, n - 6)
    crash = np.linspace(100.0, 94.0, 6)   # 6% pullback — far above 2.5% max
    prices = np.concatenate([up, crash])
    frame = pd.DataFrame({
        "open": prices * 0.999, "high": prices * 1.001,
        "low": prices * 0.998, "close": prices,
        "volume": [200_000] * n,
    })
    ok, reasons, _, _ = detect_pullback(add_indicators(frame), StrategyConfig())
    assert not ok
    assert "pullback_depth_invalid" in reasons


def test_detect_pullback_returns_prior_range_unavailable_on_empty_slice():
    # prior_breakout_lookback=1 → iloc[-1:-1] is empty → .max() returns NaN → guard fires
    cfg = StrategyConfig(prior_breakout_lookback=1)
    ok, reasons, score, _ = detect_pullback(_make_history(35), cfg)
    assert not ok
    assert "prior_range_unavailable" in reasons


# ---------------------------------------------------------------------------
# qualify_trend
# ---------------------------------------------------------------------------

def test_qualify_trend_passes_with_all_conditions_met():
    ok, reasons, score = qualify_trend(_trend_row(), StrategyConfig(min_rvol=1.5))
    assert ok
    assert score >= 0.7


def test_qualify_trend_fails_when_below_vwap():
    ok, reasons, _ = qualify_trend(_trend_row(close=98.0, vwap_calc=99.5), StrategyConfig())
    assert not ok
    assert "below_vwap" in reasons


def test_qualify_trend_fails_when_ema9_not_above_ema20():
    ok, reasons, _ = qualify_trend(_trend_row(ema9=98.0, ema20=100.0), StrategyConfig())
    assert not ok
    assert "ema9_not_above_ema20" in reasons


def test_qualify_trend_fails_when_relative_volume_too_low():
    ok, reasons, _ = qualify_trend(_trend_row(rvol=0.8), StrategyConfig(min_rvol=1.5))
    assert not ok
    assert "relative_volume_too_low" in reasons


# ---------------------------------------------------------------------------
# market_regime_ok
# ---------------------------------------------------------------------------

def test_market_regime_ok_returns_false_when_no_market_data():
    ok, reason, score = market_regime_ok(None, StrategyConfig())
    assert not ok
    assert reason == "market_regime_unavailable"
    assert score == 0.0


def test_market_regime_ok_fails_when_market_below_vwap():
    ok, reason, _ = market_regime_ok(_market_row(close=98.0, vwap_calc=100.0), StrategyConfig())
    assert not ok
    assert reason == "market_below_vwap"


def test_market_regime_ok_fails_when_ema_not_aligned():
    ok, reason, _ = market_regime_ok(_market_row(ema9=98.0, ema20=101.0), StrategyConfig())
    assert not ok
    assert reason == "market_ema_not_aligned"


def test_market_regime_ok_passes_with_aligned_market():
    ok, reason, _ = market_regime_ok(_market_row(), StrategyConfig(atr_compression_floor_bps=5.0))
    assert ok
    assert reason == "market_regime_ok"


# ---------------------------------------------------------------------------
# SignalEngine
# ---------------------------------------------------------------------------

def test_signal_engine_rejects_outside_trade_window():
    cfg = StrategyConfig(trade_windows=[["09:35", "11:30"]])
    engine = SignalEngine(cfg)
    history = _make_history(35)
    history = history.copy()
    # 20:00 UTC = 16:00 ET — after trade window end of 11:30 ET
    history.at[history.index[-1], "timestamp"] = pd.Timestamp("2026-06-02 20:00:00", tz="UTC")
    signal, decision = engine.evaluate("NVDA", history)
    assert signal is None
    assert decision["reason"] == "outside_trade_window"


def test_signal_engine_rejects_spread_too_wide():
    cfg = StrategyConfig(max_spread_bps=8.0, trade_windows=[["09:30", "16:00"]])
    engine = SignalEngine(cfg)
    history = _make_history(35)
    # All timestamps in history are 14:00-14:34 UTC = 10:00-10:34 ET (in window)
    wide_quote = Quote("NVDA", datetime(2026, 6, 2, 14, 0, tzinfo=ZoneInfo("UTC")), bid=99.0, ask=100.0)
    signal, decision = engine.evaluate("NVDA", history, quote=wide_quote)
    assert signal is None
    assert decision["reason"] == "spread_too_wide"


def test_signal_engine_rejects_empty_history():
    engine = SignalEngine(StrategyConfig())
    signal, decision = engine.evaluate("NVDA", pd.DataFrame())
    assert signal is None
    assert decision["reason"] == "no_history"
