from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from .config import AppConfig
from .contracts import stock_contract
from .depth_book import DepthBook
from .execution_manager import ExecutionManager
from .features import FeatureEngine
from .logger import RunLogger
from .order_state import ManagedOrder, TradeStatus
from .risk_manager import RiskManager
from .signal_engine import SignalEngine
from .tape import Tape


IB_DEPTH_SIDE_TO_BOOK = {0: "ask", 1: "bid"}
LAST_TICK_TYPES = {4, 68}


@dataclass
class SymbolRuntime:
    symbol: str
    contract: Any
    book: DepthBook
    tape: Tape
    ticker: Any
    processed_ticks: int = 0
    processed_dom_ticks: int = 0
    processed_tick_by_ticks: int = 0
    last_feature_at: datetime | None = None


class LiveTradingEngine:
    """Coordinates IBKR market data, signal evaluation, risk, and execution."""

    def __init__(
        self,
        config: AppConfig,
        ib: Any,
        logger: RunLogger,
        *,
        submit_orders: bool = True,
    ) -> None:
        self.config = config
        self.ib = ib
        self.logger = logger
        self.submit_orders = submit_orders
        self.features = FeatureEngine(config.strategy)
        self.signals = SignalEngine(config.strategy, config.market.tick_size)
        self.risk = RiskManager(config.risk, config.strategy)
        self.execution = ExecutionManager(config.execution, config.market, config.strategy, logger)
        self.symbols: dict[str, SymbolRuntime] = {}
        self._broker_order_ids: dict[str, int] = {}
        self._managed_by_broker_id: dict[int, str] = {}
        self._broker_filled_qty: dict[str, int] = {}
        self._started = False

    def start(self) -> None:
        if self._started:
            return
        for symbol in self.config.symbols:
            contract = stock_contract(symbol, self.config.market)
            if hasattr(self.ib, "qualifyContracts"):
                qualified = self.ib.qualifyContracts(contract)
                if qualified:
                    contract = qualified[0]
            ticker = self.ib.reqMktData(contract, "", False, False)
            self.ib.reqMktDepth(contract, self.config.market.depth_rows, False)
            runtime = SymbolRuntime(
                symbol=symbol,
                contract=contract,
                book=DepthBook(symbol, max_depth=self.config.market.depth_rows),
                tape=Tape(symbol),
                ticker=ticker,
            )
            self.symbols[symbol] = runtime
            if hasattr(ticker, "updateEvent"):
                ticker.updateEvent += self._ticker_handler(symbol)
            self.logger.event("symbol_subscribed", {"symbol": symbol})
        if hasattr(self.ib, "orderStatusEvent"):
            self.ib.orderStatusEvent += self._on_order_status
        self._started = True

    def stop(self) -> None:
        for runtime in self.symbols.values():
            if hasattr(self.ib, "cancelMktDepth"):
                self.ib.cancelMktDepth(runtime.contract, False)
            if hasattr(self.ib, "cancelMktData"):
                self.ib.cancelMktData(runtime.contract)

    def poll(self, now: datetime | None = None) -> None:
        now = now or datetime.now(timezone.utc)
        self.execution.cancel_stale_entries(now)
        expired = self.execution.flatten_expired_positions(now)
        self._submit_orders(expired)
        for runtime in self.symbols.values():
            if not self._feature_due(runtime, now):
                continue
            runtime.last_feature_at = now
            features = self.features.compute(runtime.symbol, now, runtime.book, runtime.tape)
            if self.config.logging.log_features:
                self.logger.csv("features", features)
            signal, decision = self.signals.evaluate(runtime.symbol, features)
            self.logger.decision(decision)
            if not signal:
                continue
            self.logger.csv("signals", signal.__dict__)
            risk_decision = self.risk.approve(
                signal,
                existing_position_or_order=self._has_active_symbol(signal.symbol),
            )
            self.logger.event(
                "risk_decision",
                {"symbol": signal.symbol, **risk_decision.__dict__},
            )
            if not risk_decision.approved:
                continue
            trade = self.execution.submit_entry(signal, risk_decision.qty)
            self.risk.mark_trade_opened(signal.symbol, signal.timestamp)
            self._submit_orders(trade.orders.values())

    def _ticker_handler(self, symbol: str):
        def handle(ticker: Any) -> None:
            self.on_ticker_update(symbol, ticker)

        return handle

    def on_ticker_update(self, symbol: str, ticker: Any) -> None:
        runtime = self.symbols[symbol]
        now = datetime.now(timezone.utc)
        dom_ticks = list(getattr(ticker, "domTicks", []) or [])
        for tick in dom_ticks[runtime.processed_dom_ticks :]:
            side = IB_DEPTH_SIDE_TO_BOOK.get(getattr(tick, "side", None))
            if side is None:
                continue
            ts = getattr(tick, "time", None) or now
            runtime.book.apply_update(
                getattr(tick, "position"),
                getattr(tick, "operation"),
                side,
                getattr(tick, "price"),
                getattr(tick, "size"),
                getattr(tick, "marketMaker", ""),
                timestamp=ts,
            )
        runtime.processed_dom_ticks = len(dom_ticks)

        bid = runtime.book.best_bid().price if runtime.book.best_bid() else getattr(ticker, "bid", None)
        ask = runtime.book.best_ask().price if runtime.book.best_ask() else getattr(ticker, "ask", None)
        ticks = list(getattr(ticker, "ticks", []) or [])
        for tick in ticks[runtime.processed_ticks :]:
            if getattr(tick, "tickType", None) not in LAST_TICK_TYPES:
                continue
            price = getattr(tick, "price", None)
            size = getattr(tick, "size", None)
            if price is None or size is None or size <= 0:
                continue
            runtime.tape.add_trade(getattr(tick, "time", None) or now, price, size, bid=bid, ask=ask)
            if self.config.logging.log_tape:
                self.logger.csv("tape", {"timestamp": getattr(tick, "time", None) or now, "symbol": symbol, "price": price, "size": size, "bid": bid, "ask": ask})
        runtime.processed_ticks = len(ticks)

        tick_by_ticks = list(getattr(ticker, "tickByTicks", []) or [])
        for tick in tick_by_ticks[runtime.processed_tick_by_ticks :]:
            price = getattr(tick, "price", None)
            size = getattr(tick, "size", None)
            if price is None or size is None or size <= 0:
                continue
            runtime.tape.add_trade(getattr(tick, "time", None) or now, price, size, bid=bid, ask=ask)
            if self.config.logging.log_tape:
                self.logger.csv("tape", {"timestamp": getattr(tick, "time", None) or now, "symbol": symbol, "price": price, "size": size, "bid": bid, "ask": ask})
        runtime.processed_tick_by_ticks = len(tick_by_ticks)

        if self.config.logging.log_depth:
            self.logger.csv("depth_snapshots", runtime.book.snapshot())

    def _feature_due(self, runtime: SymbolRuntime, now: datetime) -> bool:
        if runtime.last_feature_at is None:
            return True
        elapsed_ms = (now - runtime.last_feature_at).total_seconds() * 1000.0
        return elapsed_ms >= self.config.strategy.feature_interval_ms

    def _has_active_symbol(self, symbol: str) -> bool:
        for trade in self.execution.trades.values():
            if trade.symbol == symbol and trade.is_active:
                return True
        return False

    def _submit_orders(self, orders: Any) -> None:
        if not self.submit_orders:
            return
        for managed_order in orders:
            if managed_order.order_id in self._broker_order_ids:
                continue
            runtime = self.symbols.get(managed_order.symbol)
            if runtime is None:
                continue
            broker_order = self._to_ib_order(managed_order)
            broker_trade = self.ib.placeOrder(runtime.contract, broker_order)
            broker_id = getattr(getattr(broker_trade, "order", broker_order), "orderId", None)
            if broker_id is not None:
                self._broker_order_ids[managed_order.order_id] = broker_id
                self._managed_by_broker_id[broker_id] = managed_order.order_id

    def _to_ib_order(self, managed_order: ManagedOrder) -> Any:
        try:
            from ib_insync import LimitOrder, MarketOrder, StopOrder
        except ImportError as exc:
            raise RuntimeError("ib_insync is required for IBKR order submission") from exc
        if managed_order.order_type == "MKT":
            return MarketOrder(managed_order.side, managed_order.qty)
        if managed_order.order_type == "STP":
            if managed_order.stop_price is None:
                raise ValueError(f"stop order {managed_order.order_id} missing stop_price")
            return StopOrder(managed_order.side, managed_order.qty, managed_order.stop_price)
        if managed_order.price is None:
            raise ValueError(f"limit order {managed_order.order_id} missing price")
        return LimitOrder(managed_order.side, managed_order.qty, managed_order.price)

    def _on_order_status(self, broker_trade: Any) -> None:
        broker_order = getattr(broker_trade, "order", None)
        broker_status = getattr(broker_trade, "orderStatus", None)
        broker_id = getattr(broker_order, "orderId", None)
        if broker_id is None or broker_status is None:
            return
        managed_order_id = self._managed_by_broker_id.get(broker_id)
        if managed_order_id is None:
            return
        filled = int(getattr(broker_status, "filled", 0) or 0)
        previous = self._broker_filled_qty.get(managed_order_id, 0)
        fill_delta = max(0, filled - previous)
        if fill_delta <= 0:
            return
        avg_fill_price = float(getattr(broker_status, "avgFillPrice", 0.0) or 0.0)
        created = self.execution.on_fill(managed_order_id, fill_delta, avg_fill_price, datetime.now(timezone.utc))
        self._broker_filled_qty[managed_order_id] = filled
        self._submit_orders(created)
        trade = self.execution._trade_for_order(managed_order_id)
        if trade and trade.status == TradeStatus.CLOSED:
            self.risk.mark_trade_closed(trade.symbol, datetime.now(timezone.utc), trade.realized_pnl)
