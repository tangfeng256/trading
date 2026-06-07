from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pandas as pd

from orm_ignition.main import _config_arg_dest, _config_overrides, _heartbeat_message
from orm_ignition.backtest import run_backtest
from orm_ignition.config import AppConfig, LoggingConfig, RiskConfig, StrategyConfig, apply_overrides
from orm_ignition.execution_manager import ExecutionManager, ManagedOrder, OrderStatus, Position, Side
from orm_ignition.ib_client import IBClient
from orm_ignition.ib_client import _safe_float, _safe_int
from orm_ignition.logger import AuditLogger
from orm_ignition.market_state import Bar, MarketState, Quote
from orm_ignition.replay import run_replay
from orm_ignition.risk_manager import RiskManager
from orm_ignition.signal_engine import Signal, SignalEngine


def dt(hour: int, minute: int) -> datetime:
    return datetime(2026, 5, 25, hour, minute, tzinfo=timezone.utc)


def cfg(tmp_path=None) -> AppConfig:
    logging = LoggingConfig(base_dir=str(tmp_path or Path("runs")), run_id="test_run")
    return AppConfig(
        strategy=StrategyConfig(
            symbols=["NVDA", "QQQ", "SPY"],
            min_volume=100,
            min_rel_volume=1.0,
            min_or_move_bps=10,
            max_spread_bps=20,
            min_reignite_volume_mult=1.2,
            market_negative_bps=-100,
        ),
        risk=RiskConfig(
            account_size=50_000,
            risk_per_trade_pct=0.003,
            max_daily_loss_pct=0.01,
            max_trades_per_day=3,
            max_position_notional=25_000,
            min_stop_bps=5,
            max_stop_bps=300,
            entry_stale_seconds=5,
        ),
        logging=logging,
    )


class RecordingBroker:
    def __init__(self):
        self.entries = []
        self.brackets = []
        self.cancels = []
        self.flattens = []

    def submit_entry(self, order, signal):
        self.entries.append((order, signal))

    def submit_bracket(self, symbol, stop_order, target_order):
        self.brackets.append((symbol, stop_order, target_order))

    def cancel(self, order_id):
        self.cancels.append(order_id)

    def flatten(self, order):
        self.flattens.append(order)


class FakeEvent:
    def __init__(self):
        self.handlers = []

    def __iadd__(self, handler):
        self.handlers.append(handler)
        return self


class FakeTrade:
    def __init__(self, order):
        self.order = order
        self.fillEvent = FakeEvent()


class FakeIB:
    def __init__(self):
        self.placed = []
        self.cancelled = []

    def placeOrder(self, contract, order):
        self.placed.append((contract, order))
        return FakeTrade(order)

    def cancelOrder(self, order):
        self.cancelled.append(order)


class FakeOrder:
    def __init__(self, action, quantity, price=None, **kwargs):
        self.action = action
        self.totalQuantity = quantity
        self.price = price
        self.orderRef = ""
        self.ocaGroup = ""
        self.ocaType = 0
        for key, value in kwargs.items():
            setattr(self, key, value)


class FakeLimitOrder(FakeOrder):
    pass


class FakeMarketOrder(FakeOrder):
    def __init__(self, action, quantity, **kwargs):
        super().__init__(action, quantity, **kwargs)


class FakeStopOrder(FakeOrder):
    pass


def fake_ib_client():
    client = IBClient.__new__(IBClient)
    client.LimitOrder = FakeLimitOrder
    client.MarketOrder = FakeMarketOrder
    client.StopOrder = FakeStopOrder
    client.ib = FakeIB()
    client.contracts = {"NVDA": "NVDA_CONTRACT"}
    client.trades_by_order_id = {}
    client.fill_callback = None
    return client


def test_cli_overrides_update_config_values():
    app = cfg()
    apply_overrides(
        app,
        [
            "strategy.symbols=NVDA,AMD",
            "strategy.min_volume=250000",
            "strategy.use_l2=true",
            "risk.account_size=75000",
            "logging.run_id=cli_case",
        ],
    )
    assert app.strategy.symbols == ["NVDA", "AMD"]
    assert app.strategy.min_volume == 250_000
    assert app.strategy.use_l2 is True
    assert app.risk.account_size == 75_000.0
    assert app.logging.run_id == "cli_case"


def test_direct_cli_flags_become_config_overrides():
    args = type(
        "Args",
        (),
        {
            "set": [],
            _config_arg_dest("strategy", "max_hold_minutes"): "30",
            _config_arg_dest("strategy", "min_rel_volume"): "1.2",
        },
    )()
    overrides = _config_overrides(args)
    assert "strategy.max_hold_minutes=30" in overrides
    assert "strategy.min_rel_volume=1.2" in overrides


def test_ib_numeric_helpers_ignore_nan_values():
    assert _safe_float(float("nan")) == 0.0
    assert _safe_int(float("nan")) == 0
    assert _safe_int(None) == 0
    assert _safe_int(12.0) == 12


def test_heartbeat_message_says_monitoring_and_ready():
    message = _heartbeat_message(["NVDA", "AMD"])
    assert "Heartbeat: monitoring NVDA, AMD; ready to trade." in message


def seed_state(app: AppConfig, weak_volume: bool = False, below_vwap: bool = False, wide_spread: bool = False):
    market = MarketState(app.strategy.symbols, app.strategy.or_start, app.strategy.or_end)
    symbol = "NVDA"
    price = 100.0
    for i in range(15):
        bar = Bar(symbol, dt(13, 30 + i), price, price + 0.1 + i * 0.01, price - 0.1, price + 0.05, 500)
        market.on_bar(bar)
    market.on_bar(Bar("QQQ", dt(13, 46), 400, 401, 399.5, 400.5, 1000))
    market.on_bar(Bar("SPY", dt(13, 46), 500, 501, 499.5, 500.5, 1000))
    # pullback and re-ignition sequence
    volumes = [900, 500, 450, 400, 450, 1300 if not weak_volume else 300]
    closes = [101.0, 100.9, 100.8, 100.95, 101.05, 101.45]
    if below_vwap:
        closes[-1] = 99.0
    for j, (close, volume) in enumerate(zip(closes, volumes)):
        market.on_bar(Bar(symbol, dt(13, 45 + j), close - 0.2, close + 0.05, close - 0.25, close, volume))
    spread = 0.5 if wide_spread else 0.02
    last = market.state(symbol).last_bar.close
    market.on_quote(Quote(symbol, dt(13, 51), last - spread / 2, last + spread / 2))
    return market


def test_no_signal_before_opening_range_completes():
    app = cfg()
    market = MarketState(app.strategy.symbols, app.strategy.or_start, app.strategy.or_end)
    market.on_bar(Bar("NVDA", dt(13, 40), 100, 101, 99.5, 100.5, 1000))
    market.on_quote(Quote("NVDA", dt(13, 40), 100.49, 100.51))
    signal, decision = SignalEngine(app.strategy).evaluate(market.state("NVDA"), list(market.symbols.values()))
    assert signal is None
    assert decision["reason"] == "opening_range_incomplete"


def test_no_long_if_price_below_vwap():
    app = cfg()
    market = seed_state(app, below_vwap=True)
    signal, decision = SignalEngine(app.strategy).evaluate(market.state("NVDA"), list(market.symbols.values()))
    assert signal is None
    assert "below_vwap" in decision["reason"]


def test_no_long_if_breakout_occurs_on_weak_volume():
    app = cfg()
    market = seed_state(app, weak_volume=True)
    signal, decision = SignalEngine(app.strategy).evaluate(market.state("NVDA"), list(market.symbols.values()))
    assert signal is None
    assert "weak_reignite_volume" in decision["reason"] or decision["reason"] == "relative_volume_too_low"


def test_no_long_if_spread_too_wide():
    app = cfg()
    market = seed_state(app, wide_spread=True)
    signal, decision = SignalEngine(app.strategy).evaluate(market.state("NVDA"), list(market.symbols.values()))
    assert signal is None
    assert decision["reason"] == "spread_too_wide"


def test_position_size_respects_max_risk_dollars():
    app = cfg()
    signal = Signal("NVDA", dt(14, 0), 100.0, 99.0, 102.0, 0.9, [], {})
    decision = RiskManager(app.risk).approve(signal, Quote("NVDA", dt(14, 0), 99.99, 100.01), 0)
    assert decision.approved
    assert decision.quantity <= int(app.risk.risk_dollars / 1.0)


def test_stop_must_be_below_entry():
    app = cfg()
    signal = Signal("NVDA", dt(14, 0), 100.0, 100.1, 102.0, 0.9, [], {})
    decision = RiskManager(app.risk).approve(signal, Quote("NVDA", dt(14, 0), 99.99, 100.01), 0)
    assert not decision.approved
    assert decision.reason == "stop_not_below_entry"


def test_entry_order_cancels_if_stale(tmp_path):
    app = cfg(tmp_path)
    logger = AuditLogger(str(tmp_path), "stale")
    execution = ExecutionManager(RiskManager(app.risk), app.risk, app.strategy, logger)
    signal = Signal("NVDA", dt(14, 0), 100, 99, 102, 0.9, [], {})
    order = execution.submit_entry(signal, type("Decision", (), {"quantity": 10})())
    execution.reconcile(dt(14, 0) + timedelta(seconds=6))
    assert execution.orders[order.order_id].status == OrderStatus.CANCELLED


def test_partial_fill_creates_protective_stop_only_for_filled_quantity(tmp_path):
    app = cfg(tmp_path)
    logger = AuditLogger(str(tmp_path), "partial")
    execution = ExecutionManager(RiskManager(app.risk), app.risk, app.strategy, logger)
    signal = Signal("NVDA", dt(14, 0), 100, 99, 102, 0.9, [], {})
    order = execution.submit_entry(signal, type("Decision", (), {"quantity": 100})())
    execution.on_fill(order.order_id, dt(14, 0), 40, 100)
    position = execution.positions["NVDA"]
    assert position.quantity == 40
    assert position.bracket_submitted_qty == 40


def test_tp1_cannot_create_duplicate_stop_target_pairs(tmp_path):
    app = cfg(tmp_path)
    logger = AuditLogger(str(tmp_path), "dupe")
    execution = ExecutionManager(RiskManager(app.risk), app.risk, app.strategy, logger)
    execution.positions["NVDA"] = Position("NVDA", quantity=50, avg_price=100)
    execution.submit_protective_bracket("NVDA", 50, 100)
    execution.submit_protective_bracket("NVDA", 50, 100)
    assert execution.positions["NVDA"].bracket_submitted_qty == 50


def test_protective_bracket_creates_broker_tracked_stop_and_target(tmp_path):
    app = cfg(tmp_path)
    broker = RecordingBroker()
    logger = AuditLogger(str(tmp_path), "broker_bracket")
    execution = ExecutionManager(RiskManager(app.risk), app.risk, app.strategy, logger, broker=broker)
    signal = Signal("NVDA", dt(14, 0), 100, 99, 102, 0.9, [], {})
    order = execution.submit_entry(signal, type("Decision", (), {"quantity": 50})())
    execution.on_fill(order.order_id, dt(14, 0), 50, 100)
    stop_order, target_order = broker.brackets[0][1:]
    assert stop_order.order_id in execution.orders
    assert target_order.order_id in execution.orders
    assert stop_order.reason == "protective_stop"
    assert target_order.reason == "profit_target"


def test_ib_client_places_entry_and_oca_exit_orders():
    client = fake_ib_client()
    entry = ManagedOrder("E000001", "NVDA", Side.BUY, 10, 101.25, dt(14, 0), reason="entry")
    stop = ManagedOrder("S000002", "NVDA", Side.SELL, 10, 99.0, dt(14, 0), reason="protective_stop")
    target = ManagedOrder("T000003", "NVDA", Side.SELL, 10, 103.0, dt(14, 0), reason="profit_target")

    client.submit_entry(entry, Signal("NVDA", dt(14, 0), 100, 99, 102, 0.9, [], {}))
    client.submit_bracket("NVDA", stop, target)

    placed_orders = [call[1] for call in client.ib.placed]
    assert placed_orders[0].orderRef == "E000001"
    assert placed_orders[0].action == "BUY"
    assert placed_orders[1].orderRef == "S000002"
    assert placed_orders[2].orderRef == "T000003"
    assert placed_orders[1].ocaGroup == placed_orders[2].ocaGroup
    assert set(client.trades_by_order_id) == {"E000001", "S000002", "T000003"}


def test_max_hold_exits_position(tmp_path):
    app = cfg(tmp_path)
    logger = AuditLogger(str(tmp_path), "hold")
    execution = ExecutionManager(RiskManager(app.risk), app.risk, app.strategy, logger)
    execution.positions["NVDA"] = Position("NVDA", quantity=10, avg_price=100, entry_time=dt(14, 0))
    execution.reconcile(dt(14, 31))
    assert any(order.reason == "max_hold" for order in execution.orders.values())


def test_daily_loss_kill_switch_prevents_new_trades():
    app = cfg()
    risk = RiskManager(app.risk)
    risk.realized_pnl = -app.risk.max_daily_loss_dollars
    assert risk.can_open_new() == (False, "daily_loss_limit")


def test_replay_can_reconstruct_all_trades_from_logs(tmp_path):
    app = cfg(tmp_path)
    run_dir = Path(tmp_path) / "replaycase"
    logger = AuditLogger(str(tmp_path), "replaycase")
    logger.bar(Bar("NVDA", dt(14, 0), 100, 101, 99, 100.5, 1000))
    logger.order(symbol="NVDA", event="submit_entry", order_id="1", side="BUY", quantity=10, price=100.5, status="FILLED", reason="entry")
    logger.fill(symbol="NVDA", order_id="1", side="BUY", quantity=10, price=100.5, commission=0)
    out = run_replay(run_dir)
    timeline = pd.read_csv(out)
    assert set(timeline["event"]) >= {"bar", "order", "fill"}
