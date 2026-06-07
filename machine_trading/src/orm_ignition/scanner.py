from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, Iterable, Tuple

from .config import StrategyConfig
from .market_state import EASTERN, SymbolMarketState


@dataclass(frozen=True)
class ScanResult:
    passed: bool
    reason: str
    features: Dict[str, float | str | bool]


class Scanner:
    def __init__(self, config: StrategyConfig) -> None:
        self.config = config

    def scan(
        self,
        state: SymbolMarketState,
        market_states: Iterable[SymbolMarketState] = (),
    ) -> ScanResult:
        bar = state.last_bar
        quote = state.quote
        vwap = state.vwap
        or_state = state.opening_range
        features = {
            "symbol": state.symbol,
            "close": bar.close if bar else 0.0,
            "spread_bps": quote.spread_bps if quote else float("inf"),
            "volume": bar.volume if bar else 0,
            "relative_volume": state.relative_volume(),
            "or_move_bps": or_state.move_bps,
            "above_vwap": bool(bar and vwap is not None and bar.close > vwap),
            "market_risk_on": self._market_risk_on(market_states),
        }

        if bar is None:
            return ScanResult(False, "missing_bar", features)
        if quote is None:
            return ScanResult(False, "missing_quote", features)
        if bar.close <= 20:
            return ScanResult(False, "price_too_low", features)
        if quote.spread_bps > self.config.max_spread_bps:
            return ScanResult(False, "spread_too_wide", features)
        if bar.volume < self.config.min_volume:
            return ScanResult(False, "volume_too_low", features)
        if state.relative_volume() < self.config.min_rel_volume:
            return ScanResult(False, "relative_volume_too_low", features)
        if not or_state.complete:
            return ScanResult(False, "opening_range_incomplete", features)
        if or_state.move_bps < self.config.min_or_move_bps:
            return ScanResult(False, "opening_range_move_too_small", features)
        if vwap is None or bar.close <= vwap:
            return ScanResult(False, "below_vwap", features)
        if not features["market_risk_on"]:
            return ScanResult(False, "market_filter_negative", features)
        return ScanResult(True, "passed", features)

    def _market_risk_on(self, market_states: Iterable[SymbolMarketState]) -> bool:
        for state in market_states:
            if state.symbol not in self.config.market_symbols or state.last_bar is None:
                continue
            today = state.last_bar.timestamp.astimezone(EASTERN).date()
            today_bars = [b for b in state.bars if b.timestamp.astimezone(EASTERN).date() == today]
            if len(today_bars) < 2:
                continue
            first = today_bars[0].open
            last = today_bars[-1].close
            if first > 0 and (last - first) / first * 10_000.0 <= self.config.market_negative_bps:
                return False
        return True
