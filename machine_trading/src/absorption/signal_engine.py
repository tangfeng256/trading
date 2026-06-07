from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from .config import StrategyConfig
from .order_state import Signal


@dataclass
class SymbolSignalState:
    phase: str = "IDLE"
    absorption_level: float | None = None
    absorption_low: float | None = None
    absorption_time: datetime | None = None


class SignalEngine:
    def __init__(self, strategy: StrategyConfig, tick_size: float = 0.01) -> None:
        self.strategy = strategy
        self.tick_size = tick_size
        self.state: dict[str, SymbolSignalState] = {}

    def evaluate(self, symbol: str, features: dict) -> tuple[Signal | None, dict]:
        state = self.state.setdefault(symbol, SymbolSignalState())
        reasons: list[str] = []
        mid = features.get("mid")
        spread_bps = features.get("spread_bps", float("inf"))
        if mid is None:
            return None, self._decision(symbol, features, state.phase, False, "missing_mid")
        if spread_bps > self.strategy.max_spread_bps:
            state.phase = "IDLE"
            return None, self._decision(symbol, features, state.phase, False, "spread_too_wide")

        selling_pressure = (
            features.get("delta_10s", 0) < 0
            and features.get("sell_hit_count_3s", 0) >= 2
            and features.get("trade_velocity_3s", 0) > 0
        )
        absorption = (
            selling_pressure
            and features.get("absorption_score", 0) >= self.strategy.min_absorption_score
            and features.get("price_progress_bps", 0) > -8
        )
        if absorption:
            state.phase = "ABSORPTION"
            state.absorption_level = mid
            state.absorption_low = min(state.absorption_low or mid, mid)
            state.absorption_time = features["timestamp"]
            reasons.append("absorption_confirmed")
            return None, self._decision(symbol, features, state.phase, False, ",".join(reasons))

        if state.phase == "ABSORPTION":
            no_failure = state.absorption_low is None or mid >= state.absorption_low - self.strategy.min_breakout_ticks * self.tick_size
            exhausted = features.get("exhaustion_score", 0) >= self.strategy.min_exhaustion_score and no_failure
            if exhausted:
                state.phase = "EXHAUSTION"
                reasons.append("seller_exhaustion")
            elif not no_failure:
                state.phase = "IDLE"
                state.absorption_level = None
                state.absorption_low = None
                return None, self._decision(symbol, features, state.phase, False, "absorption_failed_price_kept_falling")
            else:
                return None, self._decision(symbol, features, state.phase, False, "waiting_for_exhaustion")

        if state.phase == "EXHAUSTION":
            ref = state.absorption_level or mid
            breakout = mid >= ref + self.strategy.min_breakout_ticks * self.tick_size
            vwap_ok = features.get("vwap_1m") is None or mid >= features.get("vwap_1m")
            trigger = features.get("trigger_score", 0) >= self.strategy.min_trigger_score and breakout and vwap_ok
            if not trigger:
                return None, self._decision(symbol, features, state.phase, False, "waiting_for_trigger")
            stop = (state.absorption_low or ref) - self.strategy.min_breakout_ticks * self.tick_size
            risk = max(mid - stop, self.tick_size)
            signal = Signal(
                symbol=symbol,
                timestamp=features["timestamp"],
                phase="TRIGGER",
                entry_ref_price=mid,
                absorption_level=ref,
                stop_price=stop,
                target1_price=mid + risk,
                target2_price=mid + 2 * risk,
                confidence=min(1.0, (features.get("absorption_score", 0) + features.get("exhaustion_score", 0) + features.get("trigger_score", 0)) / 3),
                reason_codes=["trigger_confirmed", *reasons],
                feature_snapshot=dict(features),
            )
            state.phase = "IDLE"
            state.absorption_level = None
            state.absorption_low = None
            return signal, self._decision(symbol, features, "TRIGGER", True, "trigger_confirmed")

        phase = "SELLING_PRESSURE" if selling_pressure else "IDLE"
        return None, self._decision(symbol, features, phase, False, "waiting_for_absorption")

    def _decision(self, symbol: str, f: dict, phase: str, passed: bool, reason: str) -> dict:
        return {
            "timestamp": f.get("timestamp"),
            "symbol": symbol,
            "phase": phase,
            "passed": passed,
            "reason": reason,
            "mid": f.get("mid"),
            "spread": f.get("spread"),
            "delta": f.get("delta_10s"),
            "absorption_score": f.get("absorption_score"),
            "exhaustion_score": f.get("exhaustion_score"),
            "trigger_score": f.get("trigger_score"),
        }
