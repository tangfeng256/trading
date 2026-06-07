from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Deque, Literal

TradeSide = Literal["buy", "sell", "unknown"]


@dataclass(frozen=True)
class TradePrint:
    timestamp: datetime
    price: float
    size: float
    side: TradeSide
    bid: float | None = None
    ask: float | None = None


class Tape:
    def __init__(self, symbol: str, retention_sec: int = 300) -> None:
        self.symbol = symbol
        self.retention = timedelta(seconds=retention_sec)
        self.prints: Deque[TradePrint] = deque()
        self._last_price: float | None = None

    def infer_side(self, price: float, bid: float | None, ask: float | None) -> TradeSide:
        if bid is not None and price <= bid:
            return "sell"
        if ask is not None and price >= ask:
            return "buy"
        if self._last_price is not None:
            if price < self._last_price:
                return "sell"
            if price > self._last_price:
                return "buy"
        return "unknown"

    def add_trade(
        self,
        timestamp: datetime,
        price: float,
        size: float,
        bid: float | None = None,
        ask: float | None = None,
        side: TradeSide | None = None,
    ) -> TradePrint:
        inferred = side or self.infer_side(price, bid, ask)
        trade = TradePrint(timestamp, float(price), float(size), inferred, bid, ask)
        self.prints.append(trade)
        self._last_price = float(price)
        self.prune(timestamp)
        return trade

    def prune(self, now: datetime) -> None:
        cutoff = now - self.retention
        while self.prints and self.prints[0].timestamp < cutoff:
            self.prints.popleft()

    def window(self, now: datetime, window_sec: float) -> list[TradePrint]:
        cutoff = now - timedelta(seconds=window_sec)
        return [trade for trade in self.prints if trade.timestamp >= cutoff]

    def window_between(self, start: datetime, end: datetime) -> list[TradePrint]:
        return [t for t in self.prints if start <= t.timestamp < end]

    def signed_delta(self, now: datetime, window_sec: float) -> float:
        total = 0.0
        for trade in self.window(now, window_sec):
            if trade.side == "buy":
                total += trade.size
            elif trade.side == "sell":
                total -= trade.size
        return total

    def sell_volume(self, now: datetime, window_sec: float) -> float:
        return sum(t.size for t in self.window(now, window_sec) if t.side == "sell")

    def buy_volume(self, now: datetime, window_sec: float) -> float:
        return sum(t.size for t in self.window(now, window_sec) if t.side == "buy")

    def trade_velocity(self, now: datetime, window_sec: float) -> float:
        return len(self.window(now, window_sec)) / max(window_sec, 1e-9)

    def aggressive_sell_count(self, now: datetime, window_sec: float) -> int:
        return sum(1 for t in self.window(now, window_sec) if t.side == "sell")

    def aggressive_buy_count(self, now: datetime, window_sec: float) -> int:
        return sum(1 for t in self.window(now, window_sec) if t.side == "buy")

    def vwap(self, now: datetime, window_sec: float) -> float | None:
        trades = self.window(now, window_sec)
        volume = sum(t.size for t in trades)
        if volume <= 0:
            return None
        return sum(t.price * t.size for t in trades) / volume

    def last_price(self) -> float | None:
        return self.prints[-1].price if self.prints else None
