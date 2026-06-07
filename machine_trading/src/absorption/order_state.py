from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any


class OrderStatus(str, Enum):
    PENDING_SUBMIT = "PENDING_SUBMIT"
    SUBMITTED = "SUBMITTED"
    PARTIALLY_FILLED = "PARTIALLY_FILLED"
    FILLED = "FILLED"
    CANCELLED = "CANCELLED"
    REJECTED = "REJECTED"


class TradeStatus(str, Enum):
    PENDING_ENTRY = "PENDING_ENTRY"
    OPEN = "OPEN"
    EXITING = "EXITING"
    CLOSED = "CLOSED"
    KILLED = "KILLED"


@dataclass
class ManagedOrder:
    order_id: str
    symbol: str
    side: str
    qty: int
    order_type: str
    price: float | None = None
    stop_price: float | None = None
    role: str = "entry"
    status: OrderStatus = OrderStatus.PENDING_SUBMIT
    filled_qty: int = 0
    avg_fill_price: float = 0.0
    created_at: datetime | None = None
    parent_id: str | None = None
    meta: dict[str, Any] = field(default_factory=dict)

    @property
    def remaining_qty(self) -> int:
        return max(0, self.qty - self.filled_qty)


@dataclass
class ManagedTrade:
    trade_id: str
    symbol: str
    entry_order_id: str
    entry_price: float
    stop_price: float
    target1_price: float
    target2_price: float
    qty: int
    opened_at: datetime
    status: TradeStatus = TradeStatus.PENDING_ENTRY
    filled_qty: int = 0
    realized_pnl: float = 0.0
    protection_qty: int = 0
    tp1_created: bool = False
    tp2_created: bool = False
    stop_created: bool = False
    tp1_filled: bool = False
    orders: dict[str, ManagedOrder] = field(default_factory=dict)

    @property
    def is_active(self) -> bool:
        return self.status not in {TradeStatus.CLOSED, TradeStatus.KILLED}


@dataclass(frozen=True)
class Signal:
    symbol: str
    timestamp: datetime
    phase: str
    entry_ref_price: float
    absorption_level: float
    stop_price: float
    target1_price: float
    target2_price: float
    confidence: float
    reason_codes: list[str]
    feature_snapshot: dict[str, Any]
