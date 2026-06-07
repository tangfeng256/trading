from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Dict

from .config import RiskConfig
from .market_state import Quote
from .signal_engine import Signal


@dataclass(frozen=True)
class RiskDecision:
    approved: bool
    quantity: int = 0
    reason: str = "approved"
    features: Dict[str, float | int | str] | None = None


class RiskManager:
    def __init__(self, config: RiskConfig) -> None:
        self.config = config
        self.realized_pnl = 0.0
        self.trades_today = 0
        self.current_day: date | None = None

    def reset_if_new_day(self, today: date) -> None:
        if self.current_day != today:
            self.current_day = today
            self.realized_pnl = 0.0
            self.trades_today = 0

    def can_open_new(self) -> tuple[bool, str]:
        if Path(self.config.kill_switch_file).exists():
            return False, "kill_switch"
        if self.realized_pnl <= -self.config.max_daily_loss_dollars:
            return False, "daily_loss_limit"
        if self.trades_today >= self.config.max_trades_per_day:
            return False, "max_trades"
        return True, "ok"

    def approve(self, signal: Signal, quote: Quote | None, open_positions: int) -> RiskDecision:
        ok, reason = self.can_open_new()
        if not ok:
            return RiskDecision(False, reason=reason)
        if open_positions >= self.config.max_total_positions:
            return RiskDecision(False, reason="max_total_positions")
        if quote is not None and quote.spread_bps > self.config.max_slippage_bps * 2:
            return RiskDecision(False, reason="spread_too_wide")
        if signal.stop_ref >= signal.entry_ref:
            return RiskDecision(False, reason="stop_not_below_entry")

        stop_distance = signal.entry_ref - signal.stop_ref
        stop_bps = stop_distance / signal.entry_ref * 10_000.0
        features = {
            "entry": signal.entry_ref,
            "stop": signal.stop_ref,
            "stop_bps": stop_bps,
            "risk_dollars": self.config.risk_dollars,
        }
        if stop_bps < self.config.min_stop_bps:
            return RiskDecision(False, reason="stop_too_tight", features=features)
        if stop_bps > self.config.max_stop_bps:
            return RiskDecision(False, reason="stop_too_wide", features=features)

        risk_qty = int(self.config.risk_dollars / stop_distance)
        notional_qty = int(self.config.max_position_notional / signal.entry_ref)
        quantity = max(0, min(risk_qty, notional_qty))
        if quantity <= 0:
            return RiskDecision(False, reason="position_size_zero", features=features)
        features["quantity"] = quantity
        return RiskDecision(True, quantity=quantity, features=features)

    def record_closed_trade(self, pnl: float) -> None:
        self.realized_pnl += pnl
        self.trades_today += 1
