from __future__ import annotations

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pandas as pd

from pullback_trend.config import StrategyConfig
from pullback_trend.indicators import add_indicators
from pullback_trend.models import DepthLevel, L2Snapshot, Quote
from pullback_trend.signal_engine import SignalEngine


def make_history(symbol="NVDA"):
    start = datetime(2026, 5, 25, 13, 35, tzinfo=ZoneInfo("UTC"))
    rows = []
    price = 100.0
    for i in range(25):
        close = price + i * 0.16
        rows.append({"symbol": symbol, "timestamp": start + timedelta(minutes=i), "open": close - 0.05, "high": close + 0.15, "low": close - 0.10, "close": close, "volume": 90000 + i * 1000, "vwap": pd.NA})
    highs = [103.9, 103.6, 103.45, 103.5, 103.7, 104.05]
    vols = [150000, 120000, 90000, 80000, 85000, 180000]
    for j, close in enumerate(highs):
        rows.append({"symbol": symbol, "timestamp": start + timedelta(minutes=25 + j), "open": close - 0.15, "high": close + 0.10, "low": close - 0.25, "close": close, "volume": vols[j], "vwap": pd.NA})
    return add_indicators(pd.DataFrame(rows))


def test_signal_emits_for_valid_pullback():
    cfg = StrategyConfig(min_volume=1000, min_rvol=1.0, min_score=0.6)
    history = make_history()
    market = make_history("QQQ")
    last = history.iloc[-1]
    signal, decision = SignalEngine(cfg).evaluate("NVDA", history, market, Quote("NVDA", last["timestamp"].to_pydatetime(), 104.0, 104.02))
    assert signal is not None, decision
    assert signal.stop_price < signal.entry_price


def test_rejects_wide_spread():
    cfg = StrategyConfig(min_volume=1000, min_rvol=1.0, min_score=0.6, max_spread_bps=1)
    history = make_history()
    last = history.iloc[-1]
    signal, decision = SignalEngine(cfg).evaluate("NVDA", history, make_history("QQQ"), Quote("NVDA", last["timestamp"].to_pydatetime(), 103, 104))
    assert signal is None
    assert decision["reason"] == "spread_too_wide"


def test_l2_book_is_required_when_enabled():
    cfg = StrategyConfig(min_volume=1000, min_rvol=1.0, min_score=0.6, use_l2=True)
    history = make_history()

    signal, decision = SignalEngine(cfg).evaluate("NVDA", history, make_history("QQQ"))

    assert signal is None
    assert decision["reason"] == "missing_l2"


def test_l2_imbalance_can_block_entry():
    cfg = StrategyConfig(min_volume=1000, min_rvol=1.0, min_score=0.6, use_l2=True, min_l2_imbalance=0.25)
    history = make_history()
    last = history.iloc[-1]
    weak_book = L2Snapshot(
        "NVDA",
        last["timestamp"].to_pydatetime(),
        bids=[DepthLevel(104.00, 100), DepthLevel(103.99, 50)],
        asks=[DepthLevel(104.02, 500), DepthLevel(104.03, 300)],
    )

    signal, decision = SignalEngine(cfg).evaluate("NVDA", history, make_history("QQQ"), l2=weak_book)

    assert signal is None
    assert decision["reason"] == "l2_imbalance_weak"


def test_supportive_l2_book_allows_entry_and_sets_ask_entry():
    cfg = StrategyConfig(min_volume=1000, min_rvol=1.0, min_score=0.6, use_l2=True, min_l2_imbalance=0.10)
    history = make_history()
    last = history.iloc[-1]
    supportive_book = L2Snapshot(
        "NVDA",
        last["timestamp"].to_pydatetime(),
        bids=[DepthLevel(104.00, 600), DepthLevel(103.99, 300)],
        asks=[DepthLevel(104.02, 300), DepthLevel(104.03, 100)],
    )

    signal, decision = SignalEngine(cfg).evaluate("NVDA", history, make_history("QQQ"), l2=supportive_book)

    assert signal is not None, decision
    assert signal.entry_price == 104.02
    assert signal.features["l2_imbalance"] > 0
