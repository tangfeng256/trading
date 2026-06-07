from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timezone
from types import SimpleNamespace

from ib_insync import MktDepthData, TickData

from absorption.config import AppConfig, LoggingConfig, MarketConfig, StrategyConfig
from absorption.live_engine import LiveTradingEngine
from absorption.logger import RunLogger
from absorption.order_state import Signal


class FakeEvent:
    def __init__(self) -> None:
        self.handlers = []

    def __iadd__(self, handler):
        self.handlers.append(handler)
        return self

    def fire(self, *args):
        for handler in list(self.handlers):
            handler(*args)


class FakeTicker:
    def __init__(self) -> None:
        self.updateEvent = FakeEvent()
        self.domTicks = []
        self.ticks = []
        self.tickByTicks = []
        self.bid = None
        self.ask = None


class FakeIB:
    def __init__(self) -> None:
        self.tickers = {}
        self.depth_requests = []
        self.market_data_requests = []
        self.orderStatusEvent = FakeEvent()
        self.placed_orders = []
        self._next_order_id = 1

    def qualifyContracts(self, contract):
        return [contract]

    def reqMktData(self, contract, *_args):
        ticker = FakeTicker()
        self.tickers[contract.symbol] = ticker
        self.market_data_requests.append(contract.symbol)
        return ticker

    def reqMktDepth(self, contract, rows, is_smart_depth):
        self.depth_requests.append((contract.symbol, rows, is_smart_depth))

    def placeOrder(self, contract, order):
        order.orderId = self._next_order_id
        self._next_order_id += 1
        self.placed_orders.append((contract.symbol, order))
        return SimpleNamespace(order=order)

    def cancelMktDepth(self, *_args):
        pass

    def cancelMktData(self, *_args):
        pass


def test_live_engine_subscribes_and_updates_each_symbol_independently(tmp_path):
    engine, ib = _engine(tmp_path)
    engine.start()

    assert ib.market_data_requests == ["NVDA", "TSLA"]
    assert ib.depth_requests == [("NVDA", 5, False), ("TSLA", 5, False)]

    ts = datetime(2026, 5, 25, 14, 0, tzinfo=timezone.utc)
    _feed_quote_and_trade(ib.tickers["NVDA"], ts, bid=100.0, ask=100.02, trade=100.0)
    _feed_quote_and_trade(ib.tickers["TSLA"], ts, bid=200.0, ask=200.03, trade=200.03)

    assert engine.symbols["NVDA"].book.best_bid().price == 100.0
    assert engine.symbols["TSLA"].book.best_bid().price == 200.0
    assert engine.symbols["NVDA"].tape.last_price() == 100.0
    assert engine.symbols["TSLA"].tape.last_price() == 200.03


def test_live_engine_routes_approved_signal_to_broker_order(tmp_path):
    engine, ib = _engine(tmp_path)
    engine.start()
    ts = datetime(2026, 5, 25, 14, 0, tzinfo=timezone.utc)
    _feed_quote_and_trade(ib.tickers["NVDA"], ts, bid=100.0, ask=100.02, trade=100.02)
    _feed_quote_and_trade(ib.tickers["TSLA"], ts, bid=200.0, ask=200.02, trade=200.02)

    def evaluate(symbol, features):
        if symbol != "NVDA":
            return None, {"timestamp": features["timestamp"], "symbol": symbol, "phase": "IDLE", "passed": False, "reason": "test"}
        signal = Signal(
            symbol=symbol,
            timestamp=ts,
            phase="TRIGGER",
            entry_ref_price=100.01,
            absorption_level=100.0,
            stop_price=99.51,
            target1_price=100.51,
            target2_price=101.01,
            confidence=0.9,
            reason_codes=["test"],
            feature_snapshot={"spread_bps": 2.0},
        )
        return signal, {"timestamp": ts, "symbol": symbol, "phase": "TRIGGER", "passed": True, "reason": "test"}

    engine.signals.evaluate = evaluate
    engine.poll(ts)

    assert len(ib.placed_orders) == 1
    symbol, order = ib.placed_orders[0]
    assert symbol == "NVDA"
    assert order.action == "BUY"
    assert order.totalQuantity > 0
    assert order.orderType == "LMT"


def _engine(tmp_path):
    config = replace(
        AppConfig(),
        symbols=["NVDA", "TSLA"],
        market=MarketConfig(depth_rows=5),
        strategy=StrategyConfig(feature_interval_ms=100, trade_start="09:30:00", trade_end="11:30:00"),
        logging=LoggingConfig(root=str(tmp_path), log_depth=False, log_tape=False, log_features=False),
    )
    ib = FakeIB()
    logger = RunLogger(config.logging.root)
    return LiveTradingEngine(config, ib, logger, submit_orders=True), ib


def _feed_quote_and_trade(ticker, ts, *, bid, ask, trade):
    ticker.domTicks.extend(
        [
            MktDepthData(ts, 0, "", 0, 1, bid, 100),
            MktDepthData(ts, 0, "", 0, 0, ask, 100),
        ]
    )
    ticker.ticks.append(TickData(ts, 4, trade, 50))
    ticker.updateEvent.fire(ticker)
