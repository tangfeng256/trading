from __future__ import annotations

from datetime import date, datetime, timezone
from zoneinfo import ZoneInfo

from orm_ignition.config import AppConfig, LoggingConfig, RiskConfig, StrategyConfig, config_from_dict
from orm_ignition.execution_manager import ExecutionManager, Position
from orm_ignition.logger import AuditLogger
from orm_ignition.market_state import Bar, MarketState, SymbolMarketState
from orm_ignition.risk_manager import RiskManager
from orm_ignition.scanner import Scanner
from orm_ignition.signal_engine import Signal, SignalEngine


def dt(hour: int, minute: int, day: int = 25) -> datetime:
    return datetime(2026, 5, day, hour, minute, tzinfo=timezone.utc)


def _cfg(tmp_path=None) -> AppConfig:
    from pathlib import Path
    logging = LoggingConfig(base_dir=str(tmp_path or Path("runs")), run_id="test_run")
    return AppConfig(
        strategy=StrategyConfig(
            symbols=["NVDA", "QQQ", "SPY"],
            min_volume=100,
            min_rel_volume=1.0,
            min_or_move_bps=10,
            max_spread_bps=20,
        ),
        risk=RiskConfig(
            account_size=50_000,
            entry_stale_seconds=5,
        ),
        logging=logging,
    )


# ---------------------------------------------------------------------------
# VWAP session reset
# ---------------------------------------------------------------------------

def test_vwap_resets_at_start_of_new_session_day():
    state = SymbolMarketState("NVDA")
    # Bar on day 1 (UTC 14:00 = 10:00 ET — same calendar date in ET and UTC for June)
    day1 = datetime(2026, 6, 1, 14, 0, tzinfo=timezone.utc)
    state.on_bar(Bar("NVDA", day1, 100.0, 101.0, 99.0, 100.0, 1_000))
    assert state.vwap == 100.0

    # Bar on day 2 — VWAP should reset and reflect only today's bar
    day2 = datetime(2026, 6, 2, 14, 0, tzinfo=timezone.utc)
    state.on_bar(Bar("NVDA", day2, 200.0, 201.0, 199.0, 200.0, 500))
    assert state.vwap == 200.0   # not a blend of 100 and 200


def test_vwap_accumulates_within_same_session():
    state = SymbolMarketState("NVDA")
    base = datetime(2026, 6, 2, 14, 0, tzinfo=timezone.utc)
    state.on_bar(Bar("NVDA", base, 100.0, 101.0, 99.0, 100.0, 1_000))
    state.on_bar(Bar("NVDA", base.replace(minute=1), 100.0, 101.0, 99.0, 200.0, 1_000))
    # cumulative_pv = 100*1000 + 200*1000 = 300_000, volume = 2000 → vwap = 150
    assert state.vwap == 150.0


# ---------------------------------------------------------------------------
# Market regime uses today's bars only
# ---------------------------------------------------------------------------

def test_market_risk_on_ignores_prior_day_performance():
    cfg = StrategyConfig(market_symbols=["QQQ"], market_negative_bps=-45.0)
    scanner = Scanner(cfg)
    state = SymbolMarketState("QQQ")

    # Yesterday QQQ was at 500 — a big drop to 400 today would fail the old check
    yesterday = datetime(2026, 6, 1, 14, 0, tzinfo=timezone.utc)
    state.on_bar(Bar("QQQ", yesterday, 500.0, 501.0, 499.0, 500.0, 1_000))

    # Today QQQ is flat at 400 (different day, opens at 400, closes at 400.5)
    today_open = datetime(2026, 6, 2, 14, 0, tzinfo=timezone.utc)
    today_close = datetime(2026, 6, 2, 14, 1, tzinfo=timezone.utc)
    state.on_bar(Bar("QQQ", today_open, 400.0, 401.0, 399.0, 400.0, 1_000))
    state.on_bar(Bar("QQQ", today_close, 400.0, 401.0, 399.0, 400.5, 1_000))

    # Old code: (400.5 - 500) / 500 * 10000 = -1990 bps → would return False (wrong)
    # New code: (400.5 - 400) / 400 * 10000 = 12.5 bps → returns True (correct)
    assert scanner._market_risk_on([state]) is True


def test_market_risk_on_correctly_blocks_when_today_is_down():
    cfg = StrategyConfig(market_symbols=["QQQ"], market_negative_bps=-45.0)
    scanner = Scanner(cfg)
    state = SymbolMarketState("QQQ")
    today = datetime(2026, 6, 2, 14, 0, tzinfo=timezone.utc)
    state.on_bar(Bar("QQQ", today, 400.0, 401.0, 399.0, 400.0, 1_000))
    # close drops 70 bps below open: (397.2 - 400) / 400 * 10000 = -70 bps < -45 threshold
    today2 = datetime(2026, 6, 2, 14, 1, tzinfo=timezone.utc)
    state.on_bar(Bar("QQQ", today2, 400.0, 400.0, 396.0, 397.2, 1_000))
    assert scanner._market_risk_on([state]) is False


# ---------------------------------------------------------------------------
# config_from_dict tolerates unknown keys
# ---------------------------------------------------------------------------

def test_config_from_dict_ignores_unknown_strategy_keys():
    data = {"strategy": {"unknown_future_field": "ignored", "min_volume": 50_000}}
    config = config_from_dict(data)
    assert config.strategy.min_volume == 50_000


def test_config_from_dict_ignores_unknown_risk_keys():
    data = {"risk": {"deprecated_key": 99, "account_size": 75_000.0}}
    config = config_from_dict(data)
    assert config.risk.account_size == 75_000.0


# ---------------------------------------------------------------------------
# on_fill / cancel_order guards
# ---------------------------------------------------------------------------

def test_on_fill_ignores_unknown_order_id(tmp_path):
    app = _cfg(tmp_path)
    execution = ExecutionManager(RiskManager(app.risk), app.risk, app.strategy, AuditLogger(str(tmp_path), "t"))
    execution.on_fill("ghost-order", dt(14, 0), 10, 100.0)  # must not raise


def test_cancel_order_ignores_unknown_order_id(tmp_path):
    app = _cfg(tmp_path)
    execution = ExecutionManager(RiskManager(app.risk), app.risk, app.strategy, AuditLogger(str(tmp_path), "t"))
    execution.cancel_order("ghost-order", dt(14, 0), "test")  # must not raise


# ---------------------------------------------------------------------------
# reconcile wires daily reset
# ---------------------------------------------------------------------------

def test_reconcile_resets_risk_counters_on_new_day(tmp_path):
    app = _cfg(tmp_path)
    risk = RiskManager(app.risk)
    execution = ExecutionManager(risk, app.risk, app.strategy, AuditLogger(str(tmp_path), "t"))

    risk.current_day = date(2026, 5, 24)   # yesterday
    risk.trades_today = 3
    risk.realized_pnl = -300.0

    execution.reconcile(dt(14, 0))         # date is 2026-05-25 → triggers reset

    assert risk.current_day == date(2026, 5, 25)
    assert risk.trades_today == 0
    assert risk.realized_pnl == 0.0
