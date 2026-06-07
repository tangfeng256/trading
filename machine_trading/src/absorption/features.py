from __future__ import annotations

import math
from collections import defaultdict, deque
from datetime import datetime, timedelta
from statistics import pstdev
from typing import Any, Deque

from .config import StrategyConfig
from .depth_book import DepthBook
from .tape import Tape
from .utils_time import to_et


def clamp(value: float, low: float = 0.0, high: float = 1.0) -> float:
    return max(low, min(high, value))


class FeatureEngine:
    def __init__(self, strategy: StrategyConfig) -> None:
        self.strategy = strategy
        self.history: dict[str, Deque[dict[str, Any]]] = defaultdict(lambda: deque(maxlen=1200))

    def compute(self, symbol: str, now: datetime, book: DepthBook, tape: Tape) -> dict[str, Any]:
        mid = book.mid()
        spread = book.spread()
        micro = book.microprice()
        spread_bps = (spread / mid * 10_000.0) if spread is not None and mid else math.inf
        hist = self.history[symbol]
        cutoff_10s = now - timedelta(seconds=self.strategy.absorption_window_sec)
        recent = [row for row in hist if row["timestamp"] >= cutoff_10s]
        microprice_slope = 0.0
        if recent and micro is not None and recent[0].get("microprice") is not None:
            elapsed = max((now - recent[0]["timestamp"]).total_seconds(), 1e-9)
            microprice_slope = (micro - recent[0]["microprice"]) / elapsed

        bid_depth_5 = book.depth_sum("bid", 5)
        ask_depth_5 = book.depth_sum("ask", 5)
        delta_3s = tape.signed_delta(now, 3)
        delta_10s = tape.signed_delta(now, 10)
        sell_hit_count_3s = tape.aggressive_sell_count(now, 3)
        buy_lift_count_3s = tape.aggressive_buy_count(now, 3)
        trade_velocity_3s = tape.trade_velocity(now, 3)
        repl_since = now - timedelta(seconds=self.strategy.absorption_window_sec)
        bid_replenishment_rate = book.recent_replenishment(repl_since) / max(self.strategy.absorption_window_sec, 1)
        mids = [row["mid"] for row in recent if row.get("mid") is not None]
        price_progress_bps = 0.0
        if mid and mids:
            price_progress_bps = (mid - max(mids)) / max(mids) * 10_000.0
        spreads = [row["spread_bps"] for row in recent if math.isfinite(row.get("spread_bps", math.inf))]
        spread_stability_score = 1.0 if len(spreads) < 2 else clamp(1.0 - pstdev(spreads) / max(self.strategy.max_spread_bps, 1))
        stable_depth_score = 1.0
        bid_depths = [row["bid_depth_5"] for row in recent if row.get("bid_depth_5")]
        if bid_depths and bid_depth_5:
            stable_depth_score = clamp(bid_depth_5 / max(bid_depths))

        sell_volume_10s = tape.sell_volume(now, self.strategy.absorption_window_sec)
        buy_volume_5s = tape.buy_volume(now, self.strategy.exhaustion_window_sec)
        normalized_sell_pressure = clamp(sell_volume_10s / 2_000.0)
        normalized_bid_replenishment = clamp(bid_replenishment_rate / 100.0)
        no_downward_progress_score = clamp(1.0 + price_progress_bps / 10.0)
        absorption_score = (
            0.30 * normalized_sell_pressure
            + 0.25 * normalized_bid_replenishment
            + 0.20 * no_downward_progress_score
            + 0.15 * stable_depth_score
            + 0.10 * spread_stability_score
        )

        prior_end = now - timedelta(seconds=3)
        prior_start = prior_end - timedelta(seconds=self.strategy.exhaustion_window_sec)
        previous_sell_count = sum(1 for t in tape.window_between(prior_start, prior_end) if t.side == "sell")
        sell_slowdown = 1.0 if previous_sell_count == 0 else clamp(1.0 - sell_hit_count_3s / previous_sell_count)
        micro_rising = 1.0 if microprice_slope > 0 else 0.0
        ask_lifting = clamp(buy_lift_count_3s / max(sell_hit_count_3s + buy_lift_count_3s, 1))
        exhaustion_score = 0.40 * sell_slowdown + 0.25 * micro_rising + 0.20 * ask_lifting + 0.15 * spread_stability_score

        vwap_1m = tape.vwap(now, 60)
        vwap_3m = tape.vwap(now, 180)
        last = tape.last_price()
        micro_high = max([row["microprice"] for row in list(hist)[-10:] if row.get("microprice") is not None] or [micro or 0])
        vwap_recovery = 1.0 if last and vwap_1m and last >= vwap_1m else 0.0
        micro_breakout = 1.0 if micro is not None and micro >= micro_high else 0.0
        spread_ok = 1.0 if spread_bps <= self.strategy.max_spread_bps else 0.0
        trigger_score = 0.35 * micro_breakout + 0.30 * vwap_recovery + 0.20 * ask_lifting + 0.15 * spread_ok

        prices = [t.price for t in tape.window(now, 60)]
        realized_volatility = pstdev(prices) / (sum(prices) / len(prices)) if len(prices) >= 2 else 0.0
        row = {
            "timestamp": now,
            "symbol": symbol,
            "mid": mid,
            "spread": spread,
            "spread_bps": spread_bps,
            "microprice": micro,
            "microprice_slope": microprice_slope,
            "bid_depth_5": bid_depth_5,
            "ask_depth_5": ask_depth_5,
            "imbalance_5": book.imbalance(5),
            "delta_3s": delta_3s,
            "delta_10s": delta_10s,
            "sell_hit_count_3s": sell_hit_count_3s,
            "buy_lift_count_3s": buy_lift_count_3s,
            "trade_velocity_3s": trade_velocity_3s,
            "bid_replenishment_rate": bid_replenishment_rate,
            "price_progress_bps": price_progress_bps,
            "absorption_score": absorption_score,
            "exhaustion_score": exhaustion_score,
            "trigger_score": trigger_score,
            "vwap_1m": vwap_1m,
            "vwap_3m": vwap_3m,
            "realized_volatility": realized_volatility,
            "time_of_day": to_et(now).time().isoformat(),
            "last_price": last,
            "sell_volume_10s": sell_volume_10s,
            "buy_volume_5s": buy_volume_5s,
        }
        hist.append(row)
        return row
