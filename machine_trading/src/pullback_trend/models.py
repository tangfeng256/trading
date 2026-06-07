from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any


@dataclass(frozen=True)
class Bar:
    symbol: str
    timestamp: datetime
    open: float
    high: float
    low: float
    close: float
    volume: int
    vwap: float | None = None


@dataclass(frozen=True)
class Quote:
    symbol: str
    timestamp: datetime
    bid: float
    ask: float

    @property
    def mid(self) -> float:
        return (self.bid + self.ask) / 2.0

    @property
    def spread(self) -> float:
        return max(0.0, self.ask - self.bid)


@dataclass(frozen=True)
class DepthLevel:
    price: float
    size: float
    market_maker: str = ""


@dataclass(frozen=True)
class L2Snapshot:
    symbol: str
    timestamp: datetime
    bids: list[DepthLevel]
    asks: list[DepthLevel]

    @property
    def best_bid(self) -> float:
        return self.bids[0].price if self.bids else 0.0

    @property
    def best_ask(self) -> float:
        return self.asks[0].price if self.asks else 0.0

    @property
    def bid_size(self) -> float:
        return sum(level.size for level in self.bids)

    @property
    def ask_size(self) -> float:
        return sum(level.size for level in self.asks)

    @property
    def total_size(self) -> float:
        return self.bid_size + self.ask_size

    @property
    def imbalance(self) -> float:
        return 0.0 if self.total_size <= 0 else (self.bid_size - self.ask_size) / self.total_size

    @property
    def quote(self) -> Quote | None:
        if self.best_bid <= 0 or self.best_ask <= 0 or self.best_bid >= self.best_ask:
            return None
        return Quote(self.symbol, self.timestamp, self.best_bid, self.best_ask)


@dataclass
class Signal:
    symbol: str
    timestamp: datetime
    entry_price: float
    stop_price: float
    score: float
    reasons: list[str]
    features: dict[str, Any] = field(default_factory=dict)

    @property
    def risk_per_share(self) -> float:
        return self.entry_price - self.stop_price


class PositionState(str, Enum):
    FLAT = "FLAT"
    SIGNAL_DETECTED = "SIGNAL_DETECTED"
    ENTRY_ORDER_WORKING = "ENTRY_ORDER_WORKING"
    LONG_OPEN = "LONG_OPEN"
    TP1_FILLED = "TP1_FILLED"
    EXITING = "EXITING"
    COOLDOWN = "COOLDOWN"
    ERROR = "ERROR"


class Side(str, Enum):
    BUY = "BUY"
    SELL = "SELL"


class OrderStatus(str, Enum):
    WORKING = "WORKING"
    PARTIAL = "PARTIAL"
    FILLED = "FILLED"
    CANCELLED = "CANCELLED"
    REJECTED = "REJECTED"


@dataclass
class ManagedOrder:
    order_id: str
    symbol: str
    side: Side
    quantity: int
    limit_price: float | None
    created_at: datetime
    status: OrderStatus = OrderStatus.WORKING
    filled_quantity: int = 0
    avg_fill_price: float = 0.0
    parent_id: str | None = None
    role: str = "entry"
    reason: str = ""

    @property
    def remaining(self) -> int:
        return max(0, self.quantity - self.filled_quantity)


@dataclass
class Position:
    symbol: str
    state: PositionState = PositionState.FLAT
    quantity: int = 0
    avg_price: float = 0.0
    entry_time: datetime | None = None
    stop_price: float | None = None
    tp1_price: float | None = None
    tp2_price: float | None = None
    tp1_filled: bool = False
    bracket_parent_id: str | None = None
    bracket_submitted_qty: int = 0
    realized_pnl: float = 0.0
    last_signal: Signal | None = None

    @property
    def is_open(self) -> bool:
        return self.quantity > 0
