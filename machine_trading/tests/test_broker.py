from datetime import datetime, timezone

from multi_strategy.broker import SharedBroker
from multi_strategy.registry import PositionRegistry


class _Logger:
    def __init__(self):
        self.events = []
        self.rows = []

    def event(self, event_type, payload):
        self.events.append((event_type, payload))

    def csv(self, name, row):
        self.rows.append((name, row))


class _Receiver:
    strategy_name = "absorption"

    def __init__(self):
        self.fills = []

    def on_broker_fill(self, order_id, timestamp, quantity, price, commission=0.0):
        self.fills.append((order_id, quantity, price))


class _Order:
    def __init__(self, action, quantity, *, order_type="LMT", limit_price=100.0, stop_price=0.0):
        self.action = action
        self.totalQuantity = quantity
        self.orderType = order_type
        self.lmtPrice = limit_price
        self.auxPrice = stop_price
        self.orderId = None


class _Execution:
    def __init__(self, shares, price=100.0):
        self.time = datetime(2026, 5, 27, 14, 30, tzinfo=timezone.utc)
        self.shares = shares
        self.price = price


class _Fill:
    def __init__(self, shares, price=100.0):
        self.execution = _Execution(shares, price)
        self.commissionReport = None


class _FillEvent:
    def __iadd__(self, handler):
        self.handler = handler
        return self


class _Trade:
    def __init__(self, order):
        self.order = order
        self.fillEvent = _FillEvent()


class _Contract:
    def __init__(self, symbol):
        self.symbol = symbol


class _Position:
    def __init__(self, symbol, quantity, avg_cost):
        self.contract = _Contract(symbol)
        self.position = quantity
        self.avgCost = avg_cost


class _IB:
    def __init__(self):
        self.orders = []
        self.trade_list = []
        self.modifications = []
        self.cancelled = []
        self.account_positions = []

    def placeOrder(self, contract, order):
        if order.orderId is not None:
            self.modifications.append(order)
            return next(trade for trade in self.trade_list if trade.order is order)
        order.orderId = len(self.orders) + 1
        self.orders.append(order)
        trade = _Trade(order)
        self.trade_list.append(trade)
        return trade

    def trades(self):
        return self.trade_list

    def cancelOrder(self, order):
        self.cancelled.append(order)

    def positions(self):
        return self.account_positions

    def reqPositions(self):
        return self.account_positions


def test_shared_broker_reduces_exits_so_sells_cannot_exceed_long_inventory():
    ib = _IB()
    logger = _Logger()
    receiver = _Receiver()
    broker = SharedBroker(ib, {"TSLA": object()}, PositionRegistry(), logger)

    broker._place(receiver, "TSLA", "entry-1", "entry", _Order("BUY", 56))
    broker._on_fill("entry-1", _Fill(56, 426.72))

    broker._place(receiver, "TSLA", "tp1-2", "tp1", _Order("SELL", 20))
    broker._place(receiver, "TSLA", "tp2-3", "tp2", _Order("SELL", 40))

    assert ib.orders[-2].totalQuantity == 20
    assert ib.orders[-1].totalQuantity == 36
    assert broker.exit_reservations[("absorption", "TSLA")] == 56
    assert logger.events[-1][0] == "sell_order_reduced_to_long_inventory"
    assert logger.events[-1][1]["requested_qty"] == 40
    assert logger.events[-1][1]["accepted_qty"] == 36


def test_shared_broker_rejects_exit_when_no_long_inventory_is_available():
    ib = _IB()
    logger = _Logger()
    receiver = _Receiver()
    broker = SharedBroker(ib, {"TSLA": object()}, PositionRegistry(), logger)

    broker._place(receiver, "TSLA", "stop-1", "stop", _Order("SELL", 1))

    assert ib.orders == []
    # Long-only guard fires before _reserve_exit_quantity, so the event name
    # is sell_rejected_no_long_position rather than sell_order_rejected_no_long_inventory.
    assert len(logger.events) == 1
    assert logger.events[0][0] == "sell_rejected_no_long_position"
    assert logger.events[0][1]["symbol"] == "TSLA"
    assert logger.events[0][1]["role"] == "stop"


def test_shared_broker_allows_full_stop_alongside_reserved_targets():
    ib = _IB()
    logger = _Logger()
    receiver = _Receiver()
    broker = SharedBroker(ib, {"NVDA": object()}, PositionRegistry(), logger)

    broker._place(receiver, "NVDA", "entry-1", "entry", _Order("BUY", 117))
    broker._on_fill("entry-1", _Fill(117, 213.64))
    broker._place(receiver, "NVDA", "tp1-3", "tp1", _Order("SELL", 58))
    broker._place(receiver, "NVDA", "tp2-4", "tp2", _Order("SELL", 59))
    broker._place(receiver, "NVDA", "stop-5", "stop", _Order("SELL", 117))

    assert [order.totalQuantity for order in ib.orders[-3:]] == [58, 59, 117]
    assert all(order.ocaGroup == "absorption-NVDA-exit" for order in ib.orders[-3:])
    assert all(order.ocaType == 2 for order in ib.orders[-3:])
    assert broker.exit_reservations[("absorption", "NVDA")] == 117
    assert not any(event[0] == "sell_order_rejected_no_long_inventory" for event in logger.events)


def test_shared_broker_clears_exit_reservations_when_position_goes_flat():
    ib = _IB()
    logger = _Logger()
    receiver = _Receiver()
    broker = SharedBroker(ib, {"NVDA": object()}, PositionRegistry(), logger)

    broker._place(receiver, "NVDA", "entry-1", "entry", _Order("BUY", 100, limit_price=200.0))
    broker._on_fill("entry-1", _Fill(100, 200.0))
    broker._place(receiver, "NVDA", "tp1-3", "tp1", _Order("SELL", 50, limit_price=201.0))
    broker._place(receiver, "NVDA", "tp2-4", "tp2", _Order("SELL", 50, limit_price=202.0))
    broker._place(receiver, "NVDA", "stop-5", "stop", _Order("SELL", 100, order_type="STP", stop_price=198.5))

    broker._on_fill("stop-5", _Fill(100, 198.4))

    assert ("absorption", "NVDA") not in broker.exit_reservations
    assert "tp1-3" not in broker.exit_reservations_by_ref
    assert "tp2-4" not in broker.exit_reservations_by_ref


def test_shared_broker_flattens_when_price_breaches_stop_but_position_remains():
    ib = _IB()
    logger = _Logger()
    receiver = _Receiver()
    broker = SharedBroker(ib, {"NVDA": object()}, PositionRegistry(), logger)
    broker._market_order = lambda side, qty: _Order(side, qty, order_type="MKT")

    broker._place(receiver, "NVDA", "entry-1", "entry", _Order("BUY", 117, limit_price=213.58))
    broker._on_fill("entry-1", _Fill(117, 213.58))
    broker._place(receiver, "NVDA", "stop-5", "stop", _Order("SELL", 117, order_type="STP", stop_price=213.165))

    broker.enforce_stop_breaches("NVDA", 212.14, datetime(2026, 6, 5, 14, 0, tzinfo=timezone.utc))

    assert ib.orders[-1].action == "SELL"
    assert ib.orders[-1].orderType == "MKT"
    assert ib.orders[-1].totalQuantity == 117
    assert ib.cancelled == [ib.orders[1]]
    assert any(event[0] == "stop_breach_flatten_submitted" for event in logger.events)
    assert any(event[0] == "forced_flatten_submitted" and event[1]["reason"] == "stop_breach" for event in logger.events)


def test_shared_broker_flattens_unmanaged_account_long_below_average_price():
    ib = _IB()
    ib.account_positions = [_Position("NVDA", 117, 213.58855)]
    logger = _Logger()
    broker = SharedBroker(ib, {"NVDA": object()}, PositionRegistry(), logger)
    broker._market_order = lambda side, qty: _Order(side, qty, order_type="MKT")
    now = datetime(2026, 6, 5, 14, 6, tzinfo=timezone.utc)

    broker.sync_account_positions(now)
    broker.enforce_stop_breaches("NVDA", 212.14, now)

    assert ib.orders[-1].action == "SELL"
    assert ib.orders[-1].orderType == "MKT"
    assert ib.orders[-1].totalQuantity == 117
    assert any(event[0] == "unmanaged_account_position_flatten_submitted" for event in logger.events)
    assert any(event[0] == "forced_flatten_submitted" and event[1]["reason"] == "unmanaged_account_position_loss" for event in logger.events)
    assert broker.is_symbol_cooling_down("NVDA", now) is True
    assert broker.is_symbol_cooling_down("NVDA", datetime(2026, 6, 5, 15, 7, tzinfo=timezone.utc)) is False
    assert any(event[0] == "symbol_cooldown_started" for event in logger.events)


def test_shared_broker_logs_trading_actions_with_plan_prices():
    ib = _IB()
    logger = _Logger()
    receiver = _Receiver()
    broker = SharedBroker(ib, {"NVDA": object()}, PositionRegistry(), logger)

    broker._place(receiver, "NVDA", "entry-1", "entry", _Order("BUY", 100, limit_price=200.0))
    broker._on_fill("entry-1", _Fill(100, 200.0))
    broker._place(receiver, "NVDA", "tp1-3", "tp1", _Order("SELL", 50, limit_price=201.0))
    broker._place(receiver, "NVDA", "tp2-4", "tp2", _Order("SELL", 50, limit_price=202.0))
    broker._place(receiver, "NVDA", "stop-5", "stop", _Order("SELL", 100, order_type="STP", stop_price=198.5))

    action_rows = [row for name, row in logger.rows if name == "trading_actions"]

    assert action_rows[0]["stock"] == "NVDA"
    assert action_rows[0]["buy/sell"] == "BUY"
    assert action_rows[0]["price"] == 200.0
    assert action_rows[0]["filled_status"] == "no-fill"
    assert action_rows[-1]["tp1"] == 201.0
    assert action_rows[-1]["tp2"] == 202.0
    assert action_rows[-1]["stop"] == 198.5


def test_shared_broker_logs_partial_and_full_fill_statuses():
    ib = _IB()
    logger = _Logger()
    receiver = _Receiver()
    broker = SharedBroker(ib, {"TSLA": object()}, PositionRegistry(), logger)

    broker._place(receiver, "TSLA", "entry-1", "entry", _Order("BUY", 100, limit_price=420.0))
    broker._on_fill("entry-1", _Fill(40, 420.0))
    broker._on_fill("entry-1", _Fill(60, 420.0))

    entry_rows = [row for name, row in logger.rows if name == "trading_actions" and row["order_id"] == "entry-1"]

    assert [row["filled_status"] for row in entry_rows] == ["no-fill", "partial fill", "full fill"]
    assert [row["filled_quantity"] for row in entry_rows] == [0, 40, 100]


def test_shared_broker_rejects_new_entry_while_long_inventory_remains():
    ib = _IB()
    logger = _Logger()
    receiver = _Receiver()
    broker = SharedBroker(ib, {"NVDA": object()}, PositionRegistry(), logger)

    broker._place(receiver, "NVDA", "entry-1", "entry", _Order("BUY", 117))
    broker._on_fill("entry-1", _Fill(117, 213.64))
    broker._place(receiver, "NVDA", "entry-2", "entry", _Order("BUY", 116))

    assert [order.action for order in ib.orders] == ["BUY"]
    assert logger.events == [
        (
            "buy_order_rejected_existing_long_inventory",
            {
                "strategy": "absorption",
                "symbol": "NVDA",
                "order_id": "entry-2",
                "role": "entry",
                "long_qty": 117,
            },
        )
    ]


def test_shared_broker_trails_stop_after_position_moves_in_favor():
    ib = _IB()
    logger = _Logger()
    receiver = _Receiver()
    broker = SharedBroker(
        ib,
        {"NVDA": object()},
        PositionRegistry(),
        logger,
        trailing_activation_bps=50,
        trailing_distance_bps=35,
        trailing_min_step_bps=5,
    )

    broker._place(receiver, "NVDA", "entry-1", "entry", _Order("BUY", 110, limit_price=226.59))
    broker._on_fill("entry-1", _Fill(110, 226.59))
    broker._place(receiver, "NVDA", "stop-5", "stop", _Order("SELL", 110, order_type="STP", stop_price=225.745))

    broker.update_trailing_stops("NVDA", 232.30, datetime(2026, 6, 2, 14, 0, tzinfo=timezone.utc))

    assert ib.orders[-1].auxPrice == 231.487
    assert ib.modifications == [ib.orders[-1]]
    assert logger.events[-1][0] == "trailing_stop_updated"
    assert logger.events[-1][1]["old_stop"] == 225.745
    assert logger.events[-1][1]["new_stop"] == 231.487


def test_shared_broker_never_lowers_trailing_stop():
    ib = _IB()
    logger = _Logger()
    receiver = _Receiver()
    broker = SharedBroker(ib, {"NVDA": object()}, PositionRegistry(), logger)

    broker._place(receiver, "NVDA", "entry-1", "entry", _Order("BUY", 110, limit_price=226.59))
    broker._on_fill("entry-1", _Fill(110, 226.59))
    broker._place(receiver, "NVDA", "stop-5", "stop", _Order("SELL", 110, order_type="STP", stop_price=231.0))

    broker.update_trailing_stops("NVDA", 230.0, datetime(2026, 6, 2, 14, 0, tzinfo=timezone.utc))

    assert ib.orders[-1].auxPrice == 231.0
    assert ib.modifications == []


def test_shared_broker_promotes_lower_target_when_far_target_fills_first():
    ib = _IB()
    logger = _Logger()
    receiver = _Receiver()
    broker = SharedBroker(
        ib,
        {"NVDA": object()},
        PositionRegistry(),
        logger,
        runner_target_r_multiple=6,
    )

    broker._place(receiver, "NVDA", "entry-1", "entry", _Order("BUY", 110, limit_price=226.59))
    broker._on_fill("entry-1", _Fill(110, 226.59))
    broker._place(receiver, "NVDA", "tp1-3", "tp1", _Order("SELL", 55, limit_price=227.415))
    broker._place(receiver, "NVDA", "tp2-4", "tp2", _Order("SELL", 55, limit_price=228.25))
    broker._place(receiver, "NVDA", "stop-5", "stop", _Order("SELL", 110, order_type="STP", stop_price=225.745))

    broker._on_fill("tp2-4", _Fill(55, 228.25))

    tp1 = ib.orders[1]
    stop = ib.orders[3]
    assert tp1.lmtPrice == 231.66
    assert tp1.totalQuantity == 55
    assert stop.totalQuantity == 55
    assert tp1 in ib.modifications
    assert stop in ib.modifications
    assert any(event[0] == "runner_target_promoted" for event in logger.events)
    assert any(event[0] == "stop_quantity_updated" for event in logger.events)


def test_shared_broker_marketizes_remaining_tp1_after_partial_fill():
    ib = _IB()
    logger = _Logger()
    receiver = _Receiver()
    broker = SharedBroker(ib, {"NVDA": object()}, PositionRegistry(), logger)

    broker._place(receiver, "NVDA", "entry-1", "entry", _Order("BUY", 100, limit_price=200.0))
    broker._on_fill("entry-1", _Fill(100, 200.0))
    broker._place(receiver, "NVDA", "tp1-3", "tp1", _Order("SELL", 50, limit_price=201.5))
    broker._place(receiver, "NVDA", "tp2-4", "tp2", _Order("SELL", 50, limit_price=203.0))
    broker._place(receiver, "NVDA", "stop-5", "stop", _Order("SELL", 100, order_type="STP", stop_price=198.5))

    broker._on_fill("tp1-3", _Fill(10, 201.5))

    tp1 = ib.orders[1]
    stop = ib.orders[3]
    assert broker.long_positions[("absorption", "NVDA")] == 90
    assert broker.exit_reservations_by_ref["tp1-3"] == 40
    assert broker.exit_reservations[("absorption", "NVDA")] == 90
    assert tp1.orderType == "MKT"
    assert tp1.lmtPrice == 0.0
    assert stop.totalQuantity == 90
    assert tp1 in ib.modifications
    assert stop in ib.modifications
    assert any(event[0] == "partial_target_marketized" for event in logger.events)


def test_shared_broker_flatten_cancels_reserved_exits_before_market_sell():
    ib = _IB()
    logger = _Logger()
    receiver = _Receiver()
    broker = SharedBroker(ib, {"NVDA": object()}, PositionRegistry(), logger)

    broker._place(receiver, "NVDA", "entry-1", "entry", _Order("BUY", 100, limit_price=200.0))
    broker._on_fill("entry-1", _Fill(100, 200.0))
    broker._place(receiver, "NVDA", "tp1-3", "tp1", _Order("SELL", 50, limit_price=201.0))
    broker._place(receiver, "NVDA", "tp2-4", "tp2", _Order("SELL", 50, limit_price=202.0))
    broker._place(receiver, "NVDA", "stop-5", "stop", _Order("SELL", 100, order_type="STP", stop_price=198.5))

    broker._place(receiver, "NVDA", "flatten-6", "flatten", _Order("SELL", 100, order_type="MKT"))

    assert broker.exit_reservations[("absorption", "NVDA")] == 0
    assert ib.orders[-1].orderType == "MKT"
    assert ib.orders[-1].totalQuantity == 100
    assert ib.cancelled == ib.orders[1:4]
    assert not any(event[0] == "sell_order_rejected_no_long_inventory" for event in logger.events)


def test_shared_broker_forced_flatten_all_positions():
    ib = _IB()
    logger = _Logger()
    receiver = _Receiver()
    broker = SharedBroker(ib, {"NVDA": object()}, PositionRegistry(), logger)
    broker._market_order = lambda side, qty: _Order(side, qty, order_type="MKT")

    broker._place(receiver, "NVDA", "entry-1", "entry", _Order("BUY", 100, limit_price=200.0))
    broker._on_fill("entry-1", _Fill(100, 200.0))
    broker._place(receiver, "NVDA", "tp1-3", "tp1", _Order("SELL", 50, limit_price=201.0))
    broker._place(receiver, "NVDA", "tp2-4", "tp2", _Order("SELL", 50, limit_price=202.0))
    broker._place(receiver, "NVDA", "stop-5", "stop", _Order("SELL", 100, order_type="STP", stop_price=198.5))

    broker.flatten_all_positions(datetime(2026, 6, 2, 14, 59, tzinfo=timezone.utc))

    assert ib.orders[-1].action == "SELL"
    assert ib.orders[-1].orderType == "MKT"
    assert ib.orders[-1].totalQuantity == 100
    assert ib.cancelled == ib.orders[1:4]
    assert any(event[0] == "forced_flatten_submitted" for event in logger.events)


def test_shared_broker_syncs_existing_account_positions_and_locks_symbols():
    ib = _IB()
    ib.account_positions = [_Position("NVDA", 55, 226.59), _Position("TSLA", 20, 414.71), _Position("MSFT", 10, 500.0)]
    logger = _Logger()
    registry = PositionRegistry()
    broker = SharedBroker(ib, {"NVDA": object(), "TSLA": object()}, registry, logger)

    broker.sync_account_positions(datetime(2026, 6, 2, 13, 20, tzinfo=timezone.utc))

    assert broker.long_positions[("account", "NVDA")] == 55
    assert broker.long_positions[("account", "TSLA")] == 20
    assert broker.long_positions[("account", "MSFT")] == 10
    assert registry.owner("NVDA") == "account"
    assert registry.owner("TSLA") == "account"
    assert registry.owner("MSFT") == "account"
    assert any(event[0] == "account_positions_synced" for event in logger.events)
    assert logger.events[-1][0] == "account_positions_snapshot"


def test_shared_broker_forced_flatten_account_synced_positions():
    ib = _IB()
    ib.account_positions = [_Position("NVDA", 55, 226.59), _Position("TSLA", 20, 414.71)]
    logger = _Logger()
    broker = SharedBroker(ib, {"NVDA": object(), "TSLA": object()}, PositionRegistry(), logger)
    broker._market_order = lambda side, qty: _Order(side, qty, order_type="MKT")
    broker.sync_account_positions(datetime(2026, 6, 2, 13, 20, tzinfo=timezone.utc))

    broker.flatten_all_positions(datetime(2026, 6, 2, 14, 59, tzinfo=timezone.utc))

    assert [(order.action, order.totalQuantity, order.orderType) for order in ib.orders] == [("SELL", 55, "MKT"), ("SELL", 20, "MKT")]
    assert [event[0] for event in logger.events if event[0] == "forced_flatten_submitted"] == ["forced_flatten_submitted", "forced_flatten_submitted"]


def test_shared_broker_forced_flatten_account_short_position():
    ib = _IB()
    ib.account_positions = [_Position("NVDA", -4, 226.59)]
    logger = _Logger()
    broker = SharedBroker(ib, {"NVDA": object()}, PositionRegistry(), logger)
    broker._market_order = lambda side, qty: _Order(side, qty, order_type="MKT")

    broker.sync_account_positions(datetime(2026, 6, 2, 15, 7, tzinfo=timezone.utc))
    broker.flatten_all_positions(datetime(2026, 6, 2, 15, 7, tzinfo=timezone.utc))

    assert broker.short_positions[("account", "NVDA")] == 4
    assert [(order.action, order.totalQuantity, order.orderType) for order in ib.orders] == [("BUY", 4, "MKT")]
    assert logger.events[-1][1]["side"] == "BUY_TO_COVER"


def test_shared_broker_flattens_account_position_outside_configured_contracts():
    ib = _IB()
    ib.account_positions = [_Position("AMD", -25, 495.91)]
    logger = _Logger()
    broker = SharedBroker(ib, {"NVDA": object()}, PositionRegistry(), logger)
    broker._market_order = lambda side, qty: _Order(side, qty, order_type="MKT")

    broker.sync_account_positions(datetime(2026, 6, 2, 15, 12, tzinfo=timezone.utc))
    broker.flatten_all_positions(datetime(2026, 6, 2, 15, 12, tzinfo=timezone.utc))

    assert broker.short_positions[("account", "AMD")] == 25
    assert [(order.action, order.totalQuantity, order.orderType) for order in ib.orders] == [("BUY", 25, "MKT")]


def test_shared_broker_clears_account_short_after_cover_fill():
    ib = _IB()
    ib.account_positions = [_Position("NVDA", -4, 226.59)]
    logger = _Logger()
    registry = PositionRegistry()
    broker = SharedBroker(ib, {"NVDA": object()}, registry, logger)
    broker._market_order = lambda side, qty: _Order(side, qty, order_type="MKT")
    now = datetime(2026, 6, 2, 15, 7, tzinfo=timezone.utc)
    broker.sync_account_positions(now)
    broker.flatten_all_positions(now)

    broker._on_fill("flatten-account-NVDA-20260602150700", _Fill(4, 226.6))

    assert ("account", "NVDA") not in broker.short_positions
    assert registry.owner("NVDA") is None


def test_shared_broker_retries_pending_forced_flatten_for_remaining_position():
    ib = _IB()
    ib.account_positions = [_Position("TSLA", 121, 424.76)]
    logger = _Logger()
    broker = SharedBroker(ib, {"TSLA": object()}, PositionRegistry(), logger)
    broker._market_order = lambda side, qty: _Order(side, qty, order_type="MKT")
    first = datetime(2026, 6, 2, 14, 59, tzinfo=timezone.utc)
    second = datetime(2026, 6, 2, 15, 0, tzinfo=timezone.utc)
    broker.sync_account_positions(first)
    broker.flatten_all_positions(first)
    broker.long_positions[("account", "TSLA")] = 20

    broker.flatten_all_positions(second)

    assert len(ib.orders) == 1
    assert ib.orders[0].totalQuantity == 20
    assert ib.modifications == [ib.orders[0]]
    assert any(event[0] == "forced_flatten_retry" for event in logger.events)


def test_shared_broker_sync_clears_account_position_when_ib_no_longer_reports_it():
    ib = _IB()
    ib.account_positions = [_Position("AMD", 25, 495.91)]
    logger = _Logger()
    registry = PositionRegistry()
    broker = SharedBroker(ib, {"AMD": object()}, registry, logger)
    now = datetime(2026, 6, 2, 14, 59, tzinfo=timezone.utc)
    broker.sync_account_positions(now)
    ib.account_positions = []

    broker.sync_account_positions(now)

    assert ("account", "AMD") not in broker.long_positions
    assert registry.owner("AMD") is None


def test_shared_broker_trailing_stop_does_not_activate_below_threshold():
    ib = _IB()
    logger = _Logger()
    receiver = _Receiver()
    broker = SharedBroker(
        ib,
        {"NVDA": object()},
        PositionRegistry(),
        logger,
        trailing_activation_bps=50,
    )

    broker._place(receiver, "NVDA", "entry-1", "entry", _Order("BUY", 100, limit_price=200.0))
    broker._on_fill("entry-1", _Fill(100, 200.0))
    broker._place(receiver, "NVDA", "stop-5", "stop", _Order("SELL", 100, order_type="STP", stop_price=198.0))

    # 0.5% activation threshold requires price >= 201.0; 200.90 is not enough
    broker.update_trailing_stops("NVDA", 200.90, datetime(2026, 6, 2, 14, 0, tzinfo=timezone.utc))

    assert ib.modifications == []
    assert not any(event[0] == "trailing_stop_updated" for event in logger.events)


def test_shared_broker_no_cooldown_for_trading_window_close_flatten():
    ib = _IB()
    logger = _Logger()
    receiver = _Receiver()
    broker = SharedBroker(ib, {"NVDA": object()}, PositionRegistry(), logger)
    broker._market_order = lambda side, qty: _Order(side, qty, order_type="MKT")
    now = datetime(2026, 6, 2, 14, 59, tzinfo=timezone.utc)

    broker._place(receiver, "NVDA", "entry-1", "entry", _Order("BUY", 100, limit_price=200.0))
    broker._on_fill("entry-1", _Fill(100, 200.0))
    broker.flatten_all_positions(now, reason="trading_window_close")

    assert broker.is_symbol_cooling_down("NVDA", now) is False
    assert not any(event[0] == "symbol_cooldown_started" for event in logger.events)


def test_shared_broker_promotes_target_to_fill_plus_risk_when_runner_already_passed():
    ib = _IB()
    logger = _Logger()
    receiver = _Receiver()
    broker = SharedBroker(
        ib,
        {"NVDA": object()},
        PositionRegistry(),
        logger,
        runner_target_r_multiple=2,  # entry 200, stop 198, risk 2 → runner at 204
    )

    broker._place(receiver, "NVDA", "entry-1", "entry", _Order("BUY", 110, limit_price=200.0))
    broker._on_fill("entry-1", _Fill(110, 200.0))
    broker._place(receiver, "NVDA", "tp1-3", "tp1", _Order("SELL", 55, limit_price=201.0))
    broker._place(receiver, "NVDA", "tp2-4", "tp2", _Order("SELL", 55, limit_price=202.0))
    broker._place(receiver, "NVDA", "stop-5", "stop", _Order("SELL", 110, order_type="STP", stop_price=198.0))

    # tp2 fills at 210, which is already past the runner target of 204
    broker._on_fill("tp2-4", _Fill(55, 210.0))

    tp1 = ib.orders[1]
    assert tp1.lmtPrice == 212.0  # fill_price(210) + risk(2)
    assert any(event[0] == "runner_target_promoted" for event in logger.events)


def test_shared_broker_tracks_avg_price_correctly_across_partial_fills():
    ib = _IB()
    logger = _Logger()
    receiver = _Receiver()
    broker = SharedBroker(ib, {"NVDA": object()}, PositionRegistry(), logger)

    broker._place(receiver, "NVDA", "entry-1", "entry", _Order("BUY", 100, limit_price=200.0))
    broker._on_fill("entry-1", _Fill(40, 199.50))
    broker._on_fill("entry-1", _Fill(60, 200.50))

    # (199.50 * 40 + 200.50 * 60) / 100 = 200.10
    assert broker.long_avg_prices[("absorption", "NVDA")] == 200.10


def test_shared_broker_lock_or_raise_does_not_raise_when_same_strategy_holds_open_lock():
    ib = _IB()
    logger = _Logger()
    receiver = _Receiver()
    broker = SharedBroker(ib, {"NVDA": object()}, PositionRegistry(lock_on_entry_order=True), logger)

    broker._place(receiver, "NVDA", "entry-1", "entry", _Order("BUY", 100, limit_price=200.0))
    broker._on_fill("entry-1", _Fill(100, 200.0))
    # registry now holds OPEN for "absorption"; re-calling _lock_or_raise must not raise
    broker._lock_or_raise("NVDA", "absorption", datetime(2026, 6, 2, 14, 0, tzinfo=timezone.utc), "test")
