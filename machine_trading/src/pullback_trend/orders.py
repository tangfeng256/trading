from __future__ import annotations

from datetime import datetime

from .config import ExecutionConfig
from .logger import AuditLogger
from .models import ManagedOrder, OrderStatus, Position, PositionState, Side, Signal
from .risk import RiskDecision


class OrderManager:
    def __init__(self, config: ExecutionConfig, logger: AuditLogger, broker=None) -> None:
        self.config = config
        self.logger = logger
        self.broker = broker
        self.orders: dict[str, ManagedOrder] = {}
        self._seq = 1

    def submit_entry(self, signal: Signal, decision: RiskDecision, position: Position) -> ManagedOrder | None:
        if position.state in {PositionState.ENTRY_ORDER_WORKING, PositionState.LONG_OPEN, PositionState.TP1_FILLED}:
            return None
        limit_price = round(signal.entry_price * (1 + self.config.limit_offset_bps / 10_000), 4)
        order = ManagedOrder(self._next_id("E"), signal.symbol, Side.BUY, decision.quantity, limit_price, signal.timestamp, role="entry")
        self.orders[order.order_id] = order
        position.state = PositionState.ENTRY_ORDER_WORKING
        position.last_signal = signal
        self._log(order, "submit_entry", "entry")
        if self.broker:
            self.broker.submit_entry(order, signal)
        return order

    def submit_bracket(self, position: Position, timestamp: datetime) -> list[ManagedOrder]:
        if not position.is_open or position.bracket_submitted_qty >= position.quantity:
            return []
        qty = position.quantity - position.bracket_submitted_qty
        if qty <= 0 or position.stop_price is None or position.tp1_price is None or position.tp2_price is None:
            return []
        parent_id = position.bracket_parent_id or self._next_id("B")
        position.bracket_parent_id = parent_id
        stop = ManagedOrder(self._next_id("S"), position.symbol, Side.SELL, qty, position.stop_price, timestamp, parent_id=parent_id, role="stop")
        tp1_qty = max(1, int(round(qty * self.config.tp1_fraction)))
        tp1 = ManagedOrder(self._next_id("T"), position.symbol, Side.SELL, tp1_qty, position.tp1_price, timestamp, parent_id=parent_id, role="tp1")
        tp2 = ManagedOrder(self._next_id("T"), position.symbol, Side.SELL, qty - tp1_qty, position.tp2_price, timestamp, parent_id=parent_id, role="tp2")
        created = [order for order in (stop, tp1, tp2) if order.quantity > 0]
        for order in created:
            self.orders[order.order_id] = order
            self._log(order, "submit_protective_bracket", order.role)
        position.bracket_submitted_qty += qty
        if self.broker:
            self.broker.submit_bracket(position, created)
        return created

    def cancel_stale_entries(self, now: datetime) -> list[ManagedOrder]:
        cancelled = []
        for order in self.orders.values():
            if order.role == "entry" and order.status == OrderStatus.WORKING and (now - order.created_at).total_seconds() >= self.config.entry_stale_seconds:
                order.status = OrderStatus.CANCELLED
                cancelled.append(order)
                self._log(order, "cancel", "stale_entry")
                if self.broker:
                    self.broker.cancel(order.order_id)
        return cancelled

    def _log(self, order: ManagedOrder, event: str, reason: str) -> None:
        self.logger.order(
            timestamp=order.created_at.isoformat(),
            symbol=order.symbol,
            event=event,
            order_id=order.order_id,
            side=order.side.value,
            quantity=order.quantity,
            price=order.limit_price or "",
            status=order.status.value,
            reason=reason,
            parent_id=order.parent_id or "",
        )

    def _next_id(self, prefix: str) -> str:
        value = f"{prefix}{self._seq:06d}"
        self._seq += 1
        return value
