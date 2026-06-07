from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import Enum
from typing import Dict, Optional

from .config import RiskConfig, StrategyConfig
from .logger import AuditLogger
from .market_state import EASTERN, Quote
from .risk_manager import RiskDecision, RiskManager
from .signal_engine import Signal


class Side(str, Enum):
    BUY = "BUY"
    SELL = "SELL"


class OrderStatus(str, Enum):
    PENDING = "PENDING"
    PARTIAL = "PARTIAL"
    FILLED = "FILLED"
    CANCELLED = "CANCELLED"


@dataclass
class ManagedOrder:
    order_id: str
    symbol: str
    side: Side
    quantity: int
    price: float
    created_at: datetime
    status: OrderStatus = OrderStatus.PENDING
    filled_quantity: int = 0
    avg_fill_price: float = 0.0
    reason: str = ""

    @property
    def remaining(self) -> int:
        return max(0, self.quantity - self.filled_quantity)


@dataclass
class Position:
    symbol: str
    quantity: int = 0
    avg_price: float = 0.0
    entry_time: Optional[datetime] = None
    stop: Optional[float] = None
    target: Optional[float] = None
    tp1: Optional[float] = None
    tp1_done: bool = False
    bracket_submitted_qty: int = 0
    realized_pnl: float = 0.0

    @property
    def is_open(self) -> bool:
        return self.quantity > 0


class ExecutionManager:
    def __init__(
        self,
        risk: RiskManager,
        risk_config: RiskConfig,
        strategy_config: StrategyConfig,
        logger: AuditLogger,
        broker=None,
    ) -> None:
        self.risk = risk
        self.risk_config = risk_config
        self.strategy_config = strategy_config
        self.logger = logger
        self.broker = broker
        self.orders: Dict[str, ManagedOrder] = {}
        self.positions: Dict[str, Position] = {}
        self._order_seq = 1
        self._last_reconcile: Optional[datetime] = None

    def on_signal(self, signal: Signal, quote: Quote | None) -> Optional[ManagedOrder]:
        position = self.positions.get(signal.symbol)
        if position and position.is_open:
            self.logger.decision(signal.symbol, "execution", False, "position_already_open", signal.features)
            return None
        open_positions = sum(1 for pos in self.positions.values() if pos.is_open)
        decision = self.risk.approve(signal, quote, open_positions)
        self.logger.decision(signal.symbol, "risk", decision.approved, decision.reason, decision.features or {})
        if not decision.approved:
            return None
        return self.submit_entry(signal, decision)

    def submit_entry(self, signal: Signal, decision: RiskDecision) -> ManagedOrder:
        cap = signal.entry_ref * (1.0 + self.risk_config.max_slippage_bps / 10_000.0)
        order = ManagedOrder(
            order_id=self._next_id("E"),
            symbol=signal.symbol,
            side=Side.BUY,
            quantity=decision.quantity,
            price=round(cap, 4),
            created_at=signal.timestamp,
            reason="entry",
        )
        self.orders[order.order_id] = order
        self.logger.order(
            time=signal.timestamp.isoformat(),
            symbol=order.symbol,
            event="submit_entry",
            order_id=order.order_id,
            side=order.side.value,
            quantity=order.quantity,
            price=order.price,
            status=order.status.value,
            reason=order.reason,
        )
        if self.broker is not None:
            self.broker.submit_entry(order, signal)
        return order

    def on_fill(self, order_id: str, timestamp: datetime, quantity: int, price: float, commission: float = 0.0) -> None:
        if quantity <= 0:
            return
        if order_id not in self.orders:
            return
        order = self.orders[order_id]
        previous_filled = order.filled_quantity
        total_cost = order.avg_fill_price * previous_filled + price * quantity
        order.filled_quantity += quantity
        order.avg_fill_price = total_cost / order.filled_quantity
        order.status = OrderStatus.FILLED if order.remaining == 0 else OrderStatus.PARTIAL
        self.logger.fill(
            time=timestamp.isoformat(),
            symbol=order.symbol,
            order_id=order.order_id,
            side=order.side.value,
            quantity=quantity,
            price=price,
            commission=commission,
        )
        if order.side == Side.BUY:
            self._increase_position(order, timestamp, quantity, price)
        else:
            self._decrease_position(order, timestamp, quantity, price, commission)

    def _increase_position(self, order: ManagedOrder, timestamp: datetime, quantity: int, price: float) -> None:
        position = self.positions.setdefault(order.symbol, Position(order.symbol))
        total_cost = position.avg_price * position.quantity + price * quantity
        position.quantity += quantity
        position.avg_price = total_cost / position.quantity
        position.entry_time = position.entry_time or timestamp
        if position.bracket_submitted_qty < position.quantity:
            self.submit_protective_bracket(order.symbol, position.quantity - position.bracket_submitted_qty, position.avg_price)
        self._log_position(position, timestamp, "buy_fill")

    def _decrease_position(self, order: ManagedOrder, timestamp: datetime, quantity: int, price: float, commission: float) -> None:
        position = self.positions.setdefault(order.symbol, Position(order.symbol))
        realized = (price - position.avg_price) * quantity - commission
        position.realized_pnl += realized
        position.quantity = max(0, position.quantity - quantity)
        if position.quantity == 0:
            self.risk.record_closed_trade(position.realized_pnl)
            position.bracket_submitted_qty = 0
            position.tp1_done = False
            self.cancel_symbol_sells(order.symbol, timestamp, "position_closed")
        self._log_position(position, timestamp, "sell_fill")

    def submit_protective_bracket(self, symbol: str, quantity: int, entry_price: float) -> list[ManagedOrder]:
        position = self.positions[symbol]
        if quantity <= 0:
            return []
        if position.bracket_submitted_qty >= position.quantity:
            return []
        if position.stop is None:
            stop_distance = max(entry_price * self.risk_config.min_stop_bps / 10_000.0, 0.01)
            position.stop = entry_price - stop_distance
        risk = max(0.01, entry_price - position.stop)
        position.tp1 = position.tp1 or entry_price + risk * self.risk_config.tp1_r
        position.target = position.target or entry_price + risk * self.risk_config.tp2_r
        position.bracket_submitted_qty += quantity
        protective_orders = [
            ManagedOrder(
                order_id=self._next_id("S"),
                symbol=symbol,
                side=Side.SELL,
                quantity=quantity,
                price=round(position.stop, 4),
                created_at=position.entry_time,
                reason="protective_stop",
            ),
            ManagedOrder(
                order_id=self._next_id("T"),
                symbol=symbol,
                side=Side.SELL,
                quantity=quantity,
                price=round(position.target, 4),
                created_at=position.entry_time,
                reason="profit_target",
            ),
        ]
        for order in protective_orders:
            self.orders[order.order_id] = order
            self.logger.order(
                symbol=symbol,
                event="submit_protective_order",
                order_id=order.order_id,
                side=order.side.value,
                quantity=order.quantity,
                price=order.price,
                status="WORKING",
                reason=order.reason,
            )
        if self.broker is not None:
            self.broker.submit_bracket(symbol, protective_orders[0], protective_orders[1])
        return protective_orders

    def cancel_symbol_sells(self, symbol: str, now: datetime, reason: str) -> None:
        for order in list(self.orders.values()):
            if order.symbol != symbol or order.side != Side.SELL or order.status in {OrderStatus.FILLED, OrderStatus.CANCELLED}:
                continue
            self.cancel_order(order.order_id, now, reason)

    def reconcile(self, now: datetime) -> None:
        if self._last_reconcile and (now - self._last_reconcile).total_seconds() < 2:
            return
        self._last_reconcile = now
        self.risk.reset_if_new_day(now.date())
        for order in list(self.orders.values()):
            if order.status in {OrderStatus.FILLED, OrderStatus.CANCELLED}:
                continue
            if order.reason == "entry" and (now - order.created_at).total_seconds() >= self.risk_config.entry_stale_seconds:
                self.cancel_order(order.order_id, now, "stale_entry")
        for position in list(self.positions.values()):
            if not position.is_open or position.entry_time is None:
                continue
            if now - position.entry_time >= timedelta(minutes=self.strategy_config.max_hold_minutes):
                self.flatten(position.symbol, now, "max_hold")
            elif now.astimezone(EASTERN).time() >= _parse_time(self.strategy_config.trade_end):
                self.flatten(position.symbol, now, "trade_cutoff")

    def cancel_order(self, order_id: str, now: datetime, reason: str) -> None:
        if order_id not in self.orders:
            return
        order = self.orders[order_id]
        order.status = OrderStatus.CANCELLED
        self.logger.order(
            time=now.isoformat(),
            symbol=order.symbol,
            event="cancel",
            order_id=order.order_id,
            side=order.side.value,
            quantity=order.remaining,
            price=order.price,
            status=order.status.value,
            reason=reason,
        )
        if self.broker is not None:
            self.broker.cancel(order_id)

    def flatten(self, symbol: str, now: datetime, reason: str) -> Optional[ManagedOrder]:
        position = self.positions.get(symbol)
        if not position or not position.is_open:
            return None
        self.cancel_symbol_sells(symbol, now, f"{reason}_flatten")
        order = ManagedOrder(
            order_id=self._next_id("X"),
            symbol=symbol,
            side=Side.SELL,
            quantity=position.quantity,
            price=0.0,
            created_at=now,
            reason=reason,
        )
        self.orders[order.order_id] = order
        self.logger.order(
            time=now.isoformat(),
            symbol=symbol,
            event="flatten",
            order_id=order.order_id,
            side=order.side.value,
            quantity=order.quantity,
            price=order.price,
            status=order.status.value,
            reason=reason,
        )
        if self.broker is not None:
            self.broker.flatten(order)
        return order

    def _log_position(self, position: Position, timestamp: datetime, reason: str) -> None:
        self.logger.position(
            time=timestamp.isoformat(),
            symbol=position.symbol,
            quantity=position.quantity,
            avg_price=position.avg_price,
            stop=position.stop or "",
            target=position.target or "",
            realized_pnl=position.realized_pnl,
            reason=reason,
        )

    def _next_id(self, prefix: str) -> str:
        value = f"{prefix}{self._order_seq:06d}"
        self._order_seq += 1
        return value


def _parse_time(value: str):
    from datetime import time

    hour, minute = value.split(":")
    return time(int(hour), int(minute))
