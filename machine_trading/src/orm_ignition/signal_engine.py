from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, time
from typing import Dict, List, Optional

from .config import StrategyConfig
from .market_state import EASTERN, SymbolMarketState
from .scanner import Scanner


@dataclass(frozen=True)
class Signal:
    symbol: str
    timestamp: datetime
    entry_ref: float
    stop_ref: float
    target_ref: float
    confidence: float
    reason_codes: List[str]
    features: Dict[str, float | str | bool]


class SignalEngine:
    def __init__(self, config: StrategyConfig, scanner: Optional[Scanner] = None) -> None:
        self.config = config
        self.scanner = scanner or Scanner(config)

    def evaluate(
        self,
        state: SymbolMarketState,
        market_states: List[SymbolMarketState],
    ) -> tuple[Optional[Signal], Dict[str, float | str | bool]]:
        bar = state.last_bar
        now_et = bar.timestamp.astimezone(EASTERN).time() if bar else time(0, 0)
        base_features: Dict[str, float | str | bool] = {"symbol": state.symbol}

        if bar is None:
            return None, {"reason": "missing_bar", **base_features}
        if now_et < _parse_time(self.config.or_end):
            return None, {"reason": "opening_range_incomplete", **base_features}
        if now_et < _parse_time(self.config.trade_start) or now_et > _parse_time(self.config.trade_end):
            return None, {"reason": "outside_trade_window", **base_features}

        scan = self.scanner.scan(state, market_states)
        features = dict(scan.features)
        if not scan.passed:
            return None, {"reason": scan.reason, **features}

        opening_range = state.opening_range
        vwap = state.vwap
        if opening_range.high is None or opening_range.low is None or vwap is None:
            return None, {"reason": "inconsistent_scan_state", **features}

        bars = list(state.bars)
        recent = bars[-6:]
        prior = bars[-7:-1]
        pullback_low = min(bar.low for bar in recent)
        prior_micro_high = max((bar.high for bar in prior), default=opening_range.high)
        impulse_volume = max((bar.volume for bar in prior), default=1)
        pullback_volume = min((bar.volume for bar in recent[:-1]), default=bar.volume)
        rolling_volume = max(1.0, state.rolling_volume())
        tolerance = opening_range.high * self.config.or_reclaim_tolerance_bps / 10_000.0

        opening_drive = (
            bar.close > opening_range.high
            and opening_range.move_bps >= self.config.min_or_move_bps
            and impulse_volume >= rolling_volume
        )
        pullback_ok = (
            pullback_low >= opening_range.high - tolerance
            and pullback_low > opening_range.low
            and pullback_volume <= impulse_volume
        )
        volume_acceleration = bar.volume >= self.config.min_reignite_volume_mult * rolling_volume
        close_reclaim = bar.close > max(opening_range.high, prior_micro_high)
        close_near_high = bar.close_location >= self.config.close_location_min
        l2_ok = self._l2_ok(state)

        features.update(
            {
                "opening_range_high": opening_range.high,
                "opening_range_low": opening_range.low,
                "vwap": vwap,
                "pullback_low": pullback_low,
                "prior_micro_high": prior_micro_high,
                "impulse_volume": impulse_volume,
                "pullback_volume": pullback_volume,
                "rolling_volume": rolling_volume,
                "volume_acceleration": volume_acceleration,
                "close_location": bar.close_location,
                "l2_ok": l2_ok,
            }
        )

        failed = []
        if not opening_drive:
            failed.append("opening_drive_failed")
        if not pullback_ok:
            failed.append("pullback_failed")
        if not close_reclaim:
            failed.append("reclaim_failed")
        if bar.close <= vwap:
            failed.append("below_vwap")
        if not close_near_high:
            failed.append("weak_close_location")
        if not volume_acceleration:
            failed.append("weak_reignite_volume")
        if not l2_ok:
            failed.append("l2_failed")
        if failed:
            return None, {"reason": ",".join(failed), **features}

        atr = state.atr()
        stop_ref = min(pullback_low, vwap - atr * self.config.volatility_buffer_mult)
        entry_ref = state.quote.ask if state.quote else bar.close
        risk = entry_ref - stop_ref
        if risk <= 0:
            return None, {"reason": "stop_not_below_entry", **features}
        target_ref = entry_ref + risk * 2.0
        confidence = min(1.0, 0.55 + 0.1 * len([x for x in [opening_drive, pullback_ok, volume_acceleration, l2_ok] if x]))
        reasons = ["opening_drive", "pullback", "reignition", "above_vwap", "volume_acceleration"]
        return (
            Signal(state.symbol, bar.timestamp, entry_ref, stop_ref, target_ref, confidence, reasons, features),
            {"reason": "signal", **features},
        )

    def _l2_ok(self, state: SymbolMarketState) -> bool:
        if not self.config.use_l2 or state.book is None:
            return True
        return state.book.spread_stable and state.book.bid_support


def _parse_time(value: str) -> time:
    hour, minute = value.split(":")
    return time(int(hour), int(minute))
