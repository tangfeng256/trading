from __future__ import annotations

from datetime import datetime, timedelta

from .config import ExecutionConfig, StrategyConfig
from .logger import AuditLogger
from .models import ManagedOrder, OrderStatus, Position, PositionState, Side
from .orders import OrderManager
from .risk import RiskManager
from .utils import as_eastern, parse_time


class PositionManager:
    def __init__(self, strategy: StrategyConfig, execution: ExecutionConfig, risk: RiskManager, orders: OrderManager, logger: AuditLogger) -> None:
        self.strategy = strategy
        self.execution = execution
        self.risk = risk
        self.orders = orders
        self.logger = logger
        self.positions: dict[str, Position] = {}

    def position(self, symbol: str) -> Position:
        return self.positions.setdefault(symbol, Position(symbol))

    def on_entry_fill(self, order: ManagedOrder, timestamp: datetime, quantity: int, price: float) -> None:
        position = self.position(order.symbol)
        total = position.avg_price * position.quantity + price * quantity
        position.quantity += quantity
        position.avg_price = total / position.quantity
        position.entry_time = position.entry_time or timestamp
        signal = position.last_signal
        if signal:
            risk = signal.entry_price - signal.stop_price
            position.stop_price = signal.stop_price
            position.tp1_price = signal.entry_price + risk * self.execution.tp1_r
            position.tp2_price = signal.entry_price + risk * self.execution.tp2_r
        position.state = PositionState.LONG_OPEN
        self.logger.trade(timestamp=timestamp.isoformat(), symbol=order.symbol, event="entry_fill", quantity=quantity, price=price, pnl="", reason="entry")
        self.logger.position(timestamp=timestamp.isoformat(), symbol=order.symbol, state=position.state.value, quantity=position.quantity, avg_price=position.avg_price, stop_price=position.stop_price, tp1_price=position.tp1_price, tp2_price=position.tp2_price, realized_pnl=position.realized_pnl)
        self.orders.submit_bracket(position, timestamp)

    def on_exit_fill(self, order: ManagedOrder, timestamp: datetime, quantity: int, price: float) -> None:
        position = self.position(order.symbol)
        pnl = (price - position.avg_price) * quantity
        position.realized_pnl += pnl
        position.quantity = max(0, position.quantity - quantity)
        if order.role == "tp1":
            position.tp1_filled = True
            position.state = PositionState.TP1_FILLED
            position.stop_price = position.avg_price * (1 + self.execution.breakeven_offset_bps / 10_000)
            position.bracket_submitted_qty = min(position.bracket_submitted_qty, position.quantity)
        if position.quantity == 0:
            self.risk.record_closed_trade(position.realized_pnl)
            position.state = PositionState.COOLDOWN
            position.bracket_parent_id = None
            position.bracket_submitted_qty = 0
        self.logger.trade(timestamp=timestamp.isoformat(), symbol=order.symbol, event=f"{order.role}_fill", quantity=quantity, price=price, pnl=pnl, reason=order.role)
        self.logger.position(timestamp=timestamp.isoformat(), symbol=order.symbol, state=position.state.value, quantity=position.quantity, avg_price=position.avg_price, stop_price=position.stop_price, tp1_price=position.tp1_price, tp2_price=position.tp2_price, realized_pnl=position.realized_pnl)

    def reconcile_time_exits(self, now: datetime) -> list[ManagedOrder]:
        exits = []
        self.orders.cancel_stale_entries(now)
        for position in self.positions.values():
            if not position.is_open or not position.entry_time:
                continue
            age = now - position.entry_time
            if age >= timedelta(minutes=self.strategy.max_hold_minutes):
                exits.append(self._flatten(position, now, "max_hold"))
            elif age >= timedelta(minutes=self.strategy.no_progress_minutes) and position.tp1_price and position.avg_price > 0:
                exits.append(self._flatten(position, now, "no_progress"))
            elif as_eastern(now).time() >= parse_time(self.strategy.regular_session_end):
                exits.append(self._flatten(position, now, "session_end"))
        return [order for order in exits if order is not None]

    def _flatten(self, position: Position, now: datetime, reason: str) -> ManagedOrder | None:
        if position.state == PositionState.EXITING or not position.is_open:
            return None
        order = ManagedOrder(self.orders._next_id("X"), position.symbol, Side.SELL, position.quantity, None, now, role="flatten", reason=reason)
        self.orders.orders[order.order_id] = order
        position.state = PositionState.EXITING
        self.logger.order(timestamp=now.isoformat(), symbol=position.symbol, event="flatten", order_id=order.order_id, side="SELL", quantity=order.quantity, price="", status=OrderStatus.WORKING.value, reason=reason, parent_id="")
        self.logger.position(timestamp=now.isoformat(), symbol=position.symbol, state=position.state.value, quantity=position.quantity, avg_price=position.avg_price, stop_price=position.stop_price, tp1_price=position.tp1_price, tp2_price=position.tp2_price, realized_pnl=position.realized_pnl)
        if self.orders.broker:
            self.orders.broker.flatten(order)
        return order
