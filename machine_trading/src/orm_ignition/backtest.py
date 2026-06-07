from __future__ import annotations

import json
from pathlib import Path
from typing import Dict, List

import pandas as pd

from .config import AppConfig
from .execution_manager import ExecutionManager
from .logger import AuditLogger
from .market_state import Bar, MarketState, Quote
from .risk_manager import RiskManager
from .scanner import Scanner
from .signal_engine import SignalEngine


def run_backtest(config: AppConfig, bars_path: str | Path) -> Path:
    logger = AuditLogger(config.logging.base_dir, config.logging.run_id, config.logging.write_book_snapshots)
    market = MarketState(config.strategy.symbols, config.strategy.or_start, config.strategy.or_end)
    risk = RiskManager(config.risk)
    execution = ExecutionManager(risk, config.risk, config.strategy, logger)
    engine = SignalEngine(config.strategy, Scanner(config.strategy))

    df = pd.read_csv(bars_path)
    df["timestamp"] = pd.to_datetime(df["timestamp"], utc=True)
    trades: List[Dict] = []
    for row in df.sort_values("timestamp").itertuples(index=False):
        symbol = str(row.symbol).upper()
        if symbol not in market.symbols:
            continue
        bar = Bar(symbol, row.timestamp.to_pydatetime(), float(row.open), float(row.high), float(row.low), float(row.close), int(row.volume))
        market.on_bar(bar)
        market.on_quote(Quote(symbol, bar.timestamp, bar.close * 0.9999, bar.close * 1.0001))
        logger.bar(bar)
        state = market.state(symbol)
        signal, decision = engine.evaluate(state, list(market.symbols.values()))
        logger.decision(symbol, "signal", signal is not None, str(decision.get("reason", "")), decision)
        if signal:
            logger.signal(signal)
            order = execution.on_signal(signal, state.quote)
            if order and bar.low <= order.price:
                fill_price = min(order.price, bar.close) * (1.0 + config.risk.slippage_bps / 10_000.0)
                execution.on_fill(order.order_id, bar.timestamp, order.quantity, fill_price, order.quantity * config.risk.commission_per_share)
        execution.reconcile(bar.timestamp)

    summary = {
        "run_dir": str(logger.run_dir),
        "realized_pnl": risk.realized_pnl,
        "trades": risk.trades_today,
    }
    (logger.run_dir / "summary.json").write_text(json.dumps(summary, indent=2), encoding="ascii")
    pd.DataFrame(trades).to_csv(logger.run_dir / "trades.csv", index=False)
    return logger.run_dir
