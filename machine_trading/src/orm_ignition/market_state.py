from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, time
from typing import Deque, Dict, Iterable, List, Optional
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd


EASTERN = ZoneInfo("America/New_York")


@dataclass(frozen=True)
class Bar:
    symbol: str
    timestamp: datetime
    open: float
    high: float
    low: float
    close: float
    volume: int

    @property
    def close_location(self) -> float:
        span = self.high - self.low
        if span <= 0:
            return 1.0
        return (self.close - self.low) / span


@dataclass(frozen=True)
class Quote:
    symbol: str
    timestamp: datetime
    bid: float
    ask: float
    bid_size: int = 0
    ask_size: int = 0

    @property
    def mid(self) -> float:
        return (self.bid + self.ask) / 2.0

    @property
    def spread(self) -> float:
        return max(0.0, self.ask - self.bid)

    @property
    def spread_bps(self) -> float:
        if self.mid <= 0:
            return float("inf")
        return self.spread / self.mid * 10_000.0


@dataclass(frozen=True)
class BookSnapshot:
    symbol: str
    timestamp: datetime
    bids: tuple[tuple[float, int], ...] = ()
    asks: tuple[tuple[float, int], ...] = ()

    @property
    def spread_stable(self) -> bool:
        if not self.bids or not self.asks:
            return True
        bid = self.bids[0][0]
        ask = self.asks[0][0]
        return bid > 0 and ask > bid and (ask - bid) / ((ask + bid) / 2.0) * 10_000.0 <= 12.0

    @property
    def bid_support(self) -> bool:
        bid_size = sum(size for _, size in self.bids[:3])
        ask_size = sum(size for _, size in self.asks[:3])
        return bid_size >= ask_size * 0.75 if ask_size else True


@dataclass
class OpeningRange:
    high: Optional[float] = None
    low: Optional[float] = None
    start: str = "09:30"
    end: str = "09:45"
    complete: bool = False

    def update(self, bar: Bar) -> None:
        bar_time = bar.timestamp.astimezone(EASTERN).time()
        if _parse_time(self.start) <= bar_time < _parse_time(self.end):
            self.high = bar.high if self.high is None else max(self.high, bar.high)
            self.low = bar.low if self.low is None else min(self.low, bar.low)
        if bar_time >= _parse_time(self.end) and self.high is not None and self.low is not None:
            self.complete = True

    @property
    def move_bps(self) -> float:
        if self.high is None or self.low is None or self.low <= 0:
            return 0.0
        return (self.high - self.low) / self.low * 10_000.0


@dataclass
class SymbolMarketState:
    symbol: str
    or_start: str = "09:30"
    or_end: str = "09:45"
    bars: Deque[Bar] = field(default_factory=lambda: deque(maxlen=600))
    quote: Optional[Quote] = None
    book: Optional[BookSnapshot] = None
    opening_range: OpeningRange = field(init=False)
    cumulative_pv: float = 0.0
    cumulative_volume: int = 0

    def __post_init__(self) -> None:
        self.opening_range = OpeningRange(start=self.or_start, end=self.or_end)

    def on_bar(self, bar: Bar) -> None:
        if self.bars:
            last_date = self.bars[-1].timestamp.astimezone(EASTERN).date()
            bar_date = bar.timestamp.astimezone(EASTERN).date()
            if bar_date != last_date:
                self.cumulative_pv = 0.0
                self.cumulative_volume = 0
        self.bars.append(bar)
        self.cumulative_pv += bar.close * bar.volume
        self.cumulative_volume += bar.volume
        self.opening_range.update(bar)

    def on_quote(self, quote: Quote) -> None:
        self.quote = quote

    def on_book(self, book: BookSnapshot) -> None:
        self.book = book

    @property
    def last_bar(self) -> Optional[Bar]:
        return self.bars[-1] if self.bars else None

    @property
    def vwap(self) -> Optional[float]:
        if self.cumulative_volume <= 0:
            return None
        return self.cumulative_pv / self.cumulative_volume

    def rolling_volume(self, lookback: int = 20) -> float:
        sample = list(self.bars)[-lookback - 1 : -1]
        if not sample:
            return 0.0
        return float(np.mean([bar.volume for bar in sample]))

    def relative_volume(self, lookback: int = 20) -> float:
        avg = self.rolling_volume(lookback)
        if avg <= 0 or self.last_bar is None:
            return 0.0
        return self.last_bar.volume / avg

    def atr(self, lookback: int = 14) -> float:
        bars = list(self.bars)[-lookback - 1 :]
        if len(bars) < 2:
            return 0.0
        trs = []
        for prev, cur in zip(bars, bars[1:]):
            trs.append(max(cur.high - cur.low, abs(cur.high - prev.close), abs(cur.low - prev.close)))
        return float(np.mean(trs)) if trs else 0.0

    def realized_volatility(self, lookback: int = 20) -> float:
        closes = [bar.close for bar in list(self.bars)[-lookback:] if bar.close > 0]
        if len(closes) < 2:
            return 0.0
        returns = np.diff(np.log(closes))
        return float(np.std(returns))

    def frame(self) -> pd.DataFrame:
        return pd.DataFrame([bar.__dict__ for bar in self.bars])


class MarketState:
    def __init__(self, symbols: Iterable[str], or_start: str = "09:30", or_end: str = "09:45") -> None:
        self.symbols: Dict[str, SymbolMarketState] = {
            symbol: SymbolMarketState(symbol, or_start=or_start, or_end=or_end)
            for symbol in symbols
        }

    def state(self, symbol: str) -> SymbolMarketState:
        return self.symbols[symbol]

    def on_bar(self, bar: Bar) -> None:
        self.state(bar.symbol).on_bar(bar)

    def on_quote(self, quote: Quote) -> None:
        self.state(quote.symbol).on_quote(quote)

    def on_book(self, book: BookSnapshot) -> None:
        self.state(book.symbol).on_book(book)


class BarAggregator:
    def __init__(self, symbol: str) -> None:
        self.symbol = symbol
        self.current_minute: Optional[datetime] = None
        self.open = self.high = self.low = self.close = 0.0
        self.volume = 0

    def on_trade(self, timestamp: datetime, price: float, size: int) -> Optional[Bar]:
        minute = timestamp.replace(second=0, microsecond=0)
        if self.current_minute is None:
            self._start(minute, price, size)
            return None
        if minute != self.current_minute:
            finished = self.current_bar()
            self._start(minute, price, size)
            return finished
        self.high = max(self.high, price)
        self.low = min(self.low, price)
        self.close = price
        self.volume += int(size)
        return None

    def current_bar(self) -> Bar:
        if self.current_minute is None:
            raise RuntimeError("No active bar.")
        return Bar(self.symbol, self.current_minute, self.open, self.high, self.low, self.close, self.volume)

    def _start(self, minute: datetime, price: float, size: int) -> None:
        self.current_minute = minute
        self.open = self.high = self.low = self.close = float(price)
        self.volume = int(size)


def _parse_time(value: str) -> time:
    hour, minute = value.split(":")
    return time(int(hour), int(minute))
