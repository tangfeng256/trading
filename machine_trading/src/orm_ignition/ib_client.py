from __future__ import annotations

import math
import time
from datetime import datetime, timezone
from typing import Callable, Dict, Iterable, Optional, Any

from .config import IBConfig
from .execution_manager import ManagedOrder
from .market_state import Bar, BookSnapshot, Quote
from .signal_engine import Signal


class IBClient:
    """Thin ib_insync wrapper for quotes, 1-minute bars, optional depth, and order callbacks."""

    def __init__(self, config: IBConfig, symbols: Iterable[str], paper: bool = True) -> None:
        try:
            from ib_insync import IB, LimitOrder, MarketOrder, Stock, StopOrder
        except ImportError as exc:
            raise RuntimeError("ib_insync is required for live or paper trading.") from exc
        self.IB = IB
        self.LimitOrder = LimitOrder
        self.MarketOrder = MarketOrder
        self.Stock = Stock
        self.StopOrder = StopOrder
        self.ib = IB()
        self.config = config
        self.symbols = list(symbols)
        self.paper = paper
        self.contracts: Dict[str, object] = {}
        self.trades_by_order_id: Dict[str, list[object]] = {}
        self.quote_callback: Optional[Callable[[Quote], None]] = None
        self.bar_callback: Optional[Callable[[Bar], None]] = None
        self.book_callback: Optional[Callable[[BookSnapshot], None]] = None
        self.error_callback: Optional[Callable[[str], None]] = None
        self.fill_callback: Optional[Callable[[str, datetime, int, float, float], None]] = None

    @property
    def port(self) -> int:
        return self.config.paper_port if self.paper else self.config.live_port

    def connect(self) -> None:
        for attempt in range(1, self.config.reconnect_attempts + 1):
            try:
                self.ib.connect(self.config.host, self.port, clientId=self.config.client_id)
                self.ib.reqMarketDataType(self.config.market_data_type)
                self.ib.errorEvent += self._on_error
                self.ib.disconnectedEvent += self._on_disconnect
                self._qualify_contracts()
                return
            except Exception:
                if attempt == self.config.reconnect_attempts:
                    raise
                time.sleep(self.config.reconnect_sleep_seconds)

    def disconnect(self) -> None:
        if self.ib.isConnected():
            self.ib.disconnect()

    def subscribe_market_data(self) -> None:
        for symbol, contract in self.contracts.items():
            ticker = self.ib.reqMktData(contract, "", False, False)
            ticker.updateEvent += lambda ticker, symbol=symbol: self._on_quote(symbol, ticker)

    def subscribe_bars(self) -> None:
        for symbol, contract in self.contracts.items():
            bars = self.ib.reqRealTimeBars(contract, 60, "TRADES", False)
            bars.updateEvent += lambda bars, has_new_bar, symbol=symbol: self._on_bar(symbol, bars, has_new_bar)

    def subscribe_depth(self) -> None:
        for symbol, contract in self.contracts.items():
            ticker = self.ib.reqMktDepth(contract, self.config.depth_rows, isSmartDepth=self.config.smart_depth)
            ticker.updateEvent += lambda ticker, symbol=symbol: self._on_book(symbol, ticker)

    def run(self) -> None:
        self.ib.run()

    def submit_entry(self, order: ManagedOrder, signal: Signal) -> None:
        contract = self._contract(order.symbol)
        ib_order = self.LimitOrder("BUY", order.quantity, order.price, tif="DAY", outsideRth=False)
        ib_order.orderRef = order.order_id
        trade = self.ib.placeOrder(contract, ib_order)
        self._track_trade(order.order_id, trade)

    def submit_bracket(self, symbol: str, stop_order: ManagedOrder, target_order: ManagedOrder) -> None:
        contract = self._contract(symbol)
        oca_group = f"ORM-{symbol}-{stop_order.order_id}-{target_order.order_id}"
        ib_stop = self.StopOrder("SELL", stop_order.quantity, stop_order.price, tif="DAY", outsideRth=False)
        ib_stop.orderRef = stop_order.order_id
        ib_stop.ocaGroup = oca_group
        ib_stop.ocaType = 1
        ib_target = self.LimitOrder("SELL", target_order.quantity, target_order.price, tif="DAY", outsideRth=False)
        ib_target.orderRef = target_order.order_id
        ib_target.ocaGroup = oca_group
        ib_target.ocaType = 1
        self._track_trade(stop_order.order_id, self.ib.placeOrder(contract, ib_stop))
        self._track_trade(target_order.order_id, self.ib.placeOrder(contract, ib_target))

    def flatten(self, order: ManagedOrder) -> None:
        contract = self._contract(order.symbol)
        ib_order = self.MarketOrder("SELL", order.quantity, tif="DAY", outsideRth=False)
        ib_order.orderRef = order.order_id
        self._track_trade(order.order_id, self.ib.placeOrder(contract, ib_order))

    def cancel(self, order_id: str) -> None:
        for trade in self.trades_by_order_id.get(order_id, []):
            order = getattr(trade, "order", None)
            if order is not None:
                self.ib.cancelOrder(order)

    def _qualify_contracts(self) -> None:
        for symbol in self.symbols:
            contract = self.Stock(symbol, self.config.exchange, self.config.currency)
            qualified = self.ib.qualifyContracts(contract)
            if not qualified:
                raise RuntimeError(f"IB could not qualify contract for {symbol}.")
            self.contracts[symbol] = qualified[0]

    def _contract(self, symbol: str):
        if symbol not in self.contracts:
            raise RuntimeError(f"IB contract is not qualified for {symbol}.")
        return self.contracts[symbol]

    def _track_trade(self, order_id: str, trade) -> None:
        self.trades_by_order_id.setdefault(order_id, []).append(trade)
        fill_event = getattr(trade, "fillEvent", None)
        if fill_event is not None:
            fill_event += lambda trade, fill, order_id=order_id: self._on_fill(order_id, fill)

    def _on_fill(self, order_id: str, fill) -> None:
        if not self.fill_callback:
            return
        execution = getattr(fill, "execution", None)
        if execution is None:
            return
        timestamp = getattr(execution, "time", None) or datetime.now(timezone.utc)
        if timestamp.tzinfo is None:
            timestamp = timestamp.replace(tzinfo=timezone.utc)
        commission_report = getattr(fill, "commissionReport", None)
        commission = _safe_float(getattr(commission_report, "commission", 0.0))
        self.fill_callback(
            order_id,
            timestamp,
            _safe_int(getattr(execution, "shares", 0)),
            _safe_float(getattr(execution, "price", 0.0)),
            commission,
        )

    def _on_quote(self, symbol: str, ticker) -> None:
        if not self.quote_callback:
            return
        bid = _safe_float(getattr(ticker, "bid", 0))
        ask = _safe_float(getattr(ticker, "ask", 0))
        if bid <= 0 or ask <= 0 or ask <= bid:
            return
        self.quote_callback(
            Quote(
                symbol,
                datetime.now(timezone.utc),
                bid,
                ask,
                _safe_int(getattr(ticker, "bidSize", 0)),
                _safe_int(getattr(ticker, "askSize", 0)),
            )
        )

    def _on_bar(self, symbol: str, bars, has_new_bar: bool) -> None:
        if not self.bar_callback or not has_new_bar or not bars:
            return
        raw = bars[-1]
        ts = raw.time if hasattr(raw.time, "tzinfo") else datetime.now(timezone.utc)
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        open_ = _safe_float(raw.open_)
        high = _safe_float(raw.high)
        low = _safe_float(raw.low)
        close = _safe_float(raw.close)
        if min(open_, high, low, close) <= 0:
            return
        self.bar_callback(Bar(symbol, ts, open_, high, low, close, _safe_int(raw.volume)))

    def _on_book(self, symbol: str, ticker) -> None:
        if not self.book_callback:
            return
        bids = _book_levels((ticker.domBids or [])[: self.config.depth_rows])
        asks = _book_levels((ticker.domAsks or [])[: self.config.depth_rows])
        self.book_callback(BookSnapshot(symbol, datetime.now(timezone.utc), bids=bids, asks=asks))

    def _on_error(self, req_id, error_code, error_string, contract=None) -> None:
        if self.error_callback:
            self.error_callback(f"reqId={req_id} code={error_code} {error_string}")

    def _on_disconnect(self) -> None:
        if self.error_callback:
            self.error_callback("IB disconnected")


def _safe_float(value: Any, default: float = 0.0) -> float:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return default
    return result if math.isfinite(result) else default


def _safe_int(value: Any, default: int = 0) -> int:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return default
    return int(result) if math.isfinite(result) else default


def _book_levels(levels) -> tuple[tuple[float, int], ...]:
    cleaned = []
    for level in levels:
        price = _safe_float(getattr(level, "price", 0))
        size = _safe_int(getattr(level, "size", 0))
        if price > 0:
            cleaned.append((price, size))
    return tuple(cleaned)
