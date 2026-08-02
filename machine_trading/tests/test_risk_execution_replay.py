import csv
from datetime import datetime, timedelta, timezone

from absorption.config import ExecutionConfig, MarketConfig, RiskConfig, StrategyConfig
from absorption.execution_manager import ExecutionManager
from absorption.order_state import OrderStatus, Signal, TradeStatus
from absorption.replay import replay_run
from absorption.risk_manager import RiskManager


def test_no_entry_if_stop_greater_than_or_equal_entry():
    risk = RiskManager(RiskConfig(), StrategyConfig())
    decision = risk.approve(_signal(stop=100.00, entry=100.00))
    assert not decision.approved
    assert decision.reason == "stop_not_below_entry"


def test_position_size_respects_risk_dollars():
    risk = RiskManager(RiskConfig(account_equity=50_000, risk_per_trade_pct=0.0025, max_notional=25_000), StrategyConfig())
    decision = risk.approve(_signal(entry=100.00, stop=99.50))
    assert decision.approved
    assert decision.risk_dollars == 125
    assert decision.qty == 250


def test_partial_fill_creates_protection_for_filled_quantity_only():
    manager = ExecutionManager(ExecutionConfig(tp1_fraction=0.5), MarketConfig(), StrategyConfig())
    trade = manager.submit_entry(_signal(), qty=100)
    created = manager.on_fill(trade.entry_order_id, fill_qty=40, fill_price=100.0, timestamp=_now())
    assert trade.filled_qty == 40
    assert sum(1 for order in created if order.role == "stop") == 1
    stop = next(order for order in created if order.role == "stop")
    assert stop.qty == 40


def test_late_entry_fill_resizes_queued_protection_to_total_fill():
    manager = ExecutionManager(ExecutionConfig(tp1_fraction=0.33), MarketConfig(), StrategyConfig())
    trade = manager.submit_entry(_signal(), qty=121)
    queued = manager.on_fill(trade.entry_order_id, fill_qty=100, fill_price=100.0, timestamp=_now())

    manager.on_fill(trade.entry_order_id, fill_qty=21, fill_price=100.0, timestamp=_now() + timedelta(milliseconds=100))

    tp1 = next(order for order in queued if order.role == "tp1")
    tp2 = next(order for order in queued if order.role == "tp2")
    stop = next(order for order in queued if order.role == "stop")
    assert (tp1.qty, tp2.qty, stop.qty) == (39, 82, 121)
    assert tp1.qty + tp2.qty == trade.filled_qty
    assert trade.protection_qty == trade.filled_qty


def test_execution_prices_are_rounded_to_market_tick():
    manager = ExecutionManager(ExecutionConfig(tp1_fraction=0.5), MarketConfig(tick_size=0.01), StrategyConfig())
    trade = manager.submit_entry(_signal(entry=203.085, stop=202.6122, target1=203.8817, target2=204.6434), qty=100)
    entry = manager.orders[trade.entry_order_id]

    created = manager.on_fill(trade.entry_order_id, fill_qty=100, fill_price=203.09, timestamp=_now())
    stop = next(order for order in created if order.role == "stop")
    tp1 = next(order for order in created if order.role == "tp1")
    tp2 = next(order for order in created if order.role == "tp2")

    assert entry.price == 203.10
    assert stop.stop_price == 202.61
    assert tp1.price == 203.89
    assert tp2.price == 204.65


def test_tp1_does_not_create_duplicate_stop_target_pairs():
    manager = ExecutionManager(ExecutionConfig(tp1_fraction=0.5), MarketConfig(), StrategyConfig())
    trade = manager.submit_entry(_signal(), qty=100)
    first = manager.on_fill(trade.entry_order_id, fill_qty=40, fill_price=100.0, timestamp=_now())
    second = manager.ensure_protection(trade.trade_id, _now())
    assert first
    assert second == []
    assert sum(1 for order in trade.orders.values() if order.role == "stop") == 1
    assert sum(1 for order in trade.orders.values() if order.role == "tp1") == 1


def test_exit_fills_accumulate_realized_pnl_and_close_only_when_flat():
    manager = ExecutionManager(ExecutionConfig(tp1_fraction=0.5), MarketConfig(), StrategyConfig())
    trade = manager.submit_entry(_signal(), qty=100)
    exits = manager.on_fill(trade.entry_order_id, fill_qty=100, fill_price=100.0, timestamp=_now())
    tp1 = next(order for order in exits if order.role == "tp1")
    stop = next(order for order in exits if order.role == "stop")

    manager.on_fill(tp1.order_id, fill_qty=50, fill_price=101.0, timestamp=_now())
    assert trade.realized_pnl == 50.0
    assert trade.status == TradeStatus.OPEN

    manager.on_fill(stop.order_id, fill_qty=50, fill_price=100.0, timestamp=_now())
    assert trade.realized_pnl == 50.0
    assert trade.status == TradeStatus.CLOSED


def test_max_hold_exits_position():
    manager = ExecutionManager(ExecutionConfig(), MarketConfig(), StrategyConfig(max_hold_minutes=30))
    trade = manager.submit_entry(_signal(), qty=10)
    manager.on_fill(trade.entry_order_id, fill_qty=10, fill_price=100.0, timestamp=_now())
    exits = manager.flatten_expired_positions(_now() + timedelta(minutes=31))
    assert len(exits) == 1
    assert exits[0].role == "flatten"
    assert trade.status == TradeStatus.EXITING


def test_daily_loss_kill_switch_blocks_new_trades():
    risk = RiskManager(RiskConfig(account_equity=50_000, max_daily_loss_pct=0.01), StrategyConfig())
    ts = _now()
    risk.mark_trade_closed("NVDA", ts, realized_pnl=-501)
    decision = risk.approve(_signal(timestamp=ts))
    assert not decision.approved
    assert decision.reason == "daily_loss_exceeded"


def test_on_fill_with_unknown_order_id_returns_empty_list():
    manager = ExecutionManager(ExecutionConfig(), MarketConfig(), StrategyConfig())
    result = manager.on_fill("nonexistent-order", fill_qty=10, fill_price=100.0, timestamp=_now())
    assert result == []


def test_trade_for_order_uses_reverse_index():
    manager = ExecutionManager(ExecutionConfig(tp1_fraction=0.5), MarketConfig(), StrategyConfig())
    trade = manager.submit_entry(_signal(), qty=100)
    manager.on_fill(trade.entry_order_id, fill_qty=100, fill_price=100.0, timestamp=_now())
    stop_order = next(o for o in trade.orders.values() if o.role == "stop")
    found = manager._trade_for_order(stop_order.order_id)
    assert found is not None
    assert found.trade_id == trade.trade_id


def test_replay_can_load_run_folder_and_reconstruct_decisions(tmp_path):
    run = tmp_path / "runs" / "20260525_120000"
    run.mkdir(parents=True)
    with (run / "decisions.csv").open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=["timestamp", "symbol", "phase", "passed", "reason", "mid", "absorption_score", "exhaustion_score", "trigger_score"])
        writer.writeheader()
        writer.writerow({"timestamp": _now().isoformat(), "symbol": "NVDA", "phase": "IDLE", "passed": False, "reason": "waiting", "mid": 100, "absorption_score": 0.1, "exhaustion_score": 0.1, "trigger_score": 0.1})
    summary = replay_run(run)
    assert summary["decisions"] == 1
    assert (run / "replay_timeline.csv").exists()


def _now():
    return datetime(2026, 5, 25, 14, 0, tzinfo=timezone.utc)


def _signal(timestamp=None, entry=100.00, stop=99.50, target1=100.50, target2=101.00):
    timestamp = timestamp or _now()
    return Signal(
        symbol="NVDA",
        timestamp=timestamp,
        phase="TRIGGER",
        entry_ref_price=entry,
        absorption_level=99.90,
        stop_price=stop,
        target1_price=target1,
        target2_price=target2,
        confidence=0.8,
        reason_codes=["test"],
        feature_snapshot={"spread_bps": 1.0},
    )
