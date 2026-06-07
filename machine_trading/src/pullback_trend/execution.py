from __future__ import annotations

import math
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pandas as pd

from .config import AppConfig
from .indicators import add_indicators
from .logger import AuditLogger
from .models import DepthLevel, L2Snapshot, ManagedOrder, OrderStatus, Position, Quote, Side, Signal
from .orders import OrderManager
from .position_manager import PositionManager
from .risk import RiskManager
from .signal_engine import SignalEngine


def run_paper(config: AppConfig, dry_run: bool = False) -> Path:
    return _run_broker_session(config, dry_run=dry_run, mode="paper", require_live_enabled=False)


def run_live(config: AppConfig, dry_run: bool = False) -> Path:
    return _run_broker_session(config, dry_run=dry_run, mode="live", require_live_enabled=True)


def _run_broker_session(config: AppConfig, dry_run: bool, mode: str, require_live_enabled: bool) -> Path:
    logger = AuditLogger(config.logging.base_dir, config.logging.run_id)
    if dry_run:
        logger.summary({"mode": f"dry_run_{mode}", "symbols": config.strategy.symbols, "orders_enabled": False})
        logger.close()
        return logger.run_dir
    if require_live_enabled and not config.execution.live_trading_enabled:
        logger.close()
        raise RuntimeError("Refusing to place live orders: execution.live_trading_enabled is false")

    try:
        from ib_insync import IB, Stock
    except ImportError as exc:
        logger.close()
        raise RuntimeError("ib_insync is required for broker trading") from exc

    ib = IB()
    broker: InteractiveBrokersBroker | None = None
    try:
        ib.connect(config.ib.host, config.ib.port, clientId=config.ib.client_id)
        ib.reqMarketDataType(config.ib.market_data_type)

        risk = RiskManager(config.risk, state_path=Path(config.logging.base_dir) / "risk_state.json")
        broker = InteractiveBrokersBroker(ib, config, logger)
        orders = OrderManager(config.execution, logger, broker=broker)
        positions = PositionManager(config.strategy, config.execution, risk, orders, logger)
        engine = SignalEngine(config.strategy)
        broker.bind(orders, positions)

        symbols = list(dict.fromkeys([*config.strategy.symbols, config.strategy.market_symbol]))
        contracts = {
            symbol: Stock(symbol, config.ib.exchange, config.ib.currency)
            for symbol in symbols
        }
        ib.qualifyContracts(*contracts.values())
        broker.contracts.update(contracts)

        histories: dict[str, pd.DataFrame] = {}
        last_completed_bar: dict[str, datetime] = {}
        tickers: dict[str, Any] = {}
        if config.ib.request_streaming_quotes:
            tickers = {symbol: ib.reqMktData(contract, "", False, False) for symbol, contract in contracts.items()}
            broker.market_data.extend(tickers.values())
        depth_tickers: dict[str, Any] = {}
        if config.strategy.use_l2:
            depth_tickers = {
                symbol: ib.reqMktDepth(
                    contract,
                    numRows=config.ib.market_depth_rows,
                    isSmartDepth=config.ib.smart_depth,
                )
                for symbol, contract in contracts.items()
            }
            broker.market_depth.extend(contracts.values())

        def on_completed_bar(symbol: str, bar: Any) -> None:
            row = _bar_row(symbol, bar)
            history = histories.get(symbol, pd.DataFrame())
            if not history.empty and pd.Timestamp(row["timestamp"]) <= pd.Timestamp(history.iloc[-1]["timestamp"]):
                return
            histories[symbol] = _append_bar(history, row)
            now = pd.Timestamp(row["timestamp"]).to_pydatetime()
            last_completed_bar[symbol] = now
            positions.reconcile_time_exits(now)
            if symbol == config.strategy.market_symbol:
                return

            position = positions.position(symbol)
            if position.is_open:
                return
            l2 = _l2_from_ticker(symbol, now, depth_tickers.get(symbol)) if config.strategy.use_l2 else None
            quote = l2.quote if l2 and l2.quote else _quote_from_ticker(symbol, now, tickers.get(symbol), float(row["close"]))
            market_history = histories.get(config.strategy.market_symbol)
            signal, decision = engine.evaluate(symbol, histories[symbol], market_history, quote, l2)
            logger.decision(
                timestamp=now.isoformat(),
                symbol=symbol,
                approved=signal is not None,
                reason=decision.get("reason", ""),
                score=decision.get("score", ""),
                features=decision.get("features", {}),
            )
            if signal is None:
                return
            risk_decision = risk.approve(
                signal,
                quote,
                open_positions=sum(1 for pos in positions.positions.values() if pos.is_open),
            )
            if not risk_decision.approved:
                logger.decision(
                    timestamp=now.isoformat(),
                    symbol=symbol,
                    approved=False,
                    reason=risk_decision.reason,
                    score=signal.score,
                    features=risk_decision.features or {},
                )
                return
            orders.submit_entry(signal, risk_decision, position)

        for symbol, contract in contracts.items():
            bars = ib.reqHistoricalData(
                contract,
                endDateTime="",
                durationStr=config.ib.historical_duration,
                barSizeSetting=config.ib.bar_size,
                whatToShow="TRADES",
                useRTH=config.ib.use_rth,
                formatDate=2,
                keepUpToDate=True,
            )
            seed = [_bar_row(symbol, bar) for bar in list(bars)[:-1]]
            if seed:
                histories[symbol] = add_indicators(pd.DataFrame(seed))

            def handle_update(bar_list: Any, has_new_bar: bool, watched_symbol: str = symbol) -> None:
                if has_new_bar and len(bar_list) >= 2:
                    on_completed_bar(watched_symbol, bar_list[-2])

            bars.updateEvent += handle_update

        logger.summary(
            {
                "mode": f"{mode}_running",
                "symbols": symbols,
                "orders_enabled": mode == "paper" or config.execution.live_trading_enabled,
                "ib_port": config.ib.port,
                "streaming_quotes": config.ib.request_streaming_quotes,
                "l2_enabled": config.strategy.use_l2,
                "market_depth_rows": config.ib.market_depth_rows,
            }
        )
        print(f"{mode.capitalize()} trading running. Run dir: {logger.run_dir}. Press Ctrl+C to stop.")
        print(
            _heartbeat_line(
                mode,
                config,
                symbols,
                histories,
                depth_tickers,
                positions,
                last_completed_bar,
            ),
            flush=True,
        )
        heartbeat_seconds = max(1, config.ib.heartbeat_seconds)
        last_heartbeat = time.monotonic()
        while True:
            ib.waitOnUpdate(timeout=1)
            now_monotonic = time.monotonic()
            if now_monotonic - last_heartbeat >= heartbeat_seconds:
                print(
                    _heartbeat_line(
                        mode,
                        config,
                        symbols,
                        histories,
                        depth_tickers,
                        positions,
                        last_completed_bar,
                    ),
                    flush=True,
                )
                last_heartbeat = now_monotonic
    except KeyboardInterrupt:
        logger.summary({"mode": f"{mode}_stopped", "reason": "keyboard_interrupt"})
    finally:
        if broker:
            broker.cancel_all_market_data()
        if ib.isConnected():
            ib.disconnect()
        logger.close()
    return logger.run_dir


class InteractiveBrokersBroker:
    def __init__(self, ib: Any, config: AppConfig, logger: AuditLogger) -> None:
        self.ib = ib
        self.config = config
        self.logger = logger
        self.orders: OrderManager | None = None
        self.positions: PositionManager | None = None
        self.contracts: dict[str, Any] = {}
        self.trades_by_ib_order_id: dict[int, ManagedOrder] = {}
        self.market_data: list[Any] = []
        self.market_depth: list[Any] = []

    def bind(self, orders: OrderManager, positions: PositionManager) -> None:
        self.orders = orders
        self.positions = positions

    def submit_entry(self, order: ManagedOrder, signal: Signal) -> None:
        from ib_insync import LimitOrder, Stock

        contract = self.contracts.setdefault(
            order.symbol,
            Stock(order.symbol, self.config.ib.exchange, self.config.ib.currency),
        )
        ib_order = LimitOrder("BUY", order.quantity, order.limit_price, tif="DAY", outsideRth=False)
        trade = self.ib.placeOrder(contract, ib_order)
        self._track_trade(trade, order)

    def submit_bracket(self, position: Position, orders: list[ManagedOrder]) -> None:
        from ib_insync import LimitOrder, StopOrder, Stock

        contract = self.contracts.setdefault(
            position.symbol,
            Stock(position.symbol, self.config.ib.exchange, self.config.ib.currency),
        )
        for managed in orders:
            if managed.role == "stop":
                ib_order = StopOrder("SELL", managed.quantity, managed.limit_price, tif="DAY", outsideRth=False)
            else:
                ib_order = LimitOrder("SELL", managed.quantity, managed.limit_price, tif="DAY", outsideRth=False)
            trade = self.ib.placeOrder(contract, ib_order)
            self._track_trade(trade, managed)

    def flatten(self, order: ManagedOrder) -> None:
        from ib_insync import MarketOrder, Stock

        contract = self.contracts.setdefault(
            order.symbol,
            Stock(order.symbol, self.config.ib.exchange, self.config.ib.currency),
        )
        trade = self.ib.placeOrder(contract, MarketOrder("SELL", order.quantity, tif="DAY", outsideRth=False))
        self._track_trade(trade, order)

    def cancel(self, order_id: str) -> None:
        managed = self._managed_order(order_id)
        if not managed:
            return
        for trade_order_id, tracked in list(self.trades_by_ib_order_id.items()):
            if tracked is managed:
                for trade in self.ib.trades():
                    if trade.order.orderId == trade_order_id:
                        self.ib.cancelOrder(trade.order)

    def cancel_all_market_data(self) -> None:
        for ticker in self.market_data:
            self.ib.cancelMktData(ticker.contract)
        self.market_data.clear()
        for contract in self.market_depth:
            self.ib.cancelMktDepth(contract, isSmartDepth=self.config.ib.smart_depth)
        self.market_depth.clear()

    def _track_trade(self, trade: Any, managed: ManagedOrder) -> None:
        self.trades_by_ib_order_id[trade.order.orderId] = managed
        trade.fillEvent += self._on_fill
        trade.statusEvent += self._on_status
        self.logger.order(
            timestamp=managed.created_at.isoformat(),
            symbol=managed.symbol,
            event="ib_submit",
            order_id=managed.order_id,
            side=managed.side.value,
            quantity=managed.quantity,
            price=managed.limit_price or "",
            status=managed.status.value,
            reason=managed.role,
            parent_id=managed.parent_id or "",
        )

    def _on_fill(self, trade: Any, fill: Any) -> None:
        if self.positions is None:
            return
        managed = self.trades_by_ib_order_id.get(trade.order.orderId)
        if managed is None:
            return
        quantity = int(fill.execution.shares)
        price = float(fill.execution.price)
        timestamp = _fill_time(fill)
        previous_filled = managed.filled_quantity
        managed.filled_quantity += quantity
        managed.avg_fill_price = ((managed.avg_fill_price * previous_filled) + (price * quantity)) / managed.filled_quantity
        managed.status = OrderStatus.FILLED if managed.remaining == 0 else OrderStatus.PARTIAL
        self.logger.trade(
            timestamp=timestamp.isoformat(),
            symbol=managed.symbol,
            event="ib_fill",
            quantity=quantity,
            price=price,
            pnl="",
            reason=managed.role,
        )
        if managed.side == Side.BUY:
            self.positions.on_entry_fill(managed, timestamp, quantity, price)
        else:
            self.positions.on_exit_fill(managed, timestamp, quantity, price)
            if managed.role == "tp1":
                self._replace_stop_after_tp1(managed.symbol, timestamp)
            elif managed.role in {"tp2", "stop", "flatten"}:
                self._cancel_exit_siblings_if_flat(managed.symbol)

    def _on_status(self, trade: Any) -> None:
        managed = self.trades_by_ib_order_id.get(trade.order.orderId)
        if managed is None:
            return
        status = str(trade.orderStatus.status).lower()
        if status in {"cancelled", "inactive"}:
            managed.status = OrderStatus.CANCELLED if status == "cancelled" else OrderStatus.REJECTED
            self.logger.order(
                timestamp=datetime.now(timezone.utc).isoformat(),
                symbol=managed.symbol,
                event=f"ib_{status}",
                order_id=managed.order_id,
                side=managed.side.value,
                quantity=managed.remaining,
                price=managed.limit_price or "",
                status=managed.status.value,
                reason=managed.role,
                parent_id=managed.parent_id or "",
            )

    def _replace_stop_after_tp1(self, symbol: str, timestamp: datetime) -> None:
        if self.orders is None or self.positions is None:
            return
        position = self.positions.position(symbol)
        if not position.is_open or position.stop_price is None:
            return
        stop = next(
            (
                order
                for order in self.orders.orders.values()
                if order.symbol == symbol and order.role == "stop" and order.status in {OrderStatus.WORKING, OrderStatus.PARTIAL}
            ),
            None,
        )
        if stop is None:
            return
        self.cancel(stop.order_id)
        stop.status = OrderStatus.CANCELLED
        new_stop = ManagedOrder(
            self.orders._next_id("S"),
            symbol,
            Side.SELL,
            position.quantity,
            position.stop_price,
            timestamp,
            parent_id=position.bracket_parent_id,
            role="stop",
            reason="breakeven_stop_after_tp1",
        )
        self.orders.orders[new_stop.order_id] = new_stop
        self.logger.order(
            timestamp=timestamp.isoformat(),
            symbol=symbol,
            event="replace_stop_breakeven",
            order_id=new_stop.order_id,
            side=new_stop.side.value,
            quantity=new_stop.quantity,
            price=new_stop.limit_price,
            status=new_stop.status.value,
            reason="tp1_filled",
            parent_id=new_stop.parent_id or "",
        )
        self.submit_bracket(position, [new_stop])

    def _cancel_exit_siblings_if_flat(self, symbol: str) -> None:
        if self.orders is None or self.positions is None:
            return
        if self.positions.position(symbol).is_open:
            return
        for order in self.orders.orders.values():
            if order.symbol == symbol and order.side == Side.SELL and order.status in {OrderStatus.WORKING, OrderStatus.PARTIAL}:
                self.cancel(order.order_id)

    def _managed_order(self, order_id: str) -> ManagedOrder | None:
        if self.orders is None:
            return None
        return self.orders.orders.get(order_id)


def _append_bar(history: pd.DataFrame, row: dict[str, Any]) -> pd.DataFrame:
    frame = pd.concat([history, pd.DataFrame([row])], ignore_index=True)
    return add_indicators(frame.tail(300).reset_index(drop=True))


def _heartbeat_line(
    mode: str,
    config: AppConfig,
    symbols: list[str],
    histories: dict[str, pd.DataFrame],
    depth_tickers: dict[str, Any],
    positions: PositionManager,
    last_completed_bar: dict[str, datetime],
) -> str:
    now = datetime.now(timezone.utc).replace(microsecond=0)
    orders_enabled = mode == "paper" or config.execution.live_trading_enabled
    watched = sum(1 for symbol in symbols if symbol in histories)
    open_positions = sum(1 for position in positions.positions.values() if position.is_open)
    l2_ready = sum(
        1
        for symbol, ticker in depth_tickers.items()
        if _l2_from_ticker(symbol, now, ticker) is not None
    )
    latest_bar = max(last_completed_bar.values()).replace(microsecond=0).isoformat() if last_completed_bar else "none"
    l2_status = f"{l2_ready}/{len(depth_tickers)}" if config.strategy.use_l2 else "off"
    status = "ready_to_trade" if orders_enabled else "monitoring_only"
    return (
        f"[{now.isoformat()}] heartbeat: {status}; "
        f"monitoring {watched}/{len(symbols)} symbols; "
        f"L2 {l2_status}; open_positions={open_positions}; latest_bar={latest_bar}"
    )


def _bar_row(symbol: str, bar: Any) -> dict[str, Any]:
    timestamp = pd.Timestamp(getattr(bar, "date"))
    timestamp = timestamp.tz_localize("UTC") if timestamp.tzinfo is None else timestamp.tz_convert("UTC")
    return {
        "symbol": symbol,
        "timestamp": timestamp,
        "open": float(bar.open),
        "high": float(bar.high),
        "low": float(bar.low),
        "close": float(bar.close),
        "volume": int(float(bar.volume or 0)),
        "vwap": float(bar.average) if _valid_number(getattr(bar, "average", None)) else pd.NA,
    }


def _quote_from_ticker(symbol: str, timestamp: datetime, ticker: Any, fallback: float) -> Quote:
    bid = float(getattr(ticker, "bid", 0.0) or 0.0) if ticker else 0.0
    ask = float(getattr(ticker, "ask", 0.0) or 0.0) if ticker else 0.0
    if not _valid_number(bid) or not _valid_number(ask) or bid <= 0 or ask <= 0:
        bid = fallback * 0.9999
        ask = fallback * 1.0001
    return Quote(symbol, timestamp, bid, ask)


def _l2_from_ticker(symbol: str, timestamp: datetime, ticker: Any) -> L2Snapshot | None:
    if ticker is None:
        return None
    bids = _depth_levels(getattr(ticker, "domBids", None), reverse=True)
    asks = _depth_levels(getattr(ticker, "domAsks", None), reverse=False)
    if not bids or not asks:
        return None
    return L2Snapshot(symbol, timestamp, bids, asks)


def _depth_levels(raw_levels: Any, reverse: bool) -> list[DepthLevel]:
    levels = []
    for raw in raw_levels or []:
        price = getattr(raw, "price", None)
        size = getattr(raw, "size", None)
        if not _valid_number(price) or not _valid_number(size):
            continue
        price_value = float(price)
        size_value = float(size)
        if price_value <= 0 or size_value <= 0:
            continue
        levels.append(DepthLevel(price_value, size_value, str(getattr(raw, "marketMaker", "") or "")))
    return sorted(levels, key=lambda level: level.price, reverse=reverse)


def _valid_number(value: Any) -> bool:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return False
    return math.isfinite(number)


def _fill_time(fill: Any) -> datetime:
    raw = getattr(fill.execution, "time", None) or datetime.now(timezone.utc)
    if isinstance(raw, datetime):
        return raw if raw.tzinfo else raw.replace(tzinfo=timezone.utc)
    return pd.Timestamp(raw).to_pydatetime()
