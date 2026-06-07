from __future__ import annotations

import pandas as pd

from .config import StrategyConfig
from .models import L2Snapshot, Quote, Signal
from .pullback import continuation_trigger, detect_pullback
from .regime import market_regime_ok
from .trend import qualify_trend
from .utils import bps, in_time_window


class SignalEngine:
    def __init__(self, config: StrategyConfig) -> None:
        self.config = config

    def evaluate(
        self,
        symbol: str,
        history: pd.DataFrame,
        market_history: pd.DataFrame | None = None,
        quote: Quote | None = None,
        l2: L2Snapshot | None = None,
    ) -> tuple[Signal | None, dict]:
        if history.empty:
            return None, {"reason": "no_history"}
        row = history.iloc[-1]
        ts = row["timestamp"].to_pydatetime()
        if not any(in_time_window(ts, start, end) for start, end in self.config.trade_windows):
            return None, {"reason": "outside_trade_window"}
        if self.config.use_l2:
            if l2 is None:
                return None, {"reason": "missing_l2"}
            quote = l2.quote
            if quote is None:
                return None, {"reason": "invalid_l2_book"}
            l2_features = {
                "l2_bid_size": l2.bid_size,
                "l2_ask_size": l2.ask_size,
                "l2_total_size": l2.total_size,
                "l2_imbalance": round(l2.imbalance, 4),
            }
            if l2.total_size < self.config.min_l2_total_size:
                return None, {"reason": "l2_depth_too_thin", "features": l2_features}
            if l2.imbalance < self.config.min_l2_imbalance:
                return None, {"reason": "l2_imbalance_weak", "features": l2_features}
        else:
            l2_features = {}
        if quote and bps(quote.spread, quote.mid) > self.config.max_spread_bps:
            return None, {"reason": "spread_too_wide", "spread_bps": bps(quote.spread, quote.mid), "features": l2_features}
        if row["volume"] < self.config.min_volume:
            return None, {"reason": "low_volume"}

        market_row = market_history.iloc[-1] if market_history is not None and not market_history.empty else None
        regime_ok, regime_reason, regime_score = market_regime_ok(market_row, self.config)
        trend_ok, trend_reasons, trend_score = qualify_trend(history, self.config)
        pullback_ok, pullback_reasons, pullback_score, features = detect_pullback(history, self.config)
        trigger_ok, trigger_reason = continuation_trigger(history, self.config)

        total_score = 0.2 * regime_score + 0.3 * trend_score + 0.3 * pullback_score + (0.2 if trigger_ok else 0.0)
        reasons = [regime_reason] + trend_reasons + pullback_reasons + [trigger_reason]
        decision = {"score": round(total_score, 4), "reason": ";".join(reasons), "features": {**features, **l2_features}}
        if not (regime_ok and trend_ok and pullback_ok and trigger_ok and total_score >= self.config.min_score):
            return None, decision

        pullback_low = features["pullback_low"]
        atr_buffer = max(float(row["atr14"]) * 0.35, row["close"] * 0.0008)
        stop = pullback_low - atr_buffer
        entry = float(quote.ask if self.config.use_l2 and quote else row["close"])
        return Signal(symbol, ts, entry, stop, total_score, reasons, {**features, **l2_features, "trigger": trigger_reason}), decision
