"""
OMS scenario tests covering the five reliability requirements:

  1. Always know pending positions — broker tracks qty/avg after every fill
  2. Always know the plan — stop/target preserved per position
  3. Restart behaviour — state file saves/loads; reconcile_on_startup restores or
                         clears positions based on live IBKR data
  4. Long-only guarantee — SELL without long position is hard-rejected at _place
  5. Fill status — partial, full, and no-fill scenarios; confirm_fills_on_reconnect
                   replays missed fills exactly once
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from multi_strategy.broker import SharedBroker, TrackedOrder
from multi_strategy.registry import PositionRegistry
from multi_strategy.state_store import PositionRecord, PositionStateStore

NOW = datetime(2026, 6, 2, 14, 0, tzinfo=timezone.utc)


# ─────────────────────────────────────────────────────────────────────────────
# Simulation infrastructure
# ─────────────────────────────────────────────────────────────────────────────

class _FillEvent:
    def __init__(self):
        self.handlers = []
    def __iadd__(self, fn):
        self.handlers.append(fn)
        return self


class _SimTrade:
    def __init__(self, order):
        self.order = order
        self.fillEvent = _FillEvent()
        self.fills: list = []
        self.orderStatus = type("S", (), {"status": "Submitted"})()


class _SimExecution:
    def __init__(self, shares, price, exec_id):
        self.shares = float(shares)
        self.price = float(price)
        self.execId = exec_id
        self.time = NOW


class _SimFill:
    def __init__(self, execution, commission=0.0):
        self.execution = execution
        self.commissionReport = type("R", (), {"commission": commission})()


class _SimPosition:
    def __init__(self, symbol: str, qty: int, avg_cost: float):
        self.contract = type("C", (), {"symbol": symbol})()
        self.position = qty
        self.avgCost = avg_cost


class SimulatedIB:
    """Drop-in for ib_insync.IB used in OMS tests.

    Supports programmatic fill triggering, account position injection,
    and fill-list population for confirm_fills_on_reconnect tests.
    """

    def __init__(self):
        self._trades: dict[int, _SimTrade] = {}
        self._next_id = 1
        self._account_positions: list[_SimPosition] = []
        self._cancelled: list = []

    # ib_insync-compatible interface
    def placeOrder(self, contract, order):
        if getattr(order, "orderId", None) is None:
            order.orderId = self._next_id
            self._next_id += 1
        trade = _SimTrade(order)
        self._trades[order.orderId] = trade
        return trade

    def cancelOrder(self, order):
        self._cancelled.append(order)

    def trades(self):
        return list(self._trades.values())

    def positions(self):
        return self._account_positions

    def reqPositions(self):
        return self._account_positions

    # test helpers
    def fill(self, ib_order_id: int, qty: int, price: float, commission: float = 0.0) -> None:
        """Trigger fill callbacks for an order, populating trade.fills."""
        trade = self._trades[ib_order_id]
        exec_id = f"exec-{ib_order_id}-{len(trade.fills) + 1}"
        execution = _SimExecution(qty, price, exec_id)
        fill = _SimFill(execution, commission)
        trade.fills.append(fill)
        for handler in trade.fillEvent.handlers:
            handler(trade, fill)

    def last_ib_id(self) -> int:
        return max(self._trades)

    def set_positions(self, positions: list[_SimPosition]) -> None:
        self._account_positions = positions


class _Logger:
    def __init__(self):
        self.events: list[tuple] = []
        self.rows: list[tuple] = []
    def event(self, t, p): self.events.append((t, p))
    def csv(self, n, r): self.rows.append((n, r))


class _Receiver:
    def __init__(self, strategy: str = "absorption"):
        self.strategy_name = strategy
        self.fills: list[tuple] = []
    def on_broker_fill(self, oid, ts, qty, price, commission=0.0):
        self.fills.append((oid, qty, price))


def _order(action="BUY", qty=100, *, order_type="LMT", price=200.0, stop=0.0):
    class _O:
        pass
    o = _O()
    o.action = action
    o.totalQuantity = qty
    o.orderType = order_type
    o.lmtPrice = price
    o.auxPrice = stop
    o.orderId = None
    o.ocaGroup = ""
    o.ocaType = 0
    return o


def make_broker(symbols=("NVDA",), **kwargs):
    ib = SimulatedIB()
    contracts = {s: object() for s in symbols}
    registry = PositionRegistry()
    logger = _Logger()
    broker = SharedBroker(ib, contracts, registry, logger, **kwargs)
    return broker, ib, registry, logger


# ─────────────────────────────────────────────────────────────────────────────
# 1 — Pending positions are always known
# ─────────────────────────────────────────────────────────────────────────────

def test_position_known_after_full_entry_fill():
    broker, ib, _, _ = make_broker()
    recv = _Receiver()
    broker._place(recv, "NVDA", "entry-1", "entry", _order("BUY", 100, price=200.0))
    ib.fill(ib.last_ib_id(), 100, 200.0)

    assert broker.long_positions[("absorption", "NVDA")] == 100
    assert broker.long_avg_prices[("absorption", "NVDA")] == 200.0


def test_position_known_after_partial_fills():
    broker, ib, _, _ = make_broker()
    recv = _Receiver()
    broker._place(recv, "NVDA", "entry-1", "entry", _order("BUY", 100, price=200.0))
    ib_id = ib.last_ib_id()
    ib.fill(ib_id, 40, 199.50)
    ib.fill(ib_id, 60, 200.50)

    assert broker.long_positions[("absorption", "NVDA")] == 100
    assert broker.long_avg_prices[("absorption", "NVDA")] == 200.10


def test_position_cleared_after_stop_fill():
    broker, ib, registry, _ = make_broker()
    recv = _Receiver()
    broker._place(recv, "NVDA", "entry-1", "entry", _order("BUY", 100, price=200.0))
    ib.fill(ib.last_ib_id(), 100, 200.0)
    broker._place(recv, "NVDA", "stop-2", "stop", _order("SELL", 100, order_type="STP", stop=198.0))
    ib.fill(ib.last_ib_id(), 100, 198.0)

    assert ("absorption", "NVDA") not in broker.long_positions
    assert registry.owner("NVDA") is None


# ─────────────────────────────────────────────────────────────────────────────
# 2 — Exit plan is always known
# ─────────────────────────────────────────────────────────────────────────────

def test_plan_recorded_after_stop_submitted():
    broker, ib, _, _ = make_broker()
    recv = _Receiver()
    broker._place(recv, "NVDA", "entry-1", "entry", _order("BUY", 100, price=200.0))
    ib.fill(ib.last_ib_id(), 100, 200.0)
    broker._place(recv, "NVDA", "stop-2", "stop", _order("SELL", 100, order_type="STP", stop=198.0))

    key = ("absorption", "NVDA")
    assert broker.stop_orders_by_position[key] == "stop-2"
    assert broker.initial_stop_prices[key] == 198.0
    assert broker.current_stop_prices[key] == 198.0


def test_state_file_preserves_plan(tmp_path):
    broker, ib, _, _ = make_broker()
    recv = _Receiver()
    broker._place(recv, "NVDA", "entry-1", "entry", _order("BUY", 100, price=200.0))
    ib.fill(ib.last_ib_id(), 100, 200.0)
    broker._place(recv, "NVDA", "stop-2", "stop", _order("SELL", 100, order_type="STP", stop=198.0))

    store = PositionStateStore(tmp_path)
    store.save(broker)

    records, _ = store.load()
    assert len(records) == 1
    r = records[0]
    assert r.strategy == "absorption"
    assert r.symbol == "NVDA"
    assert r.quantity == 100
    assert r.avg_price == 200.0
    assert r.initial_stop == 198.0
    assert r.current_stop == 198.0
    assert r.stop_order_id == "stop-2"


def test_state_file_survives_corrupt_write(tmp_path):
    store = PositionStateStore(tmp_path)
    (tmp_path / "position_state.json").write_text("{{corrupt", encoding="utf-8")
    records, cooldowns = store.load()
    assert records == []
    assert cooldowns == {}


# ─────────────────────────────────────────────────────────────────────────────
# 3 — Restart behaviour
# ─────────────────────────────────────────────────────────────────────────────

def test_restart_restores_position_found_in_ibkr(tmp_path):
    # --- first session: enter + stop ---
    broker1, ib1, _, _ = make_broker()
    recv = _Receiver()
    broker1._place(recv, "NVDA", "entry-1", "entry", _order("BUY", 100, price=200.0))
    ib1.fill(ib1.last_ib_id(), 100, 200.0)
    broker1._place(recv, "NVDA", "stop-2", "stop", _order("SELL", 100, order_type="STP", stop=198.0))
    store = PositionStateStore(tmp_path)
    store.save(broker1)

    # --- restart ---
    broker2, ib2, registry2, logger2 = make_broker()
    ib2.set_positions([_SimPosition("NVDA", 100, 200.0)])
    records, cooldowns = store.load()
    broker2.reconcile_on_startup(NOW, records, cooldowns)

    assert broker2.long_positions[("absorption", "NVDA")] == 100
    assert broker2.initial_stop_prices.get(("absorption", "NVDA")) == 198.0
    assert broker2.current_stop_prices.get(("absorption", "NVDA")) == 198.0
    assert broker2.stop_orders_by_position.get(("absorption", "NVDA")) == "stop-2"
    assert registry2.owner("NVDA") == "absorption"
    assert any(e[0] == "startup_position_restored" for e in logger2.events)


def test_restart_clears_position_gone_from_ibkr(tmp_path):
    broker1, ib1, _, _ = make_broker()
    recv = _Receiver()
    broker1._place(recv, "NVDA", "entry-1", "entry", _order("BUY", 100, price=200.0))
    ib1.fill(ib1.last_ib_id(), 100, 200.0)
    store = PositionStateStore(tmp_path)
    store.save(broker1)

    broker2, ib2, registry2, logger2 = make_broker()
    ib2.set_positions([])  # position closed while we were down
    records, cooldowns = store.load()
    broker2.reconcile_on_startup(NOW, records, cooldowns)

    assert ("absorption", "NVDA") not in broker2.long_positions
    assert registry2.owner("NVDA") is None
    assert any(e[0] == "startup_position_gone" for e in logger2.events)


def test_restart_restores_cooldown_if_still_active(tmp_path):
    broker1, ib1, _, _ = make_broker()
    until = NOW + timedelta(hours=1)
    broker1.symbol_cooldowns["NVDA"] = until
    store = PositionStateStore(tmp_path)
    store.save(broker1)

    broker2, ib2, _, _ = make_broker()
    records, cooldowns = store.load()
    broker2.reconcile_on_startup(NOW, records, cooldowns)

    assert broker2.is_symbol_cooling_down("NVDA", NOW)


def test_restart_drops_expired_cooldown(tmp_path):
    broker1, ib1, _, _ = make_broker()
    broker1.symbol_cooldowns["NVDA"] = NOW - timedelta(seconds=1)  # already expired
    store = PositionStateStore(tmp_path)
    store.save(broker1)

    broker2, ib2, _, _ = make_broker()
    records, cooldowns = store.load()
    broker2.reconcile_on_startup(NOW, records, cooldowns)

    assert not broker2.is_symbol_cooling_down("NVDA", NOW)


def test_bind_position_receiver_wires_fill_to_adapter(tmp_path):
    broker, ib, _, _ = make_broker()
    recv = _Receiver()
    broker._place(recv, "NVDA", "entry-1", "entry", _order("BUY", 100, price=200.0))
    ib.fill(ib.last_ib_id(), 100, 200.0)
    broker._place(recv, "NVDA", "stop-2", "stop", _order("SELL", 100, order_type="STP", stop=198.0))
    store = PositionStateStore(tmp_path)
    store.save(broker)

    # restart
    new_recv = _Receiver()
    broker2, ib2, _, _ = make_broker()
    ib2.set_positions([_SimPosition("NVDA", 100, 200.0)])
    records, cooldowns = store.load()
    broker2.reconcile_on_startup(NOW, records, cooldowns)
    bound = broker2.bind_position_receiver("absorption", "NVDA", new_recv)

    assert bound
    assert broker2.position_receivers[("absorption", "NVDA")] is new_recv


# ─────────────────────────────────────────────────────────────────────────────
# 4 — Long-only guarantee
# ─────────────────────────────────────────────────────────────────────────────

def test_stop_rejected_when_no_long_position():
    broker, ib, _, logger = make_broker()
    recv = _Receiver()
    broker._place(recv, "NVDA", "stop-1", "stop", _order("SELL", 100, order_type="STP", stop=198.0))

    assert ib._trades == {}
    assert any(e[0] == "sell_rejected_no_long_position" for e in logger.events)


def test_target_rejected_when_no_long_position():
    broker, ib, _, logger = make_broker()
    recv = _Receiver()
    broker._place(recv, "NVDA", "tp1-1", "tp1", _order("SELL", 50, price=204.0))

    assert ib._trades == {}
    assert any(e[0] == "sell_rejected_no_long_position" for e in logger.events)


def test_further_sells_rejected_after_position_fully_closed():
    broker, ib, _, logger = make_broker()
    recv = _Receiver()
    broker._place(recv, "NVDA", "entry-1", "entry", _order("BUY", 100, price=200.0))
    ib.fill(ib.last_ib_id(), 100, 200.0)
    broker._place(recv, "NVDA", "stop-2", "stop", _order("SELL", 100, order_type="STP", stop=198.0))
    ib.fill(ib.last_ib_id(), 100, 198.0)

    # position is now flat — further exit attempts must be rejected
    logger.events.clear()
    broker._place(recv, "NVDA", "orphan-tp1", "tp1", _order("SELL", 50, price=204.0))

    assert any(e[0] == "sell_rejected_no_long_position" for e in logger.events)


def test_flatten_is_allowed_even_without_tracked_long_position():
    # flatten bypasses the long-only guard — _reserve_exit_quantity handles it
    broker, ib, _, logger = make_broker()
    recv = _Receiver()
    # Force an "account" position without going through normal entry
    broker.long_positions[("absorption", "NVDA")] = 50
    broker.position_receivers[("absorption", "NVDA")] = recv
    broker._place(recv, "NVDA", "flatten-1", "flatten", _order("SELL", 50, order_type="MKT", price=0.0))

    assert ib._trades  # order was placed
    assert not any(e[0] == "sell_rejected_no_long_position" for e in logger.events)


def test_long_only_invariant_holds_across_multiple_symbols():
    broker, ib, _, logger = make_broker(symbols=("NVDA", "TSLA"))
    recv_nvda = _Receiver()
    recv_tsla = _Receiver()

    # Enter NVDA legitimately
    broker._place(recv_nvda, "NVDA", "entry-1", "entry", _order("BUY", 50, price=200.0))
    ib.fill(ib.last_ib_id(), 50, 200.0)

    # Attempt a SELL on TSLA (no long position) — must be rejected
    broker._place(recv_tsla, "TSLA", "stop-tsla", "stop", _order("SELL", 50, order_type="STP", stop=400.0))

    assert ("absorption", "TSLA") not in broker.long_positions
    assert any(
        e[0] == "sell_rejected_no_long_position" and e[1]["symbol"] == "TSLA"
        for e in logger.events
    )


# ─────────────────────────────────────────────────────────────────────────────
# 5 — Fill status: partial, full, no-fill; reconnect replay
# ─────────────────────────────────────────────────────────────────────────────

def test_fill_status_partial_then_full():
    broker, ib, _, _ = make_broker()
    recv = _Receiver()
    broker._place(recv, "NVDA", "entry-1", "entry", _order("BUY", 100, price=200.0))
    ib_id = ib.last_ib_id()

    ib.fill(ib_id, 40, 200.0)
    assert broker.order_filled_quantities["entry-1"] == 40
    assert broker.order_quantities["entry-1"] == 100

    ib.fill(ib_id, 60, 200.0)
    assert broker.order_filled_quantities["entry-1"] == 100


def test_no_fill_leaves_position_at_zero():
    broker, ib, _, _ = make_broker()
    recv = _Receiver()
    broker._place(recv, "NVDA", "entry-1", "entry", _order("BUY", 100, price=200.0))

    # No fill triggered
    assert broker.long_positions.get(("absorption", "NVDA"), 0) == 0


def test_confirm_fills_on_reconnect_replays_missed_fills():
    broker, ib, _, _ = make_broker()
    recv = _Receiver()
    broker._place(recv, "NVDA", "entry-1", "entry", _order("BUY", 100, price=200.0))

    # Simulate: fill arrived at IBKR but callback was missed (populate fills list directly)
    ib_id = ib.last_ib_id()
    trade = ib._trades[ib_id]
    execution = _SimExecution(100, 200.0, "exec-missed-1")
    fill = _SimFill(execution)
    trade.fills.append(fill)
    # Don't fire the callback — broker doesn't know yet
    assert broker.long_positions.get(("absorption", "NVDA"), 0) == 0

    # Reconnect replay
    broker.confirm_fills_on_reconnect()
    assert broker.long_positions[("absorption", "NVDA")] == 100


def test_confirm_fills_on_reconnect_deduplicates_already_processed_fills():
    broker, ib, _, _ = make_broker()
    recv = _Receiver()
    broker._place(recv, "NVDA", "entry-1", "entry", _order("BUY", 100, price=200.0))

    # Fill arrives normally
    ib.fill(ib.last_ib_id(), 100, 200.0)
    assert broker.long_positions[("absorption", "NVDA")] == 100

    # Confirm on reconnect — same execId must not be applied again
    broker.confirm_fills_on_reconnect()
    assert broker.long_positions[("absorption", "NVDA")] == 100  # still 100, not 200


def test_order_ref_is_set_on_placed_orders():
    broker, ib, _, _ = make_broker()
    recv = _Receiver()
    broker._place(recv, "NVDA", "entry-1", "entry", _order("BUY", 100, price=200.0))

    placed_order = ib._trades[ib.last_ib_id()].order
    assert getattr(placed_order, "orderRef", None) == "entry-1"


# ─────────────────────────────────────────────────────────────────────────────
# State store unit tests
# ─────────────────────────────────────────────────────────────────────────────

def test_state_store_is_empty_when_no_positions(tmp_path):
    broker, _, _, _ = make_broker()
    store = PositionStateStore(tmp_path)
    store.save(broker)
    records, cooldowns = store.load()
    assert records == []
    assert cooldowns == {}


def test_state_store_does_not_save_flat_positions(tmp_path):
    broker, ib, _, _ = make_broker()
    recv = _Receiver()
    broker._place(recv, "NVDA", "entry-1", "entry", _order("BUY", 100, price=200.0))
    ib.fill(ib.last_ib_id(), 100, 200.0)
    broker._place(recv, "NVDA", "stop-2", "stop", _order("SELL", 100, order_type="STP", stop=198.0))
    ib.fill(ib.last_ib_id(), 100, 198.0)  # stop hit — position flat

    store = PositionStateStore(tmp_path)
    store.save(broker)
    records, _ = store.load()
    assert records == []


def test_state_store_returns_empty_when_file_missing(tmp_path):
    store = PositionStateStore(tmp_path)
    records, cooldowns = store.load()
    assert records == []
    assert cooldowns == {}
