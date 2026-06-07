from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Literal

from .utils_time import now_utc

Side = Literal["bid", "ask"]


@dataclass(frozen=True)
class DepthLevel:
    price: float
    size: float
    market_maker: str = ""


class DepthBook:
    """Local IBKR market depth ladder.

    IBKR operation codes:
    0 insert, 1 update, 2 delete. Inserts and deletes are positional and shift
    the ladder; updates replace the level at position.
    """

    INSERT = 0
    UPDATE = 1
    DELETE = 2

    def __init__(self, symbol: str, max_depth: int = 10, replenishment_retention_sec: int = 60) -> None:
        self.symbol = symbol
        self.max_depth = max_depth
        self.bids: list[DepthLevel] = []
        self.asks: list[DepthLevel] = []
        self.updated_at: datetime | None = None
        self.last_bid_sizes: dict[float, float] = {}
        self._replenishment_retention = timedelta(seconds=replenishment_retention_sec)
        self.replenishment_events: deque[tuple[datetime, float, float]] = deque()

    def _ladder(self, side: Side) -> list[DepthLevel]:
        return self.bids if side == "bid" else self.asks

    def apply_update(
        self,
        position: int,
        operation: int,
        side: Side,
        price: float,
        size: float,
        market_maker: str = "",
        timestamp: datetime | None = None,
    ) -> None:
        if position < 0:
            raise ValueError("position must be non-negative")
        effective_ts = timestamp or now_utc()
        ladder = self._ladder(side)
        level = DepthLevel(float(price), float(size), market_maker)
        if operation == self.INSERT:
            position = min(position, len(ladder))
            ladder.insert(position, level)
            del ladder[self.max_depth :]
        elif operation == self.UPDATE:
            if position < len(ladder):
                old = ladder[position]
                ladder[position] = level
                if side == "bid" and level.price == old.price and level.size > old.size:
                    self.replenishment_events.append((effective_ts, level.price, level.size - old.size))
            else:
                ladder.append(level)
                del ladder[self.max_depth :]
        elif operation == self.DELETE:
            if position < len(ladder):
                del ladder[position]
        else:
            raise ValueError(f"unknown depth operation: {operation}")
        if side == "bid":
            self.last_bid_sizes = {lvl.price: lvl.size for lvl in self.bids}
        self.updated_at = effective_ts
        self._prune_replenishment(effective_ts)

    def best_bid(self) -> DepthLevel | None:
        return self.bids[0] if self.bids else None

    def best_ask(self) -> DepthLevel | None:
        return self.asks[0] if self.asks else None

    def spread(self) -> float | None:
        bid, ask = self.best_bid(), self.best_ask()
        if not bid or not ask:
            return None
        return max(0.0, ask.price - bid.price)

    def mid(self) -> float | None:
        bid, ask = self.best_bid(), self.best_ask()
        if not bid or not ask:
            return None
        return (bid.price + ask.price) / 2.0

    def microprice(self) -> float | None:
        bid, ask = self.best_bid(), self.best_ask()
        if not bid or not ask or bid.size + ask.size <= 0:
            return self.mid()
        return (ask.price * bid.size + bid.price * ask.size) / (bid.size + ask.size)

    def depth_sum(self, side: Side, levels: int) -> float:
        return sum(level.size for level in self._ladder(side)[:levels])

    def imbalance(self, levels: int = 5) -> float:
        bid_depth = self.depth_sum("bid", levels)
        ask_depth = self.depth_sum("ask", levels)
        total = bid_depth + ask_depth
        return 0.0 if total <= 0 else (bid_depth - ask_depth) / total

    def recent_replenishment(self, since: datetime) -> float:
        return sum(size for ts, _price, size in self.replenishment_events if ts >= since)

    def _prune_replenishment(self, now: datetime) -> None:
        cutoff = now - self._replenishment_retention
        while self.replenishment_events and self.replenishment_events[0][0] < cutoff:
            self.replenishment_events.popleft()

    def snapshot(self) -> dict:
        return {
            "symbol": self.symbol,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
            "bids": [level.__dict__ for level in self.bids],
            "asks": [level.__dict__ for level in self.asks],
            "best_bid": self.best_bid().price if self.best_bid() else None,
            "best_ask": self.best_ask().price if self.best_ask() else None,
            "spread": self.spread(),
            "mid": self.mid(),
            "microprice": self.microprice(),
        }
