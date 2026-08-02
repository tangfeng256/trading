from __future__ import annotations

from datetime import datetime, timedelta
from itertools import count

from .config import ExecutionConfig, MarketConfig, StrategyConfig
from .logger import RunLogger
from .order_state import ManagedOrder, ManagedTrade, OrderStatus, Signal, TradeStatus
from .pricing import round_to_tick


class ExecutionManager:
    """Safe paper/live-neutral order state machine.

    It records intended orders and state transitions. The IB client can submit
    these orders in live mode, but duplicate protective pairs are blocked here.
    """

    def __init__(
        self,
        execution: ExecutionConfig,
        market: MarketConfig,
        strategy: StrategyConfig,
        logger: RunLogger | None = None,
    ) -> None:
        self.execution = execution
        self.market = market
        self.strategy = strategy
        self.logger = logger
        self.trades: dict[str, ManagedTrade] = {}
        self.orders: dict[str, ManagedOrder] = {}
        self._order_to_trade: dict[str, str] = {}
        self._ids = count(1)
        self.kill_switch = False

    def _next_id(self, prefix: str) -> str:
        return f"{prefix}-{next(self._ids)}"

    def submit_entry(self, signal: Signal, qty: int) -> ManagedTrade:
        if self.kill_switch:
            raise RuntimeError("emergency kill switch enabled")
        entry_order_id = self._next_id("entry")
        trade_id = self._next_id("trade")
        limit_price = round_to_tick(
            signal.entry_ref_price + self.execution.entry_price_offset_ticks * self.market.tick_size,
            self.market.tick_size,
            "up",
        )
        order = ManagedOrder(
            order_id=entry_order_id,
            symbol=signal.symbol,
            side="BUY",
            qty=qty,
            order_type=self.execution.entry_order_type,
            price=limit_price,
            role="entry",
            status=OrderStatus.SUBMITTED,
            created_at=signal.timestamp,
        )
        trade = ManagedTrade(
            trade_id=trade_id,
            symbol=signal.symbol,
            entry_order_id=entry_order_id,
            entry_price=signal.entry_ref_price,
            stop_price=signal.stop_price,
            target1_price=signal.target1_price,
            target2_price=signal.target2_price,
            qty=qty,
            opened_at=signal.timestamp,
            orders={entry_order_id: order},
        )
        self.orders[entry_order_id] = order
        self.trades[trade_id] = trade
        self._order_to_trade[entry_order_id] = trade_id
        self._log_order(order)
        return trade

    def on_fill(self, order_id: str, fill_qty: int, fill_price: float, timestamp: datetime) -> list[ManagedOrder]:
        if order_id not in self.orders:
            return []
        order = self.orders[order_id]
        old_filled = order.filled_qty
        order.filled_qty = min(order.qty, order.filled_qty + fill_qty)
        order.avg_fill_price = (
            (order.avg_fill_price * old_filled + fill_price * fill_qty) / max(order.filled_qty, 1)
        )
        order.status = OrderStatus.FILLED if order.remaining_qty == 0 else OrderStatus.PARTIALLY_FILLED
        self._log_fill(order, fill_qty, fill_price, timestamp)
        trade = self._trade_for_order(order_id)
        if not trade:
            return []
        created: list[ManagedOrder] = []
        if order.role == "entry":
            trade.filled_qty = order.filled_qty
            trade.status = TradeStatus.OPEN
            created = self.ensure_protection(trade.trade_id, timestamp)
        elif order.side == "SELL":
            entry_order = self.orders.get(trade.entry_order_id)
            entry_price = float(getattr(entry_order, "avg_fill_price", 0.0) or trade.entry_price)
            trade.realized_pnl += (fill_price - entry_price) * fill_qty
            if order.role == "tp1" and order.remaining_qty == 0:
                trade.tp1_filled = True
                if self.execution.move_stop_to_breakeven_after_tp1:
                    self.tighten_stop_to_breakeven(trade.trade_id)
            sold_qty = sum(exit_order.filled_qty for exit_order in trade.orders.values() if exit_order.side == "SELL")
            if sold_qty >= trade.filled_qty:
                trade.status = TradeStatus.CLOSED
        return created

    def ensure_protection(self, trade_id: str, timestamp: datetime) -> list[ManagedOrder]:
        trade = self.trades[trade_id]
        if trade.filled_qty <= 0:
            return []
        if trade.stop_created and trade.tp1_created and trade.tp2_created and trade.protection_qty == trade.filled_qty:
            return []
        if trade.stop_created or trade.tp1_created or trade.tp2_created:
            if trade.protection_qty == trade.filled_qty:
                return []
            # Entry fills can arrive in several executions during the protective-
            # order delay. Resize the queued order objects in place so the broker
            # receives protection for the final filled quantity, not just the first
            # execution fragment.
            tp1_qty, tp2_qty = self._protection_split(trade.filled_qty)
            existing = {order.role: order for order in trade.orders.values() if order.side == "SELL"}
            if "tp1" in existing and existing["tp1"].filled_qty == 0:
                existing["tp1"].qty = tp1_qty
            if "tp2" in existing and existing["tp2"].filled_qty == 0:
                existing["tp2"].qty = tp2_qty
            if "stop" in existing and existing["stop"].filled_qty == 0:
                existing["stop"].qty = trade.filled_qty
            created: list[ManagedOrder] = []
            if tp2_qty > 0 and "tp2" not in existing:
                created.append(self._create_exit(trade, "tp2", "SELL", tp2_qty, "LMT", timestamp, price=trade.target2_price))
                trade.tp2_created = True
            trade.protection_qty = trade.filled_qty
            return created

        tp1_qty, tp2_qty = self._protection_split(trade.filled_qty)
        created = []
        if tp1_qty > 0:
            created.append(self._create_exit(trade, "tp1", "SELL", tp1_qty, "LMT", timestamp, price=trade.target1_price))
            trade.tp1_created = True
        if tp2_qty > 0:
            created.append(self._create_exit(trade, "tp2", "SELL", tp2_qty, "LMT", timestamp, price=trade.target2_price))
            trade.tp2_created = True
        created.append(self._create_exit(trade, "stop", "SELL", trade.filled_qty, "STP", timestamp, stop_price=trade.stop_price))
        trade.stop_created = True
        trade.protection_qty = trade.filled_qty
        return created

    def _protection_split(self, quantity: int) -> tuple[int, int]:
        tp1_qty = int(quantity * self.execution.tp1_fraction)
        tp1_qty = max(1, min(tp1_qty, quantity))
        return tp1_qty, quantity - tp1_qty

    def _create_exit(
        self,
        trade: ManagedTrade,
        role: str,
        side: str,
        qty: int,
        order_type: str,
        timestamp: datetime,
        price: float | None = None,
        stop_price: float | None = None,
    ) -> ManagedOrder:
        order = ManagedOrder(
            order_id=self._next_id(role),
            symbol=trade.symbol,
            side=side,
            qty=qty,
            order_type=order_type,
            price=round_to_tick(price, self.market.tick_size, "up") if price is not None else None,
            stop_price=round_to_tick(stop_price, self.market.tick_size, "down") if stop_price is not None else None,
            role=role,
            status=OrderStatus.SUBMITTED,
            created_at=timestamp,
            parent_id=trade.entry_order_id,
        )
        trade.orders[order.order_id] = order
        self.orders[order.order_id] = order
        self._order_to_trade[order.order_id] = trade.trade_id
        self._log_order(order)
        return order

    def tighten_stop_to_breakeven(self, trade_id: str, buffer_ticks: int = 0) -> None:
        trade = self.trades[trade_id]
        new_stop = round_to_tick(trade.entry_price - buffer_ticks * self.market.tick_size, self.market.tick_size, "down")
        for order in trade.orders.values():
            if order.role == "stop" and order.status in {OrderStatus.SUBMITTED, OrderStatus.PARTIALLY_FILLED}:
                order.stop_price = max(order.stop_price or new_stop, new_stop)
                self._log_order(order)

    def cancel_stale_entries(self, now: datetime) -> list[ManagedOrder]:
        cancelled = []
        for order in self.orders.values():
            if order.role == "entry" and order.status == OrderStatus.SUBMITTED and order.created_at:
                if now - order.created_at >= timedelta(seconds=self.execution.stale_entry_seconds):
                    order.status = OrderStatus.CANCELLED
                    cancelled.append(order)
                    self._log_order(order)
        return cancelled

    def flatten_expired_positions(self, now: datetime) -> list[ManagedOrder]:
        flattened = []
        for trade in self.trades.values():
            if trade.status == TradeStatus.OPEN and now - trade.opened_at >= timedelta(minutes=self.strategy.max_hold_minutes):
                flattened.append(self.flatten_trade(trade.trade_id, now, "max_hold_time"))
        return flattened

    def flatten_trade(self, trade_id: str, timestamp: datetime, reason: str) -> ManagedOrder:
        trade = self.trades[trade_id]
        qty = max(0, trade.filled_qty)
        order = self._create_exit(trade, "flatten", "SELL", qty, "MKT", timestamp)
        order.meta["reason"] = reason
        trade.status = TradeStatus.EXITING
        return order

    def reconcile(self) -> dict:
        return {
            "open_trades": sum(1 for t in self.trades.values() if t.is_active),
            "open_orders": sum(1 for o in self.orders.values() if o.status in {OrderStatus.SUBMITTED, OrderStatus.PARTIALLY_FILLED}),
        }

    def emergency_kill(self, timestamp: datetime) -> list[ManagedOrder]:
        self.kill_switch = True
        orders = []
        for trade in self.trades.values():
            if trade.status == TradeStatus.OPEN:
                orders.append(self.flatten_trade(trade.trade_id, timestamp, "emergency_kill"))
                trade.status = TradeStatus.KILLED
        return orders

    def _trade_for_order(self, order_id: str) -> ManagedTrade | None:
        trade_id = self._order_to_trade.get(order_id)
        return self.trades.get(trade_id) if trade_id else None

    def _log_order(self, order: ManagedOrder) -> None:
        if self.logger:
            self.logger.csv("orders", order.__dict__)

    def _log_fill(self, order: ManagedOrder, qty: int, price: float, timestamp: datetime) -> None:
        if self.logger:
            self.logger.csv("fills", {"timestamp": timestamp, "order_id": order.order_id, "symbol": order.symbol, "qty": qty, "price": price})
