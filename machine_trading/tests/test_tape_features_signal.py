from datetime import datetime, timedelta, timezone

from absorption.config import StrategyConfig
from absorption.depth_book import DepthBook
from absorption.features import FeatureEngine
from absorption.signal_engine import SignalEngine
from absorption.tape import Tape


def test_aggressive_sell_classification_works():
    tape = Tape("NVDA")
    now = datetime.now(timezone.utc)
    trade = tape.add_trade(now, price=100.00, size=25, bid=100.00, ask=100.02)
    assert trade.side == "sell"
    assert tape.aggressive_sell_count(now, 3) == 1
    assert tape.signed_delta(now, 3) == -25


def test_absorption_score_rises_when_sells_hit_bid_but_bid_replenishes():
    cfg = StrategyConfig(max_spread_bps=10)
    book = DepthBook("NVDA")
    tape = Tape("NVDA")
    engine = FeatureEngine(cfg)
    base = datetime.now(timezone.utc)
    book.apply_update(0, 0, "bid", 100.00, 100, timestamp=base)
    book.apply_update(0, 0, "ask", 100.02, 100, timestamp=base)
    low = engine.compute("NVDA", base, book, tape)["absorption_score"]
    for i in range(10):
        ts = base + timedelta(milliseconds=200 * (i + 1))
        tape.add_trade(ts, 100.00, 250, bid=100.00, ask=100.02)
        book.apply_update(0, 1, "bid", 100.00, 100 + i * 25, timestamp=ts)
    high = engine.compute("NVDA", base + timedelta(seconds=3), book, tape)["absorption_score"]
    assert high > low
    assert high >= 0.70


def test_no_signal_if_price_keeps_falling():
    cfg = StrategyConfig(max_spread_bps=10)
    engine = SignalEngine(cfg, tick_size=0.01)
    t = datetime.now(timezone.utc)
    absorption_features = _features(t, mid=100.00, absorption=0.8, exhaustion=0.2, trigger=0.1, price_progress=-1)
    signal, _ = engine.evaluate("NVDA", absorption_features)
    assert signal is None
    falling_features = _features(t + timedelta(seconds=1), mid=99.95, absorption=0.2, exhaustion=0.7, trigger=0.8, price_progress=-12)
    signal, decision = engine.evaluate("NVDA", falling_features)
    assert signal is None
    assert decision["reason"] == "absorption_failed_price_kept_falling"


def test_no_entry_before_trigger_confirmation():
    cfg = StrategyConfig(max_spread_bps=10)
    engine = SignalEngine(cfg, tick_size=0.01)
    t = datetime.now(timezone.utc)
    engine.evaluate("NVDA", _features(t, mid=100.00, absorption=0.8, exhaustion=0.1, trigger=0.1))
    signal, decision = engine.evaluate("NVDA", _features(t + timedelta(seconds=1), mid=100.01, absorption=0.2, exhaustion=0.8, trigger=0.1))
    assert signal is None
    assert decision["reason"] == "waiting_for_trigger"


def test_no_entry_if_spread_too_wide():
    cfg = StrategyConfig(max_spread_bps=6)
    engine = SignalEngine(cfg)
    signal, decision = engine.evaluate("NVDA", _features(datetime.now(timezone.utc), spread_bps=20))
    assert signal is None
    assert decision["reason"] == "spread_too_wide"


def test_window_between_returns_only_trades_in_range():
    tape = Tape("NVDA")
    base = datetime(2026, 6, 2, 14, 0, 0, tzinfo=timezone.utc)
    tape.add_trade(base, 100.00, 10, bid=100.00, ask=100.02)
    tape.add_trade(base + timedelta(seconds=3), 100.01, 20, bid=100.01, ask=100.02)
    tape.add_trade(base + timedelta(seconds=7), 100.02, 30, bid=100.01, ask=100.02)

    result = tape.window_between(base + timedelta(seconds=2), base + timedelta(seconds=6))

    assert len(result) == 1
    assert result[0].size == 20


def test_previous_sell_count_does_not_overlap_with_current_window():
    # Populate tape: 5 sells at t=[-8, -7, -6, -5, -4] (prior window),
    # 0 sells at t=[-3, -2, -1] (current window). sell_slowdown should be 1.0.
    tape = Tape("NVDA")
    base = datetime(2026, 6, 2, 14, 0, 0, tzinfo=timezone.utc)
    for offset in range(1, 6):
        tape.add_trade(base - timedelta(seconds=offset + 3), 100.0, 50, bid=100.0, ask=100.02)

    cfg = StrategyConfig(max_spread_bps=10, exhaustion_window_sec=5)
    book = DepthBook("NVDA")
    book.apply_update(0, 0, "bid", 100.00, 100, timestamp=base)
    book.apply_update(0, 0, "ask", 100.02, 100, timestamp=base)
    engine = FeatureEngine(cfg)
    features = engine.compute("NVDA", base, book, tape)

    # No sells in the last 3 seconds — sell_slowdown should be 1.0 → exhaustion_score boosted
    assert features["exhaustion_score"] > 0.3


def _features(
    ts,
    mid=100.0,
    absorption=0.1,
    exhaustion=0.1,
    trigger=0.1,
    spread_bps=1.0,
    price_progress=0.0,
):
    return {
        "timestamp": ts,
        "mid": mid,
        "spread": 0.01,
        "spread_bps": spread_bps,
        "delta_10s": -500,
        "sell_hit_count_3s": 5,
        "trade_velocity_3s": 2,
        "absorption_score": absorption,
        "exhaustion_score": exhaustion,
        "trigger_score": trigger,
        "price_progress_bps": price_progress,
        "vwap_1m": mid - 0.01,
    }
