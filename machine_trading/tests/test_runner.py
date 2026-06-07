from datetime import datetime, timezone
import json

from multi_strategy.config import AppConfig
from multi_strategy.adapters import AbsorptionAdapter, OpeningRangeAdapter, PullbackAdapter
from multi_strategy.runner import MultiStrategyRunner
from multi_strategy.registry import PositionRegistry
from multi_strategy.paths import add_strategy_paths

add_strategy_paths()

from absorption.order_state import Signal as AbsorptionSignal  # noqa: E402


class _Event:
    def __iadd__(self, handler):
        self.handler = handler
        return self


class _BarList(list):
    def __init__(self, bars):
        super().__init__(bars)
        self.updateEvent = _Event()


class _Ticker:
    def __init__(self, contract):
        self.contract = contract
        self.updateEvent = _Event()


class _IB:
    def __init__(self):
        self.market_data_requests = []
        self.depth_requests = []
        self.historical_requests = []
        self.bars = _BarList([])

    def reqMktData(self, contract, generic_tick_list, snapshot, regulatory_snapshot):
        self.market_data_requests.append((contract, generic_tick_list, snapshot, regulatory_snapshot))
        return _Ticker(contract)

    def reqMktDepth(self, contract, rows, isSmartDepth=False):
        self.depth_requests.append((contract, rows, isSmartDepth))
        return _Ticker(contract)

    def reqHistoricalData(self, contract, **kwargs):
        self.historical_requests.append((contract, kwargs))
        return self.bars


class _Order:
    def __init__(self, order_id):
        self.order_id = order_id


class _Execution:
    def cancel_stale_entries(self, now):
        return [_Order("entry-1")]

    def flatten_expired_positions(self, now):
        return [_Order("flatten-2")]


class _Broker:
    def __init__(self):
        self.cancelled = []
        self.submitted = []

    def cancel(self, order_id):
        self.cancelled.append(order_id)

    def submit_absorption_order(self, receiver, order):
        self.submitted.append(order.order_id)

    def is_symbol_cooling_down(self, symbol, timestamp):
        return False


class _Logger:
    def __init__(self):
        self.rows = []
        self.events = []

    def csv(self, name, row):
        self.rows.append((name, row))

    def event(self, event_type, payload):
        self.events.append((event_type, payload))


class _BrokerWithFlatten:
    def __init__(self):
        self.calls = []
        self.open_positions = True
        self.syncs = []

    def flatten_all_positions(self, timestamp, reason):
        self.calls.append((timestamp, reason))

    def has_open_positions(self):
        return self.open_positions

    def sync_account_positions(self, timestamp):
        self.syncs.append(timestamp)


class _Level:
    def __init__(self, price, size):
        self.price = price
        self.size = size


class _DomTick:
    def __init__(self):
        self.time = datetime(2026, 5, 26, 14, 30, tzinfo=timezone.utc)
        self.side = 1
        self.operation = 0
        self.position = 0
        self.price = 217.1
        self.size = 300
        self.marketMaker = "ISLAND"


class _DepthTicker:
    bid = None
    ask = None
    bidSize = 0
    askSize = 0
    ticks = []

    def __init__(self):
        self.domBids = [_Level(217.1, 300), _Level(217.09, 200)]
        self.domAsks = [_Level(217.12, 100), _Level(217.13, 400)]
        self.domTicks = [_DomTick()]


class _Bar:
    def __init__(self, timestamp):
        self.date = timestamp


class _Adapter:
    strategy_name = "test"

    def __init__(self):
        self.bars = []

    def on_bar(self, symbol, bar, allow_new_entries=True):
        self.bars.append((symbol, allow_new_entries))


def test_heartbeat_reports_monitoring_and_ready_to_trade():
    config = AppConfig()
    runner = MultiStrategyRunner.__new__(MultiStrategyRunner)
    runner.config = config
    runner.registry = PositionRegistry(config.runtime.lock_on_entry_order)
    runner.contracts = {"NVDA": object(), "AMD": object()}

    message = runner._heartbeat(datetime(2026, 5, 26, 14, 30, 12, tzinfo=timezone.utc))

    assert "heartbeat: monitoring 2 stocks (AMD, NVDA)" in message
    assert "status=ready to trade" in message
    assert "locks=none" in message


def test_heartbeat_reports_dry_run_status():
    config = AppConfig()
    config.runtime.dry_run = True
    runner = MultiStrategyRunner.__new__(MultiStrategyRunner)
    runner.config = config
    runner.registry = PositionRegistry(config.runtime.lock_on_entry_order)
    runner.contracts = {}

    message = runner._heartbeat(datetime(2026, 5, 26, 14, 30, 12, tzinfo=timezone.utc))

    assert "status=dry-run monitoring only" in message


def test_heartbeat_reports_passed_trading_window_after_end():
    config = AppConfig()
    runner = MultiStrategyRunner.__new__(MultiStrategyRunner)
    runner.config = config
    runner.registry = PositionRegistry(config.runtime.lock_on_entry_order)
    runner.contracts = {"NVDA": object()}

    message = runner._heartbeat(datetime(2026, 6, 2, 15, 12, tzinfo=timezone.utc))

    assert "status=passed trading window" in message


def test_trading_window_is_930_to_1100_eastern():
    config = AppConfig()
    runner = MultiStrategyRunner.__new__(MultiStrategyRunner)
    runner.config = config

    assert runner._is_trading_window(datetime(2026, 6, 2, 13, 29, 59, tzinfo=timezone.utc)) is False
    assert runner._is_trading_window(datetime(2026, 6, 2, 13, 30, tzinfo=timezone.utc)) is True
    assert runner._is_trading_window(datetime(2026, 6, 2, 14, 59, 59, tzinfo=timezone.utc)) is True
    assert runner._is_trading_window(datetime(2026, 6, 2, 15, 0, tzinfo=timezone.utc)) is False


def test_entries_stop_during_closeout_minute_before_window_end():
    config = AppConfig()
    runner = MultiStrategyRunner.__new__(MultiStrategyRunner)
    runner.config = config

    assert runner._allow_new_entries(datetime(2026, 6, 2, 14, 58, 59, tzinfo=timezone.utc)) is True
    assert runner._allow_new_entries(datetime(2026, 6, 2, 14, 59, tzinfo=timezone.utc)) is False


def test_runner_forces_flatten_once_before_window_end():
    config = AppConfig()
    runner = MultiStrategyRunner.__new__(MultiStrategyRunner)
    runner.config = config
    runner.broker = _BrokerWithFlatten()
    runner.logger = _Logger()
    runner._forced_flatten_dates = set()
    runner._last_position_check_at = None
    timestamp = datetime(2026, 6, 2, 14, 59, tzinfo=timezone.utc)

    runner._force_flatten_if_needed(timestamp)
    runner._force_flatten_if_needed(timestamp)

    assert runner.broker.calls == [(timestamp, "trading_window_close")]
    assert [event[0] for event in runner.logger.events] == ["trading_window_force_flatten"]


def test_runner_keeps_checking_positions_after_window_end():
    config = AppConfig()
    runner = MultiStrategyRunner.__new__(MultiStrategyRunner)
    runner.config = config
    runner.broker = _BrokerWithFlatten()
    runner.logger = _Logger()
    runner._forced_flatten_dates = set()
    runner._last_position_check_at = None
    first = datetime(2026, 6, 2, 15, 0, tzinfo=timezone.utc)
    second = datetime(2026, 6, 2, 15, 0, 30, tzinfo=timezone.utc)

    runner._force_flatten_if_needed(first)
    runner._force_flatten_if_needed(second)

    assert runner.broker.calls == [(first, "trading_window_close"), (second, "trading_window_close")]
    assert runner.broker.syncs == [first, second]


def test_runner_skips_post_window_check_when_no_positions_are_open():
    config = AppConfig()
    runner = MultiStrategyRunner.__new__(MultiStrategyRunner)
    runner.config = config
    runner.broker = _BrokerWithFlatten()
    runner.broker.open_positions = False
    runner.logger = _Logger()
    runner._forced_flatten_dates = set()
    runner._last_position_check_at = None

    runner._force_flatten_if_needed(datetime(2026, 6, 2, 15, 0, tzinfo=timezone.utc))

    assert runner.broker.calls == []
    assert runner.logger.events == []


def test_completed_bars_pass_trading_window_flag_to_adapters():
    config = AppConfig()
    runner = MultiStrategyRunner.__new__(MultiStrategyRunner)
    runner.config = config
    adapter = _Adapter()
    runner.adapters = [adapter]

    runner._on_completed_bar("NVDA", _Bar(datetime(2026, 6, 2, 15, 0, tzinfo=timezone.utc)))

    assert adapter.bars == [("NVDA", False)]


def test_historical_warmup_bars_do_not_allow_new_entries():
    config = AppConfig()
    runner = MultiStrategyRunner.__new__(MultiStrategyRunner)
    runner.config = config
    runner.ib = _IB()
    runner.contracts = {"NVDA": object()}
    adapter = _Adapter()
    runner.adapters = [adapter]
    historical = _Bar(datetime(2026, 6, 2, 14, 30, tzinfo=timezone.utc))
    pending = _Bar(datetime(2026, 6, 2, 14, 31, tzinfo=timezone.utc))
    runner.ib.bars = _BarList([historical, pending])

    runner._subscribe_bars()

    assert adapter.bars == [("NVDA", False)]


def test_live_completed_bar_update_uses_trading_window_flag():
    config = AppConfig()
    runner = MultiStrategyRunner.__new__(MultiStrategyRunner)
    runner.config = config
    runner.ib = _IB()
    runner.contracts = {"NVDA": object()}
    adapter = _Adapter()
    runner.adapters = [adapter]
    runner.ib.bars = _BarList([_Bar(datetime(2026, 6, 2, 14, 30, tzinfo=timezone.utc))])

    runner._subscribe_bars()
    runner.ib.bars.append(_Bar(datetime(2026, 6, 2, 14, 31, tzinfo=timezone.utc)))
    runner.ib.bars.updateEvent.handler(runner.ib.bars, True)

    assert adapter.bars == [("NVDA", True)]


def test_subscribe_market_data_requests_smart_depth_by_default():
    config = AppConfig()
    config.ib.max_depth_requests = 3
    runner = MultiStrategyRunner.__new__(MultiStrategyRunner)
    runner.config = config
    runner.ib = _IB()
    runner.contracts = {"AMD": object(), "SPY": object()}
    runner.tickers = {}
    runner.depth_tickers = {}

    runner._subscribe_market_data()

    assert len(runner.ib.market_data_requests) == 2
    assert len(runner.ib.depth_requests) == 2
    assert all(request[1] == config.ib.depth_rows for request in runner.ib.depth_requests)
    assert all(request[2] is True for request in runner.ib.depth_requests)
    assert sorted(runner.depth_tickers) == ["AMD", "SPY"]


def test_subscribe_market_data_limits_depth_requests_to_configured_symbols():
    config = AppConfig()
    config.ib.depth_symbols = ["TSLA", "TQQQ", "NVDA"]
    config.ib.max_depth_requests = 2
    runner = MultiStrategyRunner.__new__(MultiStrategyRunner)
    runner.config = config
    runner.ib = _IB()
    runner.contracts = {"AMD": object(), "NVDA": object(), "TQQQ": object(), "TSLA": object()}
    runner.tickers = {}
    runner.depth_tickers = {}

    runner._subscribe_market_data()

    assert len(runner.ib.market_data_requests) == 4
    assert len(runner.ib.depth_requests) == 2
    assert list(runner.depth_tickers) == ["TQQQ", "TSLA"]


def test_configured_symbols_preserves_runtime_priority_before_strategy_markets():
    config = AppConfig()
    config.runtime.symbols = ["TSLA", "TQQQ", "NVDA", "TSLA"]
    runner = MultiStrategyRunner.__new__(MultiStrategyRunner)
    runner.config = config
    runner.config.strategy_files.pullback = "missing"
    runner.config.strategy_files.opening_range = "missing"

    assert runner._configured_symbols() == ["TSLA", "TQQQ", "NVDA"]


def test_absorption_poll_cancels_stale_entries_instead_of_resubmitting():
    adapter = AbsorptionAdapter.__new__(AbsorptionAdapter)
    adapter.execution = _Execution()
    adapter.broker = _Broker()
    adapter.config = type("Config", (), {"symbols": []})()
    adapter.registry = None
    adapter.last_feature_at = {}

    adapter.poll(datetime(2026, 5, 26, 15, 0, tzinfo=timezone.utc))

    assert adapter.broker.cancelled == ["entry-1"]
    assert adapter.broker.submitted == ["flatten-2"]


def test_absorption_exit_plan_uses_wider_stop_and_larger_targets():
    adapter = AbsorptionAdapter.__new__(AbsorptionAdapter)
    adapter.config = type("Config", (), {"market": type("Market", (), {"tick_size": 0.01})()})()
    adapter.logger = _Logger()
    signal = AbsorptionSignal(
        symbol="NVDA",
        timestamp=datetime(2026, 6, 5, 14, 12, tzinfo=timezone.utc),
        phase="long",
        entry_ref_price=211.93,
        absorption_level=211.90,
        stop_price=211.74,
        target1_price=212.12,
        target2_price=212.31,
        confidence=0.8,
        reason_codes=[],
        feature_snapshot={},
    )

    adjusted = adapter._widen_exit_plan(signal, {"realized_volatility": 0.0001})
    risk = round(adjusted.entry_ref_price - adjusted.stop_price, 4)
    expected_distance = 211.93 * 0.0025

    assert risk >= round(211.93 * 0.0025, 4)
    assert adjusted.stop_price == 211.4002
    assert adjusted.target1_price == round(211.93 + expected_distance * 1.5, 4)
    assert adjusted.target2_price == round(211.93 + expected_distance * 3.0, 4)
    assert adapter.logger.events[-1][0] == "absorption_exit_plan_adjusted"


def test_on_ticker_logs_depth_snapshots_and_dom_ticks():
    config = AppConfig()
    runner = MultiStrategyRunner.__new__(MultiStrategyRunner)
    runner.config = config
    runner.logger = _Logger()
    runner.adapters = []
    runner._processed_ticks = {}
    runner._processed_dom_ticks = {}
    runner._last_depth = {}

    runner._on_ticker("NVDA", _DepthTicker())

    depth_rows = [row for name, row in runner.logger.rows if name == "depth_snapshots"]
    dom_rows = [row for name, row in runner.logger.rows if name == "dom_ticks"]
    assert len(depth_rows) == 1
    assert json.loads(depth_rows[0]["bids"]) == [{"price": 217.1, "size": 300}, {"price": 217.09, "size": 200}]
    assert json.loads(depth_rows[0]["asks"]) == [{"price": 217.12, "size": 100}, {"price": 217.13, "size": 400}]
    assert dom_rows == [
        {
            "timestamp": datetime(2026, 5, 26, 14, 30, tzinfo=timezone.utc),
            "symbol": "NVDA",
            "side": "bid",
            "operation": "insert",
            "position": 0,
            "price": 217.1,
            "size": 300,
            "market_maker": "ISLAND",
        }
    ]


def test_absorption_poll_skips_and_logs_when_blocked_by_another_strategy():
    adapter = AbsorptionAdapter.__new__(AbsorptionAdapter)
    adapter.execution = _Execution()
    adapter.broker = _Broker()
    adapter.logger = _Logger()
    adapter.last_feature_at = {}
    adapter.config = type("Config", (), {"symbols": ["NVDA"]})()
    registry = PositionRegistry()
    registry.lock_position("NVDA", "opening_range", datetime(2026, 6, 2, 14, 0, tzinfo=timezone.utc))
    adapter.registry = registry

    adapter.poll(datetime(2026, 6, 2, 14, 30, tzinfo=timezone.utc))

    assert any(
        event[0] == "strategy_skip_locked" and event[1]["symbol"] == "NVDA"
        for event in adapter.logger.events
    )


def test_absorption_poll_skips_silently_when_strategy_owns_lock():
    # When absorption itself holds a lock (entry in flight or position open), poll
    # should skip signal evaluation without logging strategy_skip_locked.
    adapter = AbsorptionAdapter.__new__(AbsorptionAdapter)
    adapter.execution = _Execution()
    adapter.broker = _Broker()
    adapter.logger = _Logger()
    adapter.last_feature_at = {}
    adapter.config = type("Config", (), {"symbols": ["NVDA"]})()
    registry = PositionRegistry()
    registry.lock_position("NVDA", "absorption", datetime(2026, 6, 2, 14, 0, tzinfo=timezone.utc))
    adapter.registry = registry

    # poll must not crash (features/signals not set — AttributeError = skip guard failed)
    adapter.poll(datetime(2026, 6, 2, 14, 30, tzinfo=timezone.utc))

    assert not any(row[0] == "decisions" for row in adapter.logger.rows)
    assert not any(event[0] == "strategy_skip_locked" for event in adapter.logger.events)


def test_absorption_on_broker_fill_ignores_unknown_order_id():
    adapter = AbsorptionAdapter.__new__(AbsorptionAdapter)
    adapter.execution = type("Execution", (), {"orders": {}})()

    adapter.on_broker_fill("ghost-order", datetime(2026, 6, 2, 14, 0, tzinfo=timezone.utc), 100, 200.0)


def test_pullback_on_broker_fill_ignores_unknown_order_id():
    adapter = PullbackAdapter.__new__(PullbackAdapter)
    adapter.orders = type("Orders", (), {"orders": {}})()

    adapter.on_broker_fill("ghost-order", datetime(2026, 6, 2, 14, 0, tzinfo=timezone.utc), 100, 200.0)


def test_orm_on_broker_fill_ignores_unknown_order_id():
    adapter = OpeningRangeAdapter.__new__(OpeningRangeAdapter)
    adapter.execution = type("Execution", (), {"orders": {}})()

    adapter.on_broker_fill("ghost-order", datetime(2026, 6, 2, 14, 0, tzinfo=timezone.utc), 100, 200.0)


def test_orm_on_broker_fill_does_not_unlock_while_position_still_open():
    class _Side:
        value = "SELL"

    class _Order:
        symbol = "NVDA"
        side = _Side()

    class _Position:
        is_open = True

    class _Execution:
        orders = {"exit-1": _Order()}
        positions = {"NVDA": _Position()}

        def on_fill(self, *args, **kwargs):
            pass

    adapter = OpeningRangeAdapter.__new__(OpeningRangeAdapter)
    adapter.execution = _Execution()
    registry = PositionRegistry()
    registry.lock_position("NVDA", "opening_range", datetime(2026, 6, 2, 14, 0, tzinfo=timezone.utc))
    adapter.registry = registry

    adapter.on_broker_fill("exit-1", datetime(2026, 6, 2, 14, 30, tzinfo=timezone.utc), 50, 215.0)

    assert registry.owner("NVDA") == "opening_range"


def test_configured_symbols_prints_warning_when_strategy_config_fails(capsys):
    config = AppConfig()
    config.runtime.symbols = ["NVDA"]
    runner = MultiStrategyRunner.__new__(MultiStrategyRunner)
    runner.config = config
    runner.config.strategy_files.pullback = "missing"
    runner.config.strategy_files.opening_range = "missing"

    symbols = runner._configured_symbols()
    captured = capsys.readouterr()

    assert symbols == ["NVDA"]
    assert "Warning" in captured.out


def test_on_ticker_only_logs_depth_snapshot_when_book_changes():
    config = AppConfig()
    runner = MultiStrategyRunner.__new__(MultiStrategyRunner)
    runner.config = config
    runner.logger = _Logger()
    runner.adapters = []
    runner._processed_ticks = {}
    runner._processed_dom_ticks = {}
    runner._last_depth = {}

    ticker = _DepthTicker()
    runner._on_ticker("NVDA", ticker)
    runner._on_ticker("NVDA", ticker)  # identical book — should not log again

    depth_rows = [row for name, row in runner.logger.rows if name == "depth_snapshots"]
    assert len(depth_rows) == 1


def test_on_ticker_logs_depth_snapshot_again_when_book_changes():
    config = AppConfig()
    runner = MultiStrategyRunner.__new__(MultiStrategyRunner)
    runner.config = config
    runner.logger = _Logger()
    runner.adapters = []
    runner._processed_ticks = {}
    runner._processed_dom_ticks = {}
    runner._last_depth = {}

    class _ChangedDepthTicker(_DepthTicker):
        def __init__(self):
            super().__init__()
            self.domBids = [_Level(217.15, 500)]  # different book

    runner._on_ticker("NVDA", _DepthTicker())
    runner._on_ticker("NVDA", _ChangedDepthTicker())

    depth_rows = [row for name, row in runner.logger.rows if name == "depth_snapshots"]
    assert len(depth_rows) == 2


def test_orm_on_broker_fill_unlocks_when_position_closed():
    class _Side:
        value = "SELL"

    class _Order:
        symbol = "NVDA"
        side = _Side()

    class _Position:
        is_open = False

    class _Execution:
        orders = {"exit-1": _Order()}
        positions = {"NVDA": _Position()}

        def on_fill(self, *args, **kwargs):
            pass

    adapter = OpeningRangeAdapter.__new__(OpeningRangeAdapter)
    adapter.execution = _Execution()
    registry = PositionRegistry()
    registry.lock_position("NVDA", "opening_range", datetime(2026, 6, 2, 14, 0, tzinfo=timezone.utc))
    adapter.registry = registry

    adapter.on_broker_fill("exit-1", datetime(2026, 6, 2, 14, 30, tzinfo=timezone.utc), 100, 215.0)

    assert registry.owner("NVDA") is None
