from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import date
from math import floor
from pathlib import Path

from .config import RiskConfig
from .models import Quote, Signal
from .utils import bps


@dataclass
class RiskDecision:
    approved: bool
    reason: str
    quantity: int = 0
    features: dict | None = None


class RiskManager:
    def __init__(self, config: RiskConfig, state_path: Path | None = None) -> None:
        self.config = config
        self.state_path = state_path
        self.realized_pnl = 0.0
        self.trades_today = 0
        self.consecutive_losses = 0
        self._last_reset_date: date | None = None
        if state_path and Path(state_path).exists():
            self._load_state()

    def _maybe_reset_for_today(self) -> None:
        today = date.today()
        if self._last_reset_date is None:
            self._last_reset_date = today
        elif self._last_reset_date != today:
            self.realized_pnl = 0.0
            self.trades_today = 0
            self.consecutive_losses = 0
            self._last_reset_date = today

    def can_open_new(self, open_positions: int = 0) -> tuple[bool, str]:
        self._maybe_reset_for_today()
        if self.realized_pnl <= -self.config.max_daily_loss_dollars:
            return False, "daily_loss_limit"
        if self.trades_today >= self.config.max_trades_per_day:
            return False, "max_trades_per_day"
        if self.consecutive_losses >= self.config.max_consecutive_losses:
            return False, "max_consecutive_losses"
        if open_positions >= self.config.max_open_positions:
            return False, "max_open_positions"
        return True, "ok"

    def approve(self, signal: Signal, quote: Quote | None = None, open_positions: int = 0) -> RiskDecision:
        ok, reason = self.can_open_new(open_positions)
        if not ok:
            return RiskDecision(False, reason)
        if signal.stop_price >= signal.entry_price:
            return RiskDecision(False, "stop_not_below_entry")
        risk_per_share = signal.risk_per_share
        stop_bps = bps(risk_per_share, signal.entry_price)
        if stop_bps < self.config.min_stop_bps:
            return RiskDecision(False, "stop_too_tight", features={"stop_bps": stop_bps})
        if stop_bps > self.config.max_stop_bps:
            return RiskDecision(False, "stop_too_wide", features={"stop_bps": stop_bps})
        quantity = floor(self.config.risk_dollars / risk_per_share)
        quantity = min(quantity, floor(self.config.max_position_notional / signal.entry_price))
        if quantity <= 0:
            return RiskDecision(False, "quantity_zero")
        if quote and quote.ask > signal.entry_price * (1 + self.config.entry_slippage_bps / 10_000):
            return RiskDecision(False, "entry_slippage_too_high")
        return RiskDecision(True, "approved", quantity, {"risk_per_share": risk_per_share, "stop_bps": stop_bps})

    def record_closed_trade(self, pnl: float) -> None:
        self._maybe_reset_for_today()
        self.realized_pnl += pnl
        self.trades_today += 1
        self.consecutive_losses = self.consecutive_losses + 1 if pnl < 0 else 0
        self._save_state()

    def _save_state(self) -> None:
        if not self.state_path:
            return
        Path(self.state_path).write_text(
            json.dumps({
                "date": str(self._last_reset_date or date.today()),
                "realized_pnl": self.realized_pnl,
                "trades_today": self.trades_today,
                "consecutive_losses": self.consecutive_losses,
            }),
            encoding="utf-8",
        )

    def _load_state(self) -> None:
        try:
            data = json.loads(Path(self.state_path).read_text(encoding="utf-8"))
            saved_date = date.fromisoformat(data["date"])
            if saved_date == date.today():
                self.realized_pnl = float(data.get("realized_pnl", 0.0))
                self.trades_today = int(data.get("trades_today", 0))
                self.consecutive_losses = int(data.get("consecutive_losses", 0))
                self._last_reset_date = saved_date
        except Exception:
            pass
