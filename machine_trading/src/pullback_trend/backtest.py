from __future__ import annotations

from pathlib import Path

import pandas as pd

from .config import AppConfig
from .logger import AuditLogger
from .market_data import load_bars
from .models import OrderStatus, Quote, Side
from .orders import OrderManager
from .position_manager import PositionManager
from .risk import RiskManager
from .signal_engine import SignalEngine


def run_backtest(config: AppConfig, bars_path: str | Path | list[str | Path]) -> Path:
    bars = load_bars(bars_path)
    logger = AuditLogger(config.logging.base_dir, config.logging.run_id)
    risk = RiskManager(config.risk)
    orders = OrderManager(config.execution, logger)
    positions = PositionManager(config.strategy, config.execution, risk, orders, logger)
    engine = SignalEngine(config.strategy)
    histories: dict[str, pd.DataFrame] = {}
    trade_count = 0

    for _, row in bars.iterrows():
        symbol = str(row["symbol"])
        histories[symbol] = pd.concat([histories.get(symbol, pd.DataFrame()), pd.DataFrame([row])], ignore_index=True)
        now = row["timestamp"].to_pydatetime()
        market = histories.get(config.strategy.market_symbol)
        position = positions.position(symbol)

        trade_count += _simulate_exits(row, orders, positions)
        positions.reconcile_time_exits(now)

        if symbol == config.strategy.market_symbol or position.is_open:
            continue
        quote = Quote(symbol, now, float(row["close"]) * 0.9999, float(row["close"]) * 1.0001)
        signal, decision = engine.evaluate(symbol, histories[symbol], market, quote)
        logger.decision(timestamp=now.isoformat(), symbol=symbol, approved=signal is not None, reason=decision["reason"], score=decision.get("score", ""), features=decision.get("features", {}))
        if signal is None:
            continue
        risk_decision = risk.approve(signal, quote, open_positions=sum(1 for pos in positions.positions.values() if pos.is_open))
        if not risk_decision.approved:
            logger.decision(timestamp=now.isoformat(), symbol=symbol, approved=False, reason=risk_decision.reason, score=signal.score, features=risk_decision.features or {})
            continue
        orders.submit_entry(signal, risk_decision, position)

    open_positions = [pos for pos in positions.positions.values() if pos.is_open]
    logger.summary({"bars": len(bars), "trades": trade_count, "realized_pnl": risk.realized_pnl, "open_positions": len(open_positions)})
    logger.close()
    return logger.run_dir


def _simulate_exits(row: pd.Series, orders: OrderManager, positions: PositionManager) -> int:
    now = row["timestamp"].to_pydatetime()
    bar_open = float(row["open"])
    bar_high = float(row["high"])
    bar_low = float(row["low"])
    fills = 0
    for order in list(orders.orders.values()):
        if order.symbol != row["symbol"] or order.status != OrderStatus.WORKING:
            continue
        if order.side == Side.BUY and order.role == "entry":
            limit = order.limit_price
            if limit is None:
                continue
            if bar_open <= limit:
                # Bar opened at or below limit — fill at open (conservative, no better-than-limit assumption)
                _fill(order, now, order.quantity, bar_open, positions)
                fills += 1
            elif bar_low <= limit:
                _fill(order, now, order.quantity, limit, positions)
                fills += 1
        elif order.side == Side.SELL:
            if order.role in {"stop", "flatten"}:
                limit = order.limit_price
                if order.role == "flatten" or limit is None:
                    _fill(order, now, order.quantity, bar_open, positions)
                elif bar_open <= limit:
                    # Gap through stop — fill at open, not the stop price
                    _fill(order, now, order.quantity, bar_open, positions)
                elif bar_low <= limit:
                    _fill(order, now, order.quantity, limit, positions)
            elif order.limit_price is not None and bar_high >= order.limit_price:
                _fill(order, now, order.quantity, order.limit_price, positions)
    return fills


def _fill(order, now, quantity, price, positions: PositionManager) -> None:
    order.filled_quantity += quantity
    order.avg_fill_price = price
    order.status = OrderStatus.FILLED
    if order.side == Side.BUY:
        positions.on_entry_fill(order, now, quantity, price)
    else:
        positions.on_exit_fill(order, now, quantity, price)
