from __future__ import annotations

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from pullback_trend.config import ExecutionConfig
from pullback_trend.logger import AuditLogger
from pullback_trend.models import Position, PositionState, Signal
from pullback_trend.orders import OrderManager
from pullback_trend.risk import RiskDecision


def now():
    return datetime(2026, 5, 25, 14, 0, tzinfo=ZoneInfo("UTC"))


def test_duplicate_entry_is_prevented(tmp_path):
    orders = OrderManager(ExecutionConfig(), AuditLogger(str(tmp_path), "dupe_entry"))
    position = Position("NVDA", state=PositionState.FLAT)
    signal = Signal("NVDA", now(), 100, 99, 102, 0.8, [])
    assert orders.submit_entry(signal, RiskDecision(True, "ok", 10), position) is not None
    assert orders.submit_entry(signal, RiskDecision(True, "ok", 10), position) is None


def test_duplicate_bracket_prevention(tmp_path):
    orders = OrderManager(ExecutionConfig(), AuditLogger(str(tmp_path), "dupe_bracket"))
    position = Position("NVDA", quantity=50, avg_price=100, stop_price=99, tp1_price=100.5, tp2_price=101.2)
    first = orders.submit_bracket(position, now())
    second = orders.submit_bracket(position, now())
    assert len(first) == 3
    assert second == []
    assert position.bracket_submitted_qty == 50


def test_stale_entry_cancels(tmp_path):
    orders = OrderManager(ExecutionConfig(entry_stale_seconds=5), AuditLogger(str(tmp_path), "stale"))
    position = Position("NVDA")
    order = orders.submit_entry(Signal("NVDA", now(), 100, 99, 102, 0.8, []), RiskDecision(True, "ok", 10), position)
    cancelled = orders.cancel_stale_entries(now() + timedelta(seconds=6))
    assert order in cancelled
    assert order.status.value == "CANCELLED"
