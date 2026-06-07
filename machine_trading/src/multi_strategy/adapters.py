from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pandas as pd

from .broker import SharedBroker
from .logger import MultiStrategyLogger
from .paths import add_strategy_paths
from .registry import PositionRegistry

add_strategy_paths()

from absorption.config import load_config as load_absorption_config  # noqa: E402
from absorption.depth_book import DepthBook  # noqa: E402
from absorption.execution_manager import ExecutionManager as AbsExecutionManager  # noqa: E402
from absorption.features import FeatureEngine as AbsFeatureEngine  # noqa: E402
from absorption.risk_manager import RiskManager as AbsRiskManager  # noqa: E402
from absorption.signal_engine import SignalEngine as AbsSignalEngine  # noqa: E402
from absorption.tape import Tape  # noqa: E402
from pullback_trend.config import load_config as load_pullback_config  # noqa: E402
from pullback_trend.execution import _append_bar as pullback_append_bar  # noqa: E402
from pullback_trend.logger import AuditLogger as PullbackLogger  # noqa: E402
from pullback_trend.models import DepthLevel as PullbackDepthLevel  # noqa: E402
from pullback_trend.models import L2Snapshot as PullbackL2Snapshot  # noqa: E402
from pullback_trend.models import Quote as PullbackQuote  # noqa: E402
from pullback_trend.models import Side as PullbackSide  # noqa: E402
from pullback_trend.orders import OrderManager as PullbackOrderManager  # noqa: E402
from pullback_trend.position_manager import PositionManager as PullbackPositionManager  # noqa: E402
from pullback_trend.risk import RiskManager as PullbackRiskManager  # noqa: E402
from pullback_trend.signal_engine import SignalEngine as PullbackSignalEngine  # noqa: E402
from orm_ignition.config import load_config as load_orm_config  # noqa: E402
from orm_ignition.execution_manager import ExecutionManager as OrmExecutionManager  # noqa: E402
from orm_ignition.market_state import Bar as OrmBar  # noqa: E402
from orm_ignition.market_state import BookSnapshot as OrmBookSnapshot  # noqa: E402
from orm_ignition.market_state import MarketState as OrmMarketState  # noqa: E402
from orm_ignition.market_state import Quote as OrmQuote  # noqa: E402
from orm_ignition.risk_manager import RiskManager as OrmRiskManager  # noqa: E402
from orm_ignition.scanner import Scanner as OrmScanner  # noqa: E402
from orm_ignition.signal_engine import SignalEngine as OrmSignalEngine  # noqa: E402
from orm_ignition.logger import AuditLogger as OrmLogger  # noqa: E402


ABSORPTION_TP1_R_MULTIPLE = 1.5
ABSORPTION_TP2_R_MULTIPLE = 3.0
ABSORPTION_TP1_FRACTION = 0.33
ABSORPTION_MIN_STOP_BPS = 25.0
ABSORPTION_VOL_STOP_MULTIPLE = 0.6
ABSORPTION_MIN_STOP_DOLLARS = 0.35


class StrategyAdapter:
    strategy_name: str

    def symbols(self) -> set[str]:
        return set()

    def on_quote(self, symbol: str, timestamp: datetime, bid: float, ask: float, bid_size: int = 0, ask_size: int = 0) -> None:
        pass

    def on_depth(self, symbol: str, timestamp: datetime, bids: list[tuple[float, int]], asks: list[tuple[float, int]]) -> None:
        pass

    def on_depth_tick(self, symbol: str, tick: Any) -> None:
        pass

    def on_trade(self, symbol: str, timestamp: datetime, price: float, size: int, bid: float | None, ask: float | None) -> None:
        pass

    def on_bar(self, symbol: str, bar: Any, allow_new_entries: bool = True) -> None:
        pass

    def poll(self, now: datetime, allow_new_entries: bool = True) -> None:
        pass


class AbsorptionAdapter(StrategyAdapter):
    strategy_name = "absorption"

    def __init__(self, config_path: str | Path, symbols: list[str], broker: SharedBroker, registry: PositionRegistry, logger: MultiStrategyLogger) -> None:
        self.config = load_absorption_config(config_path)
        self.config = self.config.__class__(
            ib=self.config.ib,
            symbols=symbols,
            market=self.config.market,
            strategy=self.config.strategy,
            risk=self.config.risk,
            execution=replace(self.config.execution, tp1_fraction=ABSORPTION_TP1_FRACTION),
            logging=self.config.logging,
        )
        self.broker = broker
        self.registry = registry
        self.logger = logger
        self.books = {symbol: DepthBook(symbol, self.config.market.depth_rows) for symbol in symbols}
        self.tapes = {symbol: Tape(symbol) for symbol in symbols}
        self.features = AbsFeatureEngine(self.config.strategy)
        self.signals = AbsSignalEngine(self.config.strategy, self.config.market.tick_size)
        self.risk = AbsRiskManager(self.config.risk, self.config.strategy)
        self.execution = AbsExecutionManager(self.config.execution, self.config.market, self.config.strategy)
        self.last_feature_at: dict[str, datetime] = {}

    def symbols(self) -> set[str]:
        return set(self.books)

    def on_depth_tick(self, symbol: str, tick: Any) -> None:
        if symbol not in self.books:
            return
        side = {0: "ask", 1: "bid"}.get(getattr(tick, "side", None))
        if side is None:
            return
        self.books[symbol].apply_update(
            int(getattr(tick, "position")),
            int(getattr(tick, "operation")),
            side,
            float(getattr(tick, "price")),
            float(getattr(tick, "size")),
            str(getattr(tick, "marketMaker", "") or ""),
            timestamp=getattr(tick, "time", None) or datetime.now(timezone.utc),
        )

    def on_trade(self, symbol: str, timestamp: datetime, price: float, size: int, bid: float | None, ask: float | None) -> None:
        if symbol in self.tapes:
            self.tapes[symbol].add_trade(timestamp, price, size, bid=bid, ask=ask)

    def poll(self, now: datetime, allow_new_entries: bool = True) -> None:
        for order in self.execution.cancel_stale_entries(now):
            self.broker.cancel(order.order_id)
        for order in self.execution.flatten_expired_positions(now):
            self.broker.submit_absorption_order(self, order)
        if not allow_new_entries:
            return
        for symbol in self.config.symbols:
            if self.broker.is_symbol_cooling_down(symbol, now):
                self.logger.event("strategy_skip_cooldown", {"strategy": self.strategy_name, "symbol": symbol, "time": now.isoformat()})
                continue
            if not self.registry.is_available(symbol, self.strategy_name):
                self.logger.event("strategy_skip_locked", {"strategy": self.strategy_name, "symbol": symbol, "owner": self.registry.owner(symbol)})
                continue
            if self.registry.owner(symbol) == self.strategy_name:
                continue  # entry order in flight or position already open
            previous = self.last_feature_at.get(symbol)
            if previous and (now - previous).total_seconds() * 1000 < self.config.strategy.feature_interval_ms:
                continue
            self.last_feature_at[symbol] = now
            features = self.features.compute(symbol, now, self.books[symbol], self.tapes[symbol])
            signal, decision = self.signals.evaluate(symbol, features)
            self.logger.csv("decisions", {"strategy": self.strategy_name, "symbol": symbol, **decision})
            if signal is None:
                continue
            signal = self._widen_exit_plan(signal, features)
            risk_decision = self.risk.approve(signal, existing_position_or_order=False)
            if not risk_decision.approved:
                self.logger.event("risk_reject", {"strategy": self.strategy_name, "symbol": symbol, "reason": risk_decision.reason})
                continue
            trade = self.execution.submit_entry(signal, risk_decision.qty)
            self.risk.mark_trade_opened(symbol, signal.timestamp)
            for order in trade.orders.values():
                self.broker.submit_absorption_order(self, order)

    def on_broker_fill(self, order_id: str, timestamp: datetime, quantity: int, price: float, commission: float = 0.0) -> None:
        if order_id not in self.execution.orders:
            return
        created = self.execution.on_fill(order_id, quantity, price, timestamp)
        order = self.execution.orders[order_id]
        if order.side == "BUY":
            self.registry.lock_position(order.symbol, self.strategy_name, timestamp)
        for child in created:
            self.broker.submit_absorption_order(self, child)
        _get_trade = getattr(self.execution, "_trade_for_order", None)
        trade = _get_trade(order_id) if _get_trade else None
        if trade and not trade.is_active:
            self.risk.mark_trade_closed(trade.symbol, timestamp, trade.realized_pnl)
            self.registry.unlock_if_owner(trade.symbol, self.strategy_name)

    def _widen_exit_plan(self, signal: Any, features: dict[str, Any]) -> Any:
        entry = float(signal.entry_ref_price)
        tick_size = float(self.config.market.tick_size)
        structure_distance = max(entry - float(signal.stop_price), tick_size)
        bps_floor = entry * ABSORPTION_MIN_STOP_BPS / 10_000.0
        realized_vol = max(0.0, float(features.get("realized_volatility", 0.0) or 0.0))
        volatility_floor = entry * realized_vol * ABSORPTION_VOL_STOP_MULTIPLE
        stop_distance = max(structure_distance, bps_floor, volatility_floor, ABSORPTION_MIN_STOP_DOLLARS, tick_size)
        new_stop = round(entry - stop_distance, 4)
        new_tp1 = round(entry + stop_distance * ABSORPTION_TP1_R_MULTIPLE, 4)
        new_tp2 = round(entry + stop_distance * ABSORPTION_TP2_R_MULTIPLE, 4)
        if new_stop == signal.stop_price and new_tp1 == signal.target1_price and new_tp2 == signal.target2_price:
            return signal
        self.logger.event(
            "absorption_exit_plan_adjusted",
            {
                "strategy": self.strategy_name,
                "symbol": signal.symbol,
                "entry_ref_price": entry,
                "old_stop": signal.stop_price,
                "old_tp1": signal.target1_price,
                "old_tp2": signal.target2_price,
                "new_stop": new_stop,
                "new_tp1": new_tp1,
                "new_tp2": new_tp2,
                "stop_distance": round(stop_distance, 4),
                "structure_distance": round(structure_distance, 4),
                "bps_floor": round(bps_floor, 4),
                "volatility_floor": round(volatility_floor, 4),
            },
        )
        return replace(signal, stop_price=new_stop, target1_price=new_tp1, target2_price=new_tp2)


class PullbackBrokerAdapter:
    def __init__(self, owner: "PullbackAdapter", shared: SharedBroker) -> None:
        self.owner = owner
        self.shared = shared

    def submit_entry(self, order: Any, signal: Any) -> None:
        self.shared.submit_pullback_entry(self.owner, order, signal)

    def submit_bracket(self, position: Any, orders: list[Any]) -> None:
        self.shared.submit_pullback_bracket(self.owner, position, orders)

    def flatten(self, order: Any) -> None:
        self.shared.flatten_pullback(self.owner, order)

    def cancel(self, order_id: str) -> None:
        self.shared.cancel(order_id)


class PullbackAdapter(StrategyAdapter):
    strategy_name = "pullback"

    def __init__(self, config_path: str | Path, symbols: list[str], broker: SharedBroker, registry: PositionRegistry, logger: MultiStrategyLogger) -> None:
        self.config = load_pullback_config(config_path)
        self.config.strategy.symbols = symbols
        self.broker = broker
        self.registry = registry
        self.logger = logger
        self.audit = PullbackLogger(Path(logger.run_dir) / self.strategy_name)
        self.risk = PullbackRiskManager(self.config.risk)
        self.orders = PullbackOrderManager(self.config.execution, self.audit, broker=PullbackBrokerAdapter(self, broker))
        self.positions = PullbackPositionManager(self.config.strategy, self.config.execution, self.risk, self.orders, self.audit)
        self.engine = PullbackSignalEngine(self.config.strategy)
        self.histories: dict[str, pd.DataFrame] = {}
        self.quotes: dict[str, PullbackQuote] = {}
        self.l2: dict[str, PullbackL2Snapshot] = {}

    def symbols(self) -> set[str]:
        return set(self.config.strategy.symbols) | {self.config.strategy.market_symbol}

    def on_quote(self, symbol: str, timestamp: datetime, bid: float, ask: float, bid_size: int = 0, ask_size: int = 0) -> None:
        self.quotes[symbol] = PullbackQuote(symbol, timestamp, bid, ask)

    def on_depth(self, symbol: str, timestamp: datetime, bids: list[tuple[float, int]], asks: list[tuple[float, int]]) -> None:
        self.l2[symbol] = PullbackL2Snapshot(
            symbol,
            timestamp,
            [PullbackDepthLevel(price, size) for price, size in bids],
            [PullbackDepthLevel(price, size) for price, size in asks],
        )

    def on_bar(self, symbol: str, bar: Any, allow_new_entries: bool = True) -> None:
        row = _bar_row(symbol, bar)
        self.histories[symbol] = pullback_append_bar(self.histories.get(symbol, pd.DataFrame()), row)
        now = pd.Timestamp(row["timestamp"]).to_pydatetime()
        self.positions.reconcile_time_exits(now)
        if not allow_new_entries:
            return
        if symbol == self.config.strategy.market_symbol or symbol not in self.config.strategy.symbols:
            return
        if self.broker.is_symbol_cooling_down(symbol, now):
            self.logger.event("strategy_skip_cooldown", {"strategy": self.strategy_name, "symbol": symbol, "time": now.isoformat()})
            return
        if not self.registry.is_available(symbol, self.strategy_name):
            self.logger.event("strategy_skip_locked", {"strategy": self.strategy_name, "symbol": symbol, "owner": self.registry.owner(symbol)})
            return
        position = self.positions.position(symbol)
        if position.is_open:
            return
        market_history = self.histories.get(self.config.strategy.market_symbol)
        quote = self.quotes.get(symbol) or PullbackQuote(symbol, now, float(row["close"]) * 0.9999, float(row["close"]) * 1.0001)
        l2 = self.l2.get(symbol) if self.config.strategy.use_l2 else None
        signal, decision = self.engine.evaluate(symbol, self.histories[symbol], market_history, quote, l2)
        self.logger.csv("decisions", {"strategy": self.strategy_name, "symbol": symbol, "approved": signal is not None, "reason": decision.get("reason", ""), "score": decision.get("score", "")})
        if signal is None:
            return
        risk_decision = self.risk.approve(signal, quote, open_positions=len([pos for pos in self.positions.positions.values() if pos.is_open]))
        if not risk_decision.approved:
            self.logger.event("risk_reject", {"strategy": self.strategy_name, "symbol": symbol, "reason": risk_decision.reason})
            return
        self.orders.submit_entry(signal, risk_decision, position)

    def on_broker_fill(self, order_id: str, timestamp: datetime, quantity: int, price: float, commission: float = 0.0) -> None:
        if order_id not in self.orders.orders:
            return
        order = self.orders.orders[order_id]
        if order.side == PullbackSide.BUY:
            self.positions.on_entry_fill(order, timestamp, quantity, price)
            self.registry.lock_position(order.symbol, self.strategy_name, timestamp)
        else:
            self.positions.on_exit_fill(order, timestamp, quantity, price)
            if not self.positions.position(order.symbol).is_open:
                self.registry.unlock_if_owner(order.symbol, self.strategy_name)


class OrmBrokerAdapter:
    def __init__(self, owner: "OpeningRangeAdapter", shared: SharedBroker) -> None:
        self.owner = owner
        self.shared = shared

    def submit_entry(self, order: Any, signal: Any) -> None:
        self.shared.submit_orm_entry(self.owner, order, signal)

    def submit_bracket(self, symbol: str, stop_order: Any, target_order: Any) -> None:
        self.shared.submit_orm_bracket(self.owner, symbol, stop_order, target_order)

    def flatten(self, order: Any) -> None:
        self.shared.flatten_orm(self.owner, order)

    def cancel(self, order_id: str) -> None:
        self.shared.cancel(order_id)


class OpeningRangeAdapter(StrategyAdapter):
    strategy_name = "opening_range"

    def __init__(self, config_path: str | Path, symbols: list[str], broker: SharedBroker, registry: PositionRegistry, logger: MultiStrategyLogger) -> None:
        self.config = load_orm_config(config_path)
        self.config.strategy.symbols = symbols
        self.broker = broker
        self.registry = registry
        self.logger = logger
        self.audit = OrmLogger(Path(logger.run_dir) / self.strategy_name, write_book_snapshots=False)
        self.market = OrmMarketState([*symbols, *self.config.strategy.market_symbols], self.config.strategy.or_start, self.config.strategy.or_end)
        self.risk = OrmRiskManager(self.config.risk)
        self.execution = OrmExecutionManager(self.risk, self.config.risk, self.config.strategy, self.audit, broker=OrmBrokerAdapter(self, broker))
        self.engine = OrmSignalEngine(self.config.strategy, OrmScanner(self.config.strategy))

    def symbols(self) -> set[str]:
        return set(self.config.strategy.symbols) | set(self.config.strategy.market_symbols)

    def on_quote(self, symbol: str, timestamp: datetime, bid: float, ask: float, bid_size: int = 0, ask_size: int = 0) -> None:
        if symbol in self.market.symbols:
            self.market.on_quote(OrmQuote(symbol, timestamp, bid, ask, bid_size, ask_size))

    def on_depth(self, symbol: str, timestamp: datetime, bids: list[tuple[float, int]], asks: list[tuple[float, int]]) -> None:
        if symbol in self.market.symbols:
            self.market.on_book(OrmBookSnapshot(symbol, timestamp, tuple(bids), tuple(asks)))

    def on_bar(self, symbol: str, bar: Any, allow_new_entries: bool = True) -> None:
        if symbol not in self.market.symbols:
            return
        orm_bar = OrmBar(symbol, _bar_time(bar), float(bar.open), float(bar.high), float(bar.low), float(bar.close), int(float(getattr(bar, "volume", 0) or 0)))
        self.market.on_bar(orm_bar)
        state = self.market.state(symbol)
        if not allow_new_entries:
            self.execution.reconcile(orm_bar.timestamp)
            return
        if symbol not in self.config.strategy.symbols:
            return
        if self.broker.is_symbol_cooling_down(symbol, orm_bar.timestamp):
            self.logger.event("strategy_skip_cooldown", {"strategy": self.strategy_name, "symbol": symbol, "time": orm_bar.timestamp.isoformat()})
            self.execution.reconcile(orm_bar.timestamp)
            return
        if not self.registry.is_available(symbol, self.strategy_name):
            self.logger.event("strategy_skip_locked", {"strategy": self.strategy_name, "symbol": symbol, "owner": self.registry.owner(symbol)})
            return
        signal, decision = self.engine.evaluate(state, list(self.market.symbols.values()))
        self.logger.csv("decisions", {"strategy": self.strategy_name, "symbol": symbol, "approved": signal is not None, "reason": decision.get("reason", "")})
        if signal:
            self.execution.on_signal(signal, state.quote)
        self.execution.reconcile(orm_bar.timestamp)

    def on_broker_fill(self, order_id: str, timestamp: datetime, quantity: int, price: float, commission: float = 0.0) -> None:
        if order_id not in self.execution.orders:
            return
        order = self.execution.orders[order_id]
        self.execution.on_fill(order_id, timestamp, quantity, price, commission)
        if order.side.value == "BUY":
            self.registry.lock_position(order.symbol, self.strategy_name, timestamp)
        else:
            position = self.execution.positions.get(order.symbol)
            if not position or not position.is_open:
                self.registry.unlock_if_owner(order.symbol, self.strategy_name)


def _bar_time(bar: Any) -> datetime:
    raw = getattr(bar, "date", None) or getattr(bar, "time", None) or getattr(bar, "timestamp", None)
    ts = pd.Timestamp(raw).to_pydatetime() if raw is not None else datetime.now(timezone.utc)
    return ts if ts.tzinfo else ts.replace(tzinfo=timezone.utc)


def _bar_row(symbol: str, bar: Any) -> dict[str, Any]:
    timestamp = pd.Timestamp(_bar_time(bar))
    return {
        "symbol": symbol,
        "timestamp": timestamp,
        "open": float(bar.open),
        "high": float(bar.high),
        "low": float(bar.low),
        "close": float(bar.close),
        "volume": int(float(getattr(bar, "volume", 0) or 0)),
        "vwap": float(getattr(bar, "average", 0) or 0) or pd.NA,
    }
