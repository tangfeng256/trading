from datetime import datetime, timedelta, timezone

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
        self.commissions = []

    def on_broker_fill(self, order_id, timestamp, quantity, price, commission=0.0):
        self.fills.append((order_id, quantity, price))

    def on_broker_commission(self, timestamp, commission):
        self.commissions.append((timestamp, commission))


class _Order:
    def __init__(self, action, quantity, *, order_type="LMT", limit_price=100.0, stop_price=0.0):
        self.action = action
        self.totalQuantity = quantity
        self.orderType = order_type
        self.lmtPrice = limit_price
        self.auxPrice = stop_price
        self.orderId = None


class _Execution:
    def __init__(self, shares, price=100.0, exec_id=None):
        self.time = datetime(2026, 5, 27, 14, 30, tzinfo=timezone.utc)
        self.shares = shares
        self.price = price
        self.execId = exec_id


class _Fill:
    def __init__(self, shares, price=100.0):
        self.execution = _Execution(shares, price)
        self.commissionReport = None


class _Event:
    def __init__(self):
        self.handlers = []

    def __iadd__(self, handler):
        self.handlers.append(handler)
        self.handler = handler
        return self

    def fire(self, *args):
        for handler in list(self.handlers):
            handler(*args)


class _Trade:
    def __init__(self, order, contract=None):
        self.order = order
        self.contract = contract
        self.fillEvent = _Event()
        self.orderStatus = type(
            "Status",
            (),
            {
                "status": "Submitted",
                "filled": 0,
                "remaining": getattr(order, "totalQuantity", 0),
                "avgFillPrice": 0.0,
                "lastFillPrice": 0.0,
                "whyHeld": "",
            },
        )()


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
        self.openOrderEvent = _Event()
        self.orderStatusEvent = _Event()
        self.execDetailsEvent = _Event()
        self.commissionReportEvent = _Event()

    def placeOrder(self, contract, order):
        if order.orderId is not None:
            self.modifications.append(order)
            return next(trade for trade in self.trade_list if trade.order is order)
        order.orderId = len(self.orders) + 1
        self.orders.append(order)
        trade = _Trade(order, contract)
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
    # OCA groups are intentionally not used; stop and targets are managed independently.
    assert not any(getattr(order, "ocaGroup", "") for order in ib.orders[-3:])
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
    broker = SharedBroker(ib, {"NVDA": object()}, PositionRegistry(), logger, software_stop_breach_enabled=True)
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


def test_shared_broker_logs_ib_order_lifecycle_events():
    ib = _IB()
    logger = _Logger()
    receiver = _Receiver()
    broker = SharedBroker(ib, {"NVDA": _Contract("NVDA")}, PositionRegistry(), logger)

    broker._place(receiver, "NVDA", "entry-1", "entry", _Order("BUY", 100, limit_price=200.0))
    trade = ib.trade_list[-1]
    trade.orderStatus.status = "Submitted"
    trade.orderStatus.remaining = 100

    ib.openOrderEvent.fire(trade)
    ib.orderStatusEvent.fire(trade)
    fill = _Fill(40, 200.0)
    fill.execution.orderId = trade.order.orderId
    fill.execution.permId = 12345
    fill.execution.execId = "exec-1"
    fill.execution.side = "BOT"
    fill.execution.avgPrice = 200.0
    fill.execution.exchange = "NASDAQ"
    fill.contract = _Contract("NVDA")
    fill.commissionReport = type("Report", (), {"execId": "exec-1", "commission": 0.25, "currency": "USD", "realizedPNL": 0.0})()

    ib.execDetailsEvent.fire(trade, fill)
    ib.commissionReportEvent.fire(fill.commissionReport)

    open_rows = [row for name, row in logger.rows if name == "broker_open_orders"]
    status_rows = [row for name, row in logger.rows if name == "broker_order_status"]
    execution_rows = [row for name, row in logger.rows if name == "broker_executions"]
    commission_rows = [row for name, row in logger.rows if name == "broker_commissions"]

    assert any(row["source"] == "place_order_return" and row["order_id"] == "entry-1" for row in open_rows)
    assert any(row["source"] == "openOrderEvent" and row["ib_order_id"] == trade.order.orderId for row in open_rows)
    assert any(row["source"] == "orderStatusEvent" and row["status"] == "Submitted" for row in status_rows)
    assert execution_rows == [
        {
            "timestamp": fill.execution.time,
            "source": "execDetailsEvent",
            "symbol": "NVDA",
            "order_id": "entry-1",
            "ib_order_id": trade.order.orderId,
            "perm_id": 12345,
            "exec_id": "exec-1",
            "side": "BOT",
            "shares": 40,
            "price": 200.0,
            "avg_price": 200.0,
            "exchange": "NASDAQ",
            "commission": 0.25,
        }
    ]
    assert commission_rows[0]["exec_id"] == "exec-1"
    assert commission_rows[0]["commission"] == 0.25


def test_shared_broker_applies_split_exec_details_when_fill_event_misses_one():
    ib = _IB()
    logger = _Logger()
    receiver = _Receiver()
    broker = SharedBroker(ib, {"NVDA": _Contract("NVDA")}, PositionRegistry(), logger)

    broker._place(receiver, "NVDA", "entry-1", "entry", _Order("BUY", 20, limit_price=207.63))
    broker._on_fill("entry-1", _Fill(20, 207.63))
    broker._place(receiver, "NVDA", "flatten-1", "flatten", _Order("SELL", 20, order_type="MKT"))

    trade = ib.trade_list[-1]
    for exec_id in ("exec-a", "exec-b"):
        fill = _Fill(10, 206.16)
        fill.execution.orderId = trade.order.orderId
        fill.execution.permId = 901713526
        fill.execution.execId = exec_id
        fill.execution.side = "SLD"
        fill.execution.avgPrice = 206.16
        fill.execution.exchange = "ARCA"
        fill.contract = _Contract("NVDA")
        ib.execDetailsEvent.fire(trade, fill)

    fill_rows = [row for name, row in logger.rows if name == "fills" and row["order_id"] == "flatten-1"]
    assert [row["quantity"] for row in fill_rows] == [10, 10]
    assert ("absorption", "NVDA") not in broker.long_positions


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

    assert ib.orders[-1].auxPrice == 231.48
    assert ib.modifications == [ib.orders[-1]]
    assert logger.events[-1][0] == "trailing_stop_updated"
    assert logger.events[-1][1]["old_stop"] == 225.745
    assert logger.events[-1][1]["new_stop"] == 231.48


def test_shared_broker_rounds_trailing_stop_to_tick_and_defers_transient_amendment_cancel():
    ib = _IB()
    logger = _Logger()
    receiver = _Receiver()
    broker = SharedBroker(ib, {"NVDA": object()}, PositionRegistry(), logger, exit_ack_timeout_seconds=3)
    broker._market_order = lambda side, qty: _Order(side, qty, order_type="MKT")

    broker._place(receiver, "NVDA", "entry-1", "entry", _Order("BUY", 100, limit_price=209.07))
    broker._on_fill("entry-1", _Fill(100, 209.07))
    broker._place(receiver, "NVDA", "stop-5", "stop", _Order("SELL", 100, order_type="STP", stop_price=208.55))
    stop_trade = ib.trade_list[-1]
    stop_trade.order.permId = 123
    amended_at = datetime(2026, 7, 13, 13, 33, tzinfo=timezone.utc)

    broker._raise_stop("absorption", "NVDA", "stop-5", 209.4145, amended_at)
    assert stop_trade.order.auxPrice == 209.41

    stop_trade.orderStatus.status = "Cancelled"
    broker._handle_exit_order_status(stop_trade, now=amended_at + timedelta(milliseconds=100))

    assert not any(event[0] == "forced_flatten_submitted" for event in logger.events)
    assert "stop-5" in broker._exit_pending_ack
    assert any(event[0] == "exit_order_amendment_status_pending" for event in logger.events)

    stop_trade.orderStatus.status = "PreSubmitted"
    broker.resubmit_unacknowledged_exits(amended_at + timedelta(seconds=3))

    assert "stop-5" not in broker._exit_pending_ack
    assert "stop-5" not in broker._exit_amendments
    assert not any(event[0] == "forced_flatten_submitted" for event in logger.events)


def test_shared_broker_routes_commission_report_to_fill_receiver_once():
    ib = _IB()
    logger = _Logger()
    receiver = _Receiver()
    broker = SharedBroker(ib, {"NVDA": object()}, PositionRegistry(), logger)
    broker._place(receiver, "NVDA", "entry-1", "entry", _Order("BUY", 10, limit_price=100.0))
    fill = _Fill(10, 100.0)
    fill.execution.execId = "exec-1"
    broker._on_fill("entry-1", fill)
    report = type("CommissionReport", (), {"execId": "exec-1", "commission": 1.25})()

    broker._on_ib_commission_report_event(report)
    broker._on_ib_commission_report_event(report)

    assert receiver.commissions == [(fill.execution.time, 1.25)]


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
    ib.orders[3].permId = 67108270

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
    ib.orders[3].permId = 67108270

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


def test_shared_broker_retires_closed_position_orders_before_next_trade():
    ib = _IB()
    logger = _Logger()
    receiver = _Receiver()
    broker = SharedBroker(ib, {"NVDA": object()}, PositionRegistry(), logger)

    broker._place(receiver, "NVDA", "entry-1", "entry", _Order("BUY", 100, limit_price=200.0))
    broker._on_fill("entry-1", _Fill(100, 200.0))
    broker._place(receiver, "NVDA", "tp1-3", "tp1", _Order("SELL", 50, limit_price=201.0))
    broker._place(receiver, "NVDA", "tp2-4", "tp2", _Order("SELL", 50, limit_price=202.0))
    broker._place(receiver, "NVDA", "stop-5", "stop", _Order("SELL", 100, order_type="STP", stop_price=198.5))
    broker._on_fill("stop-5", _Fill(100, 198.5))

    assert not any(ref in broker.tracked_by_ref for ref in ("entry-1", "tp1-3", "tp2-4", "stop-5"))
    first_cancelled = list(ib.cancelled)
    assert ib.orders[3] not in first_cancelled  # never cancel the order whose fill closed the position

    broker._place(receiver, "NVDA", "entry-6", "entry", _Order("BUY", 100, limit_price=200.0))
    broker._on_fill("entry-6", _Fill(100, 200.0))
    broker._place(receiver, "NVDA", "tp1-8", "tp1", _Order("SELL", 50, limit_price=201.0))
    broker._place(receiver, "NVDA", "stop-10", "stop", _Order("SELL", 100, order_type="STP", stop_price=198.5))
    broker._on_fill("stop-10", _Fill(100, 198.5))

    newly_cancelled = ib.cancelled[len(first_cancelled):]
    assert ib.orders[1] not in newly_cancelled
    assert ib.orders[2] not in newly_cancelled
    assert all(order in ib.orders[5:7] for order in newly_cancelled)


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


def test_shared_broker_resizes_forced_flatten_after_late_entry_fill():
    ib = _IB()
    logger = _Logger()
    receiver = _Receiver()
    broker = SharedBroker(ib, {"NVDA": object()}, PositionRegistry(), logger)
    broker._market_order = lambda side, qty: _Order(side, qty, order_type="MKT")

    broker._place(receiver, "NVDA", "entry-188", "entry", _Order("BUY", 127, limit_price=196.22))
    broker._on_fill("entry-188", _Fill(100, 196.22))
    broker.flatten_all_positions(datetime(2026, 7, 6, 14, 4, 48, tzinfo=timezone.utc), reason="trading_window_close")

    broker._on_fill("entry-188", _Fill(27, 196.22))

    flatten_order = ib.orders[1]
    assert flatten_order.action == "SELL"
    assert flatten_order.totalQuantity == 127
    assert ib.modifications == [flatten_order]
    assert any(event[0] == "late_entry_fill_after_flatten_detected" for event in logger.events)


def test_shared_broker_submits_fresh_flatten_when_late_entry_fill_arrives_after_flatten_fill():
    ib = _IB()
    logger = _Logger()
    receiver = _Receiver()
    broker = SharedBroker(ib, {"NVDA": object()}, PositionRegistry(), logger)
    broker._market_order = lambda side, qty: _Order(side, qty, order_type="MKT")

    broker._place(receiver, "NVDA", "entry-188", "entry", _Order("BUY", 127, limit_price=196.22))
    broker._on_fill("entry-188", _Fill(100, 196.22))
    broker.flatten_all_positions(datetime(2026, 7, 6, 14, 4, 48, tzinfo=timezone.utc), reason="trading_window_close")
    broker._on_fill("flatten-absorption-NVDA-20260706140448", _Fill(100, 196.19))

    broker._on_fill("entry-188", _Fill(27, 196.22))

    fresh_flatten = ib.orders[-1]
    assert fresh_flatten is not ib.orders[1]
    assert fresh_flatten.action == "SELL"
    assert fresh_flatten.orderType == "MKT"
    assert fresh_flatten.totalQuantity == 27
    assert any(event[0] == "late_entry_fill_after_flatten_detected" for event in logger.events)


def test_shared_broker_resubmits_remaining_forced_flatten_when_resize_hit_filled_order():
    ib = _IB()
    logger = _Logger()
    receiver = _Receiver()
    broker = SharedBroker(ib, {"NVDA": object()}, PositionRegistry(), logger)
    broker._market_order = lambda side, qty: _Order(side, qty, order_type="MKT")

    broker._place(receiver, "NVDA", "entry-188", "entry", _Order("BUY", 123, limit_price=203.24))
    broker._on_fill("entry-188", _Fill(100, 203.24))
    broker.flatten_all_positions(datetime(2026, 7, 9, 13, 36, 23, tzinfo=timezone.utc), reason="exit_order_ack_timeout")
    broker._on_fill("entry-188", _Fill(23, 203.24))
    broker._on_fill("flatten-absorption-NVDA-20260709133623", _Fill(100, 203.20))

    broker._on_ib_error_event(2, 104, "Cannot modify a filled order.", None)

    fresh_flatten = ib.orders[-1]
    assert fresh_flatten is not ib.orders[1]
    assert fresh_flatten.action == "SELL"
    assert fresh_flatten.orderType == "MKT"
    assert fresh_flatten.totalQuantity == 23
    assert any(event[0] == "forced_flatten_modify_filled_resubmit" for event in logger.events)


def test_shared_broker_does_not_flatten_new_entry_after_prior_flatten_watch():
    ib = _IB()
    logger = _Logger()
    receiver = _Receiver()
    broker = SharedBroker(ib, {"NVDA": object()}, PositionRegistry(), logger)
    broker._market_order = lambda side, qty: _Order(side, qty, order_type="MKT")

    broker._place(receiver, "NVDA", "entry-188", "entry", _Order("BUY", 127, limit_price=196.22))
    broker._on_fill("entry-188", _Fill(100, 196.22))
    broker.flatten_all_positions(datetime(2026, 7, 6, 14, 4, 48, tzinfo=timezone.utc), reason="trading_window_close")
    broker._on_fill("flatten-absorption-NVDA-20260706140448", _Fill(100, 196.19))

    broker._place(receiver, "NVDA", "entry-2", "entry", _Order("BUY", 10, limit_price=197.0))
    order_count = len(ib.orders)
    broker._on_fill("entry-2", _Fill(10, 197.0))

    assert len(ib.orders) == order_count
    assert broker.long_positions[("absorption", "NVDA")] == 10
    assert not any(
        event[0] == "late_entry_fill_after_flatten_detected" and event[1].get("order_id") == "entry-2"
        for event in logger.events
    )


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


def test_shared_broker_syncs_stop_quantity_after_late_entry_partial_fill():
    ib = _IB()
    logger = _Logger()
    receiver = _Receiver()
    broker = SharedBroker(ib, {"NVDA": object()}, PositionRegistry(), logger)

    broker._place(receiver, "NVDA", "entry-1", "entry", _Order("BUY", 123, limit_price=202.49))
    broker._on_fill("entry-1", _Fill(100, 202.41))
    broker._place(receiver, "NVDA", "stop-5", "stop", _Order("SELL", 100, order_type="STP", stop_price=201.97))

    broker._on_fill("entry-1", _Fill(23, 202.32))

    stop = ib.orders[1]
    assert broker.long_positions[("absorption", "NVDA")] == 123
    assert stop.totalQuantity == 100
    assert stop not in ib.modifications
    assert broker.pending_stop_quantities[("absorption", "NVDA")] == 123
    assert any(event[0] == "stop_quantity_update_pending" for event in logger.events)


def test_shared_broker_applies_pending_stop_quantity_when_stop_acknowledged():
    ib = _IB()
    logger = _Logger()
    receiver = _Receiver()
    broker = SharedBroker(ib, {"NVDA": _Contract("NVDA")}, PositionRegistry(), logger)

    broker._place(receiver, "NVDA", "entry-1", "entry", _Order("BUY", 120, limit_price=207.63))
    broker._on_fill("entry-1", _Fill(100, 207.63))
    broker._place(receiver, "NVDA", "stop-5", "stop", _Order("SELL", 100, order_type="STP", stop_price=207.10))

    broker._on_fill("entry-1", _Fill(20, 207.63))

    stop_trade = ib.trade_list[1]
    stop = stop_trade.order
    assert stop.totalQuantity == 100
    assert broker.pending_stop_quantities[("absorption", "NVDA")] == 120

    stop.permId = 67108270
    stop_trade.orderStatus.status = "PreSubmitted"
    ib.openOrderEvent.fire(stop_trade)

    assert stop.totalQuantity == 120
    assert stop in ib.modifications
    assert ("absorption", "NVDA") not in broker.pending_stop_quantities
    assert any(event[0] == "stop_quantity_updated" and event[1]["new_qty"] == 120 for event in logger.events)


def test_shared_broker_resizes_targets_after_late_entry_fill():
    ib = _IB()
    logger = _Logger()
    receiver = _Receiver()
    broker = SharedBroker(ib, {"TSLA": _Contract("TSLA")}, PositionRegistry(), logger)

    broker._place(receiver, "TSLA", "entry-1", "entry", _Order("BUY", 65, limit_price=379.01))
    broker._on_fill("entry-1", _Fill(40, 379.01))
    broker._place(receiver, "TSLA", "tp1-3", "tp1", _Order("SELL", 13, limit_price=380.42))
    broker._place(receiver, "TSLA", "tp2-4", "tp2", _Order("SELL", 27, limit_price=381.84))
    broker._place(receiver, "TSLA", "stop-5", "stop", _Order("SELL", 40, order_type="STP", stop_price=378.04))

    for trade in ib.trade_list[1:]:
        trade.order.permId = 1000 + trade.order.orderId
        trade.orderStatus.status = "PreSubmitted"

    broker._on_fill("entry-1", _Fill(25, 379.01))
    resized = [
        type("Managed", (), {"order_id": "tp1-3", "qty": 21, "filled_qty": 0})(),
        type("Managed", (), {"order_id": "tp2-4", "qty": 44, "filled_qty": 0})(),
        type("Managed", (), {"order_id": "stop-5", "qty": 65, "filled_qty": 0})(),
    ]
    broker.sync_protective_order_quantities(receiver, resized)

    tp1, tp2, stop = (trade.order for trade in ib.trade_list[1:])
    assert (tp1.totalQuantity, tp2.totalQuantity, stop.totalQuantity) == (21, 44, 65)
    assert broker.exit_reservations[("absorption", "TSLA")] == 65
    assert broker.exit_reservations_by_ref["tp1-3"] == 21
    assert broker.exit_reservations_by_ref["tp2-4"] == 44
    assert all(order in ib.modifications for order in (tp1, tp2, stop))
    assert sum(event[0] == "target_quantity_updated" for event in logger.events) == 2


def test_shared_broker_applies_pending_target_resize_after_ib_acknowledgement():
    ib = _IB()
    logger = _Logger()
    receiver = _Receiver()
    broker = SharedBroker(ib, {"TSLA": _Contract("TSLA")}, PositionRegistry(), logger)

    broker._place(receiver, "TSLA", "entry-1", "entry", _Order("BUY", 66, limit_price=377.92))
    broker._on_fill("entry-1", _Fill(50, 377.92))
    broker._place(receiver, "TSLA", "tp1-3", "tp1", _Order("SELL", 16, limit_price=379.33))
    broker._place(receiver, "TSLA", "tp2-4", "tp2", _Order("SELL", 34, limit_price=380.75))
    broker._on_fill("entry-1", _Fill(16, 377.92))

    resized = [
        type("Managed", (), {"order_id": "tp1-3", "qty": 21, "filled_qty": 0})(),
        type("Managed", (), {"order_id": "tp2-4", "qty": 45, "filled_qty": 0})(),
    ]
    broker.sync_protective_order_quantities(receiver, resized)

    tp1_trade, tp2_trade = ib.trade_list[1:]
    assert (tp1_trade.order.totalQuantity, tp2_trade.order.totalQuantity) == (16, 34)
    assert broker.pending_target_quantities == {"tp1-3": 21, "tp2-4": 45}

    for trade in (tp1_trade, tp2_trade):
        trade.order.permId = 2000 + trade.order.orderId
        trade.orderStatus.status = "PreSubmitted"
        ib.openOrderEvent.fire(trade)

    assert (tp1_trade.order.totalQuantity, tp2_trade.order.totalQuantity) == (21, 45)
    assert broker.exit_reservations[("absorption", "TSLA")] == 66
    assert broker.pending_target_quantities == {}


def test_shared_broker_flattens_remainder_when_underprotected_stop_fills():
    ib = _IB()
    logger = _Logger()
    receiver = _Receiver()
    broker = SharedBroker(ib, {"NVDA": _Contract("NVDA")}, PositionRegistry(), logger)
    broker._market_order = lambda side, qty: _Order(side, qty, order_type="MKT")

    broker._place(receiver, "NVDA", "entry-1", "entry", _Order("BUY", 120, limit_price=207.63))
    broker._on_fill("entry-1", _Fill(120, 207.63))
    broker._place(receiver, "NVDA", "stop-5", "stop", _Order("SELL", 100, order_type="STP", stop_price=207.10))

    broker._on_fill("stop-5", _Fill(80, 207.08))
    assert not any(event[0] == "stop_quantity_updated" for event in logger.events)

    broker._on_fill("stop-5", _Fill(20, 207.08))

    flatten = ib.orders[-1]
    assert flatten.orderType == "MKT"
    assert flatten.action == "SELL"
    assert flatten.totalQuantity == 20
    assert broker.long_positions[("absorption", "NVDA")] == 20
    assert any(event[0] == "stop_filled_position_remaining_flatten" and event[1]["quantity"] == 20 for event in logger.events)


def test_shared_broker_syncs_stop_quantity_amends_ib_when_stop_acknowledged():
    ib = _IB()
    logger = _Logger()
    receiver = _Receiver()
    broker = SharedBroker(ib, {"NVDA": object()}, PositionRegistry(), logger)

    broker._place(receiver, "NVDA", "entry-1", "entry", _Order("BUY", 123, limit_price=202.49))
    broker._on_fill("entry-1", _Fill(100, 202.41))
    broker._place(receiver, "NVDA", "stop-5", "stop", _Order("SELL", 100, order_type="STP", stop_price=201.97))

    stop = ib.orders[1]
    stop.permId = 67108270  # simulate IB acknowledgement

    broker._on_fill("entry-1", _Fill(23, 202.32))

    assert stop.totalQuantity == 123
    assert stop in ib.modifications
    assert any(event[0] == "stop_quantity_updated" for event in logger.events)


def test_shared_broker_lock_or_raise_does_not_raise_when_same_strategy_holds_open_lock():
    ib = _IB()
    logger = _Logger()
    receiver = _Receiver()
    broker = SharedBroker(ib, {"NVDA": object()}, PositionRegistry(lock_on_entry_order=True), logger)

    broker._place(receiver, "NVDA", "entry-1", "entry", _Order("BUY", 100, limit_price=200.0))
    broker._on_fill("entry-1", _Fill(100, 200.0))
    # registry now holds OPEN for "absorption"; re-calling _lock_or_raise must not raise
    broker._lock_or_raise("NVDA", "absorption", datetime(2026, 6, 2, 14, 0, tzinfo=timezone.utc), "test")


def test_shared_broker_does_not_resubmit_unacknowledged_exit_order_on_intermediate_check():
    ib = _IB()
    logger = _Logger()
    receiver = _Receiver()
    contract = _Contract("NVDA")
    broker = SharedBroker(ib, {"NVDA": contract}, PositionRegistry(), logger, exit_ack_timeout_seconds=5, exit_ack_max_wait_seconds=30)

    # Place entry and fill to establish a position
    broker._place(receiver, "NVDA", "entry-1", "entry", _Order("BUY", 100, limit_price=210.0))
    broker._on_fill("entry-1", _Fill(100, 210.0))

    # Place stop order; it goes into _exit_pending_ack.
    stop_order = _Order("SELL", 100, order_type="STP", stop_price=208.0)
    broker._place(receiver, "NVDA", "stop-2", "stop", stop_order)

    # Simulate IB not acknowledging: order sits in PendingSubmit
    stop_trade = ib.trade_list[-1]
    stop_trade.orderStatus.status = "PendingSubmit"

    # Manually backdate the submission time so it appears 6 seconds old (past
    # exit_ack_timeout_seconds=5, but well within exit_ack_max_wait_seconds=30)
    submitted_at, _ = broker._exit_pending_ack["stop-2"]
    broker._exit_pending_ack["stop-2"] = (submitted_at - timedelta(seconds=6), 0)

    broker.resubmit_unacknowledged_exits(datetime.now(timezone.utc))

    # No resubmit: reusing the orderId for a still-unacknowledged order looks like a
    # duplicate submission to IB and provokes an error that ib_insync turns into an
    # unsolicited cancel, which used to cascade into an immediate forced flatten.
    assert stop_trade.order not in ib.modifications
    assert not any(e[0] == "exit_order_resubmitted" for e in logger.events)
    pending_event = next(e[1] for e in logger.events if e[0] == "exit_order_ack_pending")
    assert pending_event["order_id"] == "stop-2"
    assert pending_event["check"] == 1
    # Position still being monitored; not yet flattened
    assert "stop-2" in broker._exit_pending_ack
    _, checks = broker._exit_pending_ack["stop-2"]
    assert checks == 1


def test_shared_broker_flattens_position_when_exit_order_never_acknowledged():
    ib = _IB()
    logger = _Logger()
    receiver = _Receiver()
    contract = _Contract("NVDA")
    broker = SharedBroker(ib, {"NVDA": contract}, PositionRegistry(), logger, exit_ack_timeout_seconds=5, exit_ack_max_wait_seconds=20)
    broker._market_order = lambda side, qty: _Order(side, qty, order_type="MKT")

    broker._place(receiver, "NVDA", "entry-1", "entry", _Order("BUY", 100, limit_price=210.0))
    broker._on_fill("entry-1", _Fill(100, 210.0))

    stop_order = _Order("SELL", 100, order_type="STP", stop_price=208.0)
    broker._place(receiver, "NVDA", "stop-2", "stop", stop_order)

    stop_trade = ib.trade_list[-1]
    stop_trade.orderStatus.status = "PendingSubmit"

    submitted_at, _ = broker._exit_pending_ack["stop-2"]
    broker._exit_pending_ack["stop-2"] = (submitted_at - timedelta(seconds=21), 0)

    broker.resubmit_unacknowledged_exits(datetime.now(timezone.utc))

    assert any(e[0] == "exit_order_ack_timeout_flatten" for e in logger.events)
    flatten_event = next(e[1] for e in logger.events if e[0] == "exit_order_ack_timeout_flatten")
    assert flatten_event["symbol"] == "NVDA"
    assert flatten_event["status"] == "PendingSubmit"
    assert ib.orders[-1].orderType == "MKT"
    assert ib.orders[-1].totalQuantity == 100
    assert "stop-2" not in broker._exit_pending_ack


def test_shared_broker_does_not_flatten_when_target_order_never_acknowledged():
    ib = _IB()
    logger = _Logger()
    receiver = _Receiver()
    contract = _Contract("NVDA")
    broker = SharedBroker(ib, {"NVDA": contract}, PositionRegistry(), logger, exit_ack_timeout_seconds=5, exit_ack_max_wait_seconds=20)
    broker._market_order = lambda side, qty: _Order(side, qty, order_type="MKT")

    broker._place(receiver, "NVDA", "entry-1", "entry", _Order("BUY", 100, limit_price=210.0))
    broker._on_fill("entry-1", _Fill(100, 210.0))

    broker._place(receiver, "NVDA", "tp1-2", "tp1", _Order("SELL", 40, limit_price=211.0))
    broker._place(receiver, "NVDA", "stop-3", "stop", _Order("SELL", 100, order_type="STP", stop_price=208.0))

    assert "tp1-2" not in broker._exit_pending_ack
    assert "stop-3" in broker._exit_pending_ack

    target_trade = ib.trade_list[-2]
    target_trade.orderStatus.status = "PendingSubmit"
    broker.resubmit_unacknowledged_exits(datetime.now(timezone.utc) + timedelta(seconds=30))

    assert not any(e[0] == "exit_order_ack_timeout_flatten" and e[1]["order_id"] == "tp1-2" for e in logger.events)
    assert not any(e[0] == "forced_flatten_submitted" for e in logger.events)
    assert ib.orders[-1].orderType == "STP"


def test_shared_broker_flattens_when_protective_exit_is_cancelled_unexpectedly():
    ib = _IB()
    logger = _Logger()
    receiver = _Receiver()
    broker = SharedBroker(ib, {"NVDA": _Contract("NVDA")}, PositionRegistry(), logger)
    broker._market_order = lambda side, qty: _Order(side, qty, order_type="MKT")

    broker._place(receiver, "NVDA", "entry-1", "entry", _Order("BUY", 100, limit_price=210.0))
    broker._on_fill("entry-1", _Fill(100, 210.0))
    broker._place(receiver, "NVDA", "stop-2", "stop", _Order("SELL", 100, order_type="STP", stop_price=208.0))

    stop_trade = ib.trade_list[-1]
    stop_trade.orderStatus.status = "Cancelled"
    ib.orderStatusEvent.fire(stop_trade)

    assert any(e[0] == "exit_order_cancelled_flatten" for e in logger.events)
    cancel_event = next(e[1] for e in logger.events if e[0] == "exit_order_cancelled_flatten")
    assert cancel_event["order_id"] == "stop-2"
    assert cancel_event["status"] == "Cancelled"
    assert ib.orders[-1].orderType == "MKT"
    assert ib.orders[-1].totalQuantity == 100


def test_expected_stop_cancel_during_flatten_does_not_submit_second_flatten():
    ib = _IB()
    logger = _Logger()
    receiver = _Receiver()
    broker = SharedBroker(ib, {"NVDA": _Contract("NVDA")}, PositionRegistry(), logger)

    broker._place(receiver, "NVDA", "entry-1", "entry", _Order("BUY", 20, limit_price=209.0))
    broker._on_fill("entry-1", _Fill(20, 209.0))
    broker._place(receiver, "NVDA", "stop-2", "stop", _Order("SELL", 20, order_type="STP", stop_price=208.0))
    stop_trade = ib.trade_list[-1]

    # A max-hold flatten cancels the protective stop before submitting its
    # market sell. IB reports PendingCancel and Cancelled as separate events.
    broker._place(receiver, "NVDA", "flatten-3", "flatten", _Order("SELL", 20, order_type="MKT"))
    assert "stop-2" in broker._expected_exit_cancels

    stop_trade.orderStatus.status = "PendingCancel"
    ib.orderStatusEvent.fire(stop_trade)
    assert "stop-2" in broker._expected_exit_cancels

    stop_trade.orderStatus.status = "Cancelled"
    ib.orderStatusEvent.fire(stop_trade)

    flatten_orders = [order for order in ib.orders if order.orderType == "MKT"]
    assert len(flatten_orders) == 1
    assert "stop-2" not in broker._expected_exit_cancels
    assert not any(event[0] == "exit_order_cancelled_flatten" for event in logger.events)
    assert not any(event[0] == "forced_flatten_submitted" for event in logger.events)


def test_shared_broker_rejects_second_working_flatten_for_same_position():
    ib = _IB()
    logger = _Logger()
    receiver = _Receiver()
    broker = SharedBroker(ib, {"NVDA": _Contract("NVDA")}, PositionRegistry(), logger)

    broker._place(receiver, "NVDA", "entry-1", "entry", _Order("BUY", 20, limit_price=209.0))
    broker._on_fill("entry-1", _Fill(20, 209.0))
    broker._place(receiver, "NVDA", "flatten-2", "flatten", _Order("SELL", 20, order_type="MKT"))
    broker._place(receiver, "NVDA", "flatten-3", "flatten", _Order("SELL", 20, order_type="MKT"))

    flatten_orders = [order for order in ib.orders if order.orderType == "MKT"]
    assert len(flatten_orders) == 1
    duplicate = next(event[1] for event in logger.events if event[0] == "duplicate_flatten_rejected")
    assert duplicate["order_id"] == "flatten-3"
    assert duplicate["working_order_id"] == "flatten-2"


def test_long_only_broker_immediately_covers_an_unexpected_flatten_overfill():
    ib = _IB()
    logger = _Logger()
    receiver = _Receiver()
    broker = SharedBroker(ib, {"NVDA": _Contract("NVDA")}, PositionRegistry(), logger)
    broker._market_order = lambda side, qty: _Order(side, qty, order_type="MKT")

    broker._place(receiver, "NVDA", "entry-1", "entry", _Order("BUY", 20, limit_price=209.0))
    broker._on_fill("entry-1", _Fill(20, 209.0))
    broker._place(receiver, "NVDA", "flatten-2", "flatten", _Order("SELL", 20, order_type="MKT"))

    # Simulate an authoritative broker fill exceeding the remaining long.
    broker._on_fill("flatten-2", _Fill(25, 209.16))

    cover = ib.orders[-1]
    assert cover.action == "BUY"
    assert cover.orderType == "MKT"
    assert cover.totalQuantity == 5
    assert broker.short_positions[("absorption", "NVDA")] == 5
    assert any(event == "long_only_short_emergency_cover" for event, _ in logger.events)


def test_account_level_flatten_covers_quarantined_short_even_when_positions_are_unmanaged():
    ib = _IB()
    ib.account_positions = [_Position("NVDA", -20, 200.55)]
    logger = _Logger()
    broker = SharedBroker(
        ib,
        {"NVDA": _Contract("NVDA")},
        PositionRegistry(),
        logger,
        manage_account_positions=False,
    )
    broker._market_order = lambda side, qty: _Order(side, qty, order_type="MKT")

    submitted = broker.flatten_account_positions(
        datetime(2026, 7, 23, 13, 29, tzinfo=timezone.utc),
        "startup_position_close",
    )

    assert submitted == 1
    assert [(order.action, order.totalQuantity) for order in ib.orders] == [("BUY", 20)]
    assert broker.short_positions[("account", "NVDA")] == 20


def test_shared_broker_does_not_flatten_when_target_is_cancelled_unexpectedly():
    ib = _IB()
    logger = _Logger()
    receiver = _Receiver()
    broker = SharedBroker(ib, {"NVDA": _Contract("NVDA")}, PositionRegistry(), logger)
    broker._market_order = lambda side, qty: _Order(side, qty, order_type="MKT")

    broker._place(receiver, "NVDA", "entry-1", "entry", _Order("BUY", 100, limit_price=210.0))
    broker._on_fill("entry-1", _Fill(100, 210.0))
    broker._place(receiver, "NVDA", "tp1-2", "tp1", _Order("SELL", 40, limit_price=211.0))
    broker._place(receiver, "NVDA", "stop-3", "stop", _Order("SELL", 100, order_type="STP", stop_price=208.0))

    target_trade = ib.trade_list[-2]
    target_trade.orderStatus.status = "Cancelled"
    ib.orderStatusEvent.fire(target_trade)

    assert any(e[0] == "exit_order_cancelled_no_flatten" for e in logger.events)
    assert not any(e[0] == "exit_order_cancelled_flatten" for e in logger.events)
    assert not any(e[0] == "forced_flatten_submitted" for e in logger.events)
    assert broker.exit_reservations_by_ref.get("tp1-2", 0) == 0


def test_runner_promotion_never_amends_already_filled_tp1_after_tp2_fill():
    ib = _IB()
    logger = _Logger()
    receiver = _Receiver()
    broker = SharedBroker(ib, {"TSLA": _Contract("TSLA")}, PositionRegistry(), logger)

    broker._place(receiver, "TSLA", "entry-1", "entry", _Order("BUY", 65, limit_price=379.0))
    broker._on_fill("entry-1", _Fill(65, 379.0))
    broker._place(receiver, "TSLA", "tp1-2", "tp1", _Order("SELL", 13, limit_price=380.42))
    broker._place(receiver, "TSLA", "tp2-3", "tp2", _Order("SELL", 27, limit_price=381.84))
    broker._place(receiver, "TSLA", "stop-4", "stop", _Order("SELL", 40, order_type="STP", stop_price=378.04))

    broker._on_fill("tp1-2", _Fill(13, 380.42))
    tp1_trade = next(trade for trade in ib.trade_list if trade.order.orderRef == "tp1-2")
    tp1_trade.orderStatus.status = "Filled"
    tp1_trade.orderStatus.remaining = 0
    modifications_before = list(ib.modifications)

    broker._on_fill("tp2-3", _Fill(27, 381.84))

    assert ("tp2-3", 27, 381.84) in receiver.fills
    assert any(name == "fills" and row["order_id"] == "tp2-3" for name, row in logger.rows)
    assert tp1_trade.order not in ib.modifications[len(modifications_before):]
    assert not any(event == "runner_target_promoted" and payload["order_id"] == "tp1-2" for event, payload in logger.events)


def test_fill_is_logged_and_delivered_when_post_fill_maintenance_raises():
    ib = _IB()
    logger = _Logger()
    receiver = _Receiver()
    broker = SharedBroker(ib, {"NVDA": _Contract("NVDA")}, PositionRegistry(), logger)
    broker._place(receiver, "NVDA", "entry-1", "entry", _Order("BUY", 10, limit_price=200.0))

    def fail_stop_sync(*args, **kwargs):
        raise RuntimeError("simulated amendment failure")

    broker._sync_stop_quantity = fail_stop_sync
    broker._on_fill("entry-1", _Fill(10, 200.0))

    assert receiver.fills == [("entry-1", 10, 200.0)]
    assert any(name == "fills" and row["order_id"] == "entry-1" for name, row in logger.rows)
    assert any(event == "post_fill_maintenance_failed" for event, _ in logger.events)


def test_unmanaged_position_is_quarantined_without_being_managed_or_flattened():
    ib = _IB()
    ib.account_positions = [_Position("NVDA", -20, 200.55)]
    logger = _Logger()
    registry = PositionRegistry()
    broker = SharedBroker(
        ib,
        {"NVDA": _Contract("NVDA")},
        registry,
        logger,
        manage_account_positions=False,
        quarantine_unmanaged_positions=True,
    )
    now = datetime(2026, 7, 17, 13, 29, tzinfo=timezone.utc)

    broker.sync_account_positions(now)

    assert broker.unmanaged_positions == {"NVDA": -20}
    assert broker.entry_block_reason("NVDA", "absorption") == "unmanaged_account_position"
    assert registry.owner("NVDA") == "account"
    assert broker.has_open_positions() is False
    assert ib.orders == []

    ib.account_positions = []
    broker.sync_account_positions(now + timedelta(seconds=30))
    assert broker.unmanaged_positions == {}
    assert registry.owner("NVDA") is None


def test_account_reconciliation_subtracts_strategy_inventory_before_quarantine():
    ib = _IB()
    logger = _Logger()
    receiver = _Receiver()
    broker = SharedBroker(
        ib,
        {"NVDA": _Contract("NVDA")},
        PositionRegistry(),
        logger,
        manage_account_positions=False,
    )
    broker._place(receiver, "NVDA", "entry-1", "entry", _Order("BUY", 100, limit_price=200.0))
    broker._on_fill("entry-1", _Fill(100, 200.0))
    ib.account_positions = [_Position("NVDA", 80, 200.0)]

    broker.sync_account_positions(datetime(2026, 7, 17, 13, 35, tzinfo=timezone.utc))

    assert broker.unmanaged_positions == {"NVDA": -20}
    event = next(payload for name, payload in logger.events if name == "unmanaged_position_quarantined")
    assert event["account_quantity"] == 80
    assert event["managed_quantity"] == 100


def test_depth_permission_error_blocks_only_depth_dependent_strategies():
    ib = _IB()
    logger = _Logger()
    broker = SharedBroker(
        ib,
        {"NVDA": _Contract("NVDA")},
        PositionRegistry(),
        logger,
        fail_closed_on_depth_permission_error=True,
    )

    broker._on_ib_error_event(10, 2152, "Need additional market data permissions", _Contract("NVDA"))

    assert broker.entry_block_reason("NVDA", "absorption") == "depth_permissions_unavailable"
    assert broker.entry_block_reason("NVDA", "pullback") == "depth_permissions_unavailable"
    assert broker.entry_block_reason("NVDA", "opening_range") is None
    assert any(name == "depth_strategy_symbol_blocked" for name, _ in logger.events)


def test_depth_permission_error_does_not_block_l1_pullback():
    ib = _IB()
    logger = _Logger()
    broker = SharedBroker(
        ib,
        {"NVDA": _Contract("NVDA")},
        PositionRegistry(),
        logger,
        fail_closed_on_depth_permission_error=True,
        depth_required_strategies={"absorption"},
    )

    broker._on_ib_error_event(10, 2152, "Need additional market data permissions", _Contract("NVDA"))

    assert broker.entry_block_reason("NVDA", "absorption") == "depth_permissions_unavailable"
    assert broker.entry_block_reason("NVDA", "pullback") is None


def test_depth_permission_error_does_not_block_entries_by_default():
    ib = _IB()
    logger = _Logger()
    broker = SharedBroker(ib, {"NVDA": _Contract("NVDA")}, PositionRegistry(), logger)

    broker._on_ib_error_event(10, 2152, "Need additional market data permissions", _Contract("NVDA"))

    assert broker.entry_block_reason("NVDA", "absorption") is None
    assert broker.entry_block_reason("NVDA", "pullback") is None
    assert not any(name == "depth_strategy_symbol_blocked" for name, _ in logger.events)


def test_loss_stop_starts_configured_symbol_cooldown():
    ib = _IB()
    logger = _Logger()
    receiver = _Receiver()
    broker = SharedBroker(ib, {"NVDA": _Contract("NVDA")}, PositionRegistry(), logger, stop_loss_cooldown_seconds=600)
    broker._place(receiver, "NVDA", "entry-1", "entry", _Order("BUY", 10, limit_price=200.0))
    broker._on_fill("entry-1", _Fill(10, 200.0))
    broker._place(receiver, "NVDA", "stop-2", "stop", _Order("SELL", 10, order_type="STP", stop_price=199.0))
    fill = _Fill(10, 198.95)

    broker._on_fill("stop-2", fill)

    assert broker.is_symbol_cooling_down("NVDA", fill.execution.time + timedelta(minutes=5)) is True
    event = next(payload for name, payload in logger.events if name == "symbol_cooldown_started")
    assert event["reason"] == "stop_loss"
    assert event["seconds"] == 600
