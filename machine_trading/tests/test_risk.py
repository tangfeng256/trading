from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

from pullback_trend.config import RiskConfig
from pullback_trend.models import Signal
from pullback_trend.risk import RiskManager


def sig(entry=100.0, stop=99.0):
    return Signal("NVDA", datetime(2026, 5, 25, 14, 0, tzinfo=ZoneInfo("UTC")), entry, stop, 102, 0.8, [])


def test_position_size_respects_risk_budget():
    risk = RiskManager(RiskConfig(account_size=50_000, risk_per_trade_pct=0.0035))
    decision = risk.approve(sig())
    assert decision.approved
    assert decision.quantity <= 175


def test_stop_must_be_below_entry():
    decision = RiskManager(RiskConfig()).approve(sig(100, 100.1))
    assert not decision.approved
    assert decision.reason == "stop_not_below_entry"


def test_daily_controls_block_trading():
    risk = RiskManager(RiskConfig(account_size=50_000, max_daily_loss_pct=0.01))
    risk.realized_pnl = -500
    assert risk.can_open_new() == (False, "daily_loss_limit")


def test_daily_counters_reset_on_new_day():
    from datetime import date
    risk = RiskManager(RiskConfig(max_trades_per_day=2, account_size=50_000))
    risk._last_reset_date = date(2026, 6, 1)   # yesterday
    risk.trades_today = 2                        # maxed out
    risk.realized_pnl = -300.0

    can, reason = risk.can_open_new()

    assert can                          # daily reset cleared the counters
    assert risk.trades_today == 0
    assert risk.realized_pnl == 0.0
