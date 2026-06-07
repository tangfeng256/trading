from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import date, datetime

from .config import RiskConfig, StrategyConfig
from .order_state import Signal
from .utils_time import in_time_window


@dataclass(frozen=True)
class RiskDecision:
    approved: bool
    qty: int = 0
    reason: str = ""
    risk_dollars: float = 0.0
    stop_distance: float = 0.0


class RiskManager:
    def __init__(self, risk: RiskConfig, strategy: StrategyConfig) -> None:
        self.risk = risk
        self.strategy = strategy
        self.trades_by_day: dict[date, int] = {}
        self.realized_pnl_by_day: dict[date, float] = {}
        self.active_symbols: set[str] = set()
        self.kill_switch = False

    def approve(self, signal: Signal, existing_position_or_order: bool = False) -> RiskDecision:
        today = signal.timestamp.date()
        entry = signal.entry_ref_price
        stop = signal.stop_price
        if self.kill_switch:
            return RiskDecision(False, reason="kill_switch_enabled")
        if not in_time_window(signal.timestamp, self.strategy.trade_start, self.strategy.trade_end):
            return RiskDecision(False, reason="outside_trading_window")
        if self.realized_pnl_by_day.get(today, 0.0) <= -self.risk.account_equity * self.risk.max_daily_loss_pct:
            return RiskDecision(False, reason="daily_loss_exceeded")
        if self.trades_by_day.get(today, 0) >= self.risk.max_trades_per_day:
            return RiskDecision(False, reason="max_trades_per_day")
        if signal.symbol in self.active_symbols or existing_position_or_order:
            return RiskDecision(False, reason="existing_position_or_order")
        if signal.feature_snapshot.get("spread_bps", math.inf) > self.strategy.max_spread_bps:
            return RiskDecision(False, reason="spread_too_wide")
        if stop >= entry:
            return RiskDecision(False, reason="stop_not_below_entry")
        stop_distance = abs(entry - stop)
        stop_bps = stop_distance / entry * 10_000.0
        if stop_bps < self.risk.min_stop_bps:
            return RiskDecision(False, reason="stop_distance_too_small", stop_distance=stop_distance)
        if stop_bps > self.risk.max_stop_bps:
            return RiskDecision(False, reason="stop_distance_too_large", stop_distance=stop_distance)
        risk_dollars = self.risk.account_equity * self.risk.risk_per_trade_pct
        qty = math.floor(risk_dollars / stop_distance)
        qty = min(qty, math.floor(self.risk.max_notional / entry))
        if qty <= 0:
            return RiskDecision(False, reason="calculated_qty_non_positive", risk_dollars=risk_dollars, stop_distance=stop_distance)
        return RiskDecision(True, qty=qty, reason="approved", risk_dollars=risk_dollars, stop_distance=stop_distance)

    def mark_trade_opened(self, symbol: str, timestamp: datetime) -> None:
        self.active_symbols.add(symbol)
        self.trades_by_day[timestamp.date()] = self.trades_by_day.get(timestamp.date(), 0) + 1

    def mark_trade_closed(self, symbol: str, timestamp: datetime, realized_pnl: float) -> None:
        self.active_symbols.discard(symbol)
        self.realized_pnl_by_day[timestamp.date()] = self.realized_pnl_by_day.get(timestamp.date(), 0.0) + realized_pnl

    def emergency_kill(self) -> None:
        self.kill_switch = True
