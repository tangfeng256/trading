from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime


@dataclass(frozen=True)
class Bar:
    timestamp: datetime
    open: float
    high: float
    low: float
    close: float
    volume: float


class BarBuilder:
    def __init__(self) -> None:
        self.bars: list[Bar] = []

    def add_bar(self, bar: Bar) -> None:
        self.bars.append(bar)

    def recent_closes(self, limit: int) -> list[float]:
        return [bar.close for bar in self.bars[-limit:]]
