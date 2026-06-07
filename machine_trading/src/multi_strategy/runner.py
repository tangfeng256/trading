from __future__ import annotations

import time
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import pandas as pd

from .adapters import AbsorptionAdapter, OpeningRangeAdapter, PullbackAdapter, StrategyAdapter
from .broker import SharedBroker
from .config import AppConfig
from .logger import MultiStrategyLogger
from .paths import add_strategy_paths
from .registry import PositionRegistry
from .state_store import PositionStateStore

add_strategy_paths()


LAST_TICK_TYPES = {4, 68}


class MultiStrategyRunner:
    def __init__(self, config: AppConfig, mode: str = "paper") -> None:
        self.config = config
        self.mode = mode
        self.paper = mode == "paper"
        self.logger = MultiStrategyLogger(config.runtime.log_root)
        self.registry = PositionRegistry(config.runtime.lock_on_entry_order)
        self.ib: Any = None
        self.contracts: dict[str, Any] = {}
        self.tickers: dict[str, Any] = {}
        self.depth_tickers: dict[str, Any] = {}
        self._processed_ticks: dict[str, int] = {}
        self._processed_dom_ticks: dict[str, int] = {}
        self.adapters: list[StrategyAdapter] = []
        self.broker: SharedBroker | None = None
        self._forced_flatten_dates: set[str] = set()
        self._last_position_check_at: datetime | None = None
        self._last_depth: dict[str, tuple] = {}
        self.state_store = PositionStateStore(config.runtime.log_root)

    def run(self) -> Path:
        self._connect()
        try:
            self._build_adapters()
            if self.broker is not None:
                now_startup = datetime.now(timezone.utc)
                records, cooldowns = self.state_store.load()
                self.broker.reconcile_on_startup(now_startup, records, cooldowns)
                self.broker.sync_account_positions(now_startup)
                self._bind_adapters_to_restored_positions()
                self.broker.confirm_fills_on_reconnect()
            self._subscribe_market_data()
            self._subscribe_bars()
            self.logger.event("session_started", {"mode": self.mode, "symbols": sorted(self.contracts), "strategies": [a.strategy_name for a in self.adapters]})
            print(f"Multi-strategy {self.mode} session started. Run dir: {self.logger.run_dir}")
            print(f"Heartbeat every {max(1, self.config.ib.heartbeat_seconds)} seconds while monitoring stocks.")
            print("Press Ctrl+C to stop.")
            next_heartbeat = time.monotonic()
            while self.ib.isConnected():
                self.ib.waitOnUpdate(timeout=1)
                now = datetime.now(timezone.utc)
                allow_new_entries = self._allow_new_entries(now)
                self._force_flatten_if_needed(now)
                for adapter in self.adapters:
                    adapter.poll(now, allow_new_entries=allow_new_entries)
                if time.monotonic() >= next_heartbeat:
                    expired = self.registry.expire_stale_entry_orders(now)
                    if expired:
                        self.logger.event("stale_entry_order_locks_expired", {"symbols": expired, "time": now.isoformat()})
                        print(f"[{now.replace(microsecond=0).isoformat()}] stale entry order locks cleared: {', '.join(expired)}", flush=True)
                    if self.broker is not None:
                        self.state_store.save(self.broker)
                    print(self._heartbeat(now), flush=True)
                    next_heartbeat = time.monotonic() + max(1, self.config.ib.heartbeat_seconds)
        except KeyboardInterrupt:
            self.logger.event("session_stopped", {"reason": "keyboard_interrupt"})
        finally:
            if self.broker is not None:
                self.state_store.save(self.broker)
            self._disconnect()
            self.logger.finalize(
                {
                    "mode": self.mode,
                    "symbols": sorted(self.contracts),
                    "strategies": [adapter.strategy_name for adapter in self.adapters],
                    "locks": self.registry.snapshot(),
                    "dry_run": self.config.runtime.dry_run,
                }
            )
        return self.logger.run_dir

    def _bind_adapters_to_restored_positions(self) -> None:
        """After reconcile_on_startup, let each adapter claim any position it owns
        so fill callbacks route to the right adapter instead of the fallback receiver."""
        if self.broker is None:
            return
        for (strategy, symbol), qty in list(self.broker.long_positions.items()):
            if qty <= 0:
                continue
            for adapter in self.adapters:
                if adapter.strategy_name == strategy:
                    bound = self.broker.bind_position_receiver(strategy, symbol, adapter)
                    if bound:
                        self.logger.event("adapter_bound_to_restored_position", {
                            "strategy": strategy, "symbol": symbol, "quantity": qty,
                        })
                    break

    def _connect(self) -> None:
        try:
            from ib_insync import IB, Stock
        except ImportError as exc:
            raise RuntimeError("ib_insync is required for multi-strategy paper/live trading") from exc
        if self.mode == "live" and not self.config.runtime.live_trading_enabled:
            raise RuntimeError("Refusing live orders: runtime.live_trading_enabled is false")
        self.ib = IB()
        port = self.config.ib.paper_port if self.paper else self.config.ib.live_port
        self.ib.connect(self.config.ib.host, port, clientId=self.config.ib.client_id)
        self.ib.reqMarketDataType(self.config.ib.market_data_type)
        all_symbols = self._configured_symbols()
        self.contracts = {symbol: Stock(symbol, self.config.ib.exchange, self.config.ib.currency, primaryExchange=self.config.ib.primary_exchange) for symbol in all_symbols}
        qualified = self.ib.qualifyContracts(*self.contracts.values())
        if qualified:
            self.contracts.update({contract.symbol: contract for contract in qualified})

    def _configured_symbols(self) -> list[str]:
        symbols = list(dict.fromkeys(self.config.runtime.symbols))
        try:
            from pullback_trend.config import load_config as load_pullback_config
            from orm_ignition.config import load_config as load_orm_config

            pullback = load_pullback_config(self.config.strategy_files.pullback)
            orm = load_orm_config(self.config.strategy_files.opening_range)
            symbols.extend([pullback.strategy.market_symbol, *orm.strategy.market_symbols])
        except Exception as exc:
            print(f"Warning: could not load strategy symbol lists: {exc}", flush=True)
        return list(dict.fromkeys(symbols))

    def _build_adapters(self) -> None:
        assert self.ib is not None
        self.broker = SharedBroker(
            self.ib,
            self.contracts,
            self.registry,
            self.logger,
            dry_run=self.config.runtime.dry_run,
            trailing_stop_enabled=self.config.ib.trailing_stop_enabled,
            trailing_activation_bps=self.config.ib.trailing_activation_bps,
            trailing_distance_bps=self.config.ib.trailing_distance_bps,
            trailing_min_step_bps=self.config.ib.trailing_min_step_bps,
            runner_target_enabled=self.config.ib.runner_target_enabled,
            runner_target_r_multiple=self.config.ib.runner_target_r_multiple,
            forced_flatten_cooldown_seconds=self.config.runtime.forced_flatten_cooldown_seconds,
            manage_account_positions=self.config.runtime.manage_account_positions,
        )
        factories = {
            "opening_range": lambda: OpeningRangeAdapter(self.config.strategy_files.opening_range, self.config.runtime.symbols, self.broker, self.registry, self.logger),
            "pullback": lambda: PullbackAdapter(self.config.strategy_files.pullback, self.config.runtime.symbols, self.broker, self.registry, self.logger),
            "absorption": lambda: AbsorptionAdapter(self.config.strategy_files.absorption, self.config.runtime.symbols, self.broker, self.registry, self.logger),
        }
        enabled = set(self.config.runtime.enabled_strategies)
        self.adapters = [factories[name]() for name in self.config.runtime.strategy_priority if name in enabled]

    def _subscribe_market_data(self) -> None:
        depth_symbols = set(self._depth_symbols())
        for symbol, contract in self.contracts.items():
            ticker = self.ib.reqMktData(contract, "", False, False)
            ticker.updateEvent += lambda ticker, watched_symbol=symbol: self._on_ticker(watched_symbol, ticker)
            self.tickers[symbol] = ticker
            if symbol not in depth_symbols:
                continue
            depth = self.ib.reqMktDepth(contract, self.config.ib.depth_rows, isSmartDepth=self.config.ib.smart_depth)
            depth.updateEvent += lambda ticker, watched_symbol=symbol: self._on_ticker(watched_symbol, ticker)
            self.depth_tickers[symbol] = depth

    def _depth_symbols(self) -> list[str]:
        max_depth_requests = max(0, self.config.ib.max_depth_requests)
        requested = self.config.ib.depth_symbols or list(self.contracts)
        available = [symbol for symbol in requested if symbol in self.contracts]
        selected = available[:max_depth_requests]
        skipped = [symbol for symbol in self.contracts if symbol not in selected]
        if skipped:
            message = (
                f"Depth subscriptions limited to {max_depth_requests}: "
                f"using {', '.join(selected) or 'none'}; L1 only for {', '.join(skipped)}"
            )
            print(message, flush=True)
            if hasattr(self, "logger"):
                self.logger.event("depth_subscription_limit", {"selected": selected, "l1_only": skipped, "max_depth_requests": max_depth_requests})
        return selected

    def _subscribe_bars(self) -> None:
        for symbol, contract in self.contracts.items():
            bars = self.ib.reqHistoricalData(
                contract,
                endDateTime="",
                durationStr=self.config.ib.historical_duration,
                barSizeSetting=self.config.ib.bar_size,
                whatToShow="TRADES",
                useRTH=self.config.ib.use_rth,
                formatDate=2,
                keepUpToDate=True,
            )
            for bar in list(bars)[:-1]:
                self._on_completed_bar(symbol, bar, allow_new_entries=False)

            def handle_update(bar_list: Any, has_new_bar: bool, watched_symbol: str = symbol) -> None:
                if has_new_bar and len(bar_list) >= 2:
                    self._on_completed_bar(watched_symbol, bar_list[-2])

            bars.updateEvent += handle_update

    def _on_ticker(self, symbol: str, ticker: Any) -> None:
        now = datetime.now(timezone.utc)
        bid = _positive_float(getattr(ticker, "bid", None))
        ask = _positive_float(getattr(ticker, "ask", None))
        if bid and ask and ask > bid:
            for adapter in self.adapters:
                adapter.on_quote(symbol, now, bid, ask, int(float(getattr(ticker, "bidSize", 0) or 0)), int(float(getattr(ticker, "askSize", 0) or 0)))
            if self.broker is not None:
                midpoint = (bid + ask) / 2
                self.broker.update_trailing_stops(symbol, midpoint, now)
                self.broker.enforce_stop_breaches(symbol, midpoint, now)

        bids = _book_levels(getattr(ticker, "domBids", None), reverse=True, limit=self.config.ib.depth_rows)
        asks = _book_levels(getattr(ticker, "domAsks", None), reverse=False, limit=self.config.ib.depth_rows)
        if bids and asks:
            depth_key = (tuple(bids), tuple(asks))
            if depth_key != self._last_depth.get(symbol):
                self._last_depth[symbol] = depth_key
                self._log_depth_snapshot(symbol, now, bids, asks)
                for adapter in self.adapters:
                    adapter.on_depth(symbol, now, bids, asks)

        dom_ticks = getattr(ticker, "domTicks", None) or []
        start_dom = self._processed_dom_ticks.get(symbol, 0)
        for tick in dom_ticks[start_dom:]:
            self._log_dom_tick(symbol, tick)
            for adapter in self.adapters:
                adapter.on_depth_tick(symbol, tick)
        self._processed_dom_ticks[symbol] = len(dom_ticks)

        ticks = getattr(ticker, "ticks", None) or []
        start_tick = self._processed_ticks.get(symbol, 0)
        for tick in ticks[start_tick:]:
            if getattr(tick, "tickType", None) not in LAST_TICK_TYPES:
                continue
            price = _positive_float(getattr(tick, "price", None))
            size = int(float(getattr(tick, "size", 0) or 0))
            if not price or size <= 0:
                continue
            ts = getattr(tick, "time", None) or now
            for adapter in self.adapters:
                adapter.on_trade(symbol, ts, price, size, bid, ask)
        self._processed_ticks[symbol] = len(ticks)

    def _on_completed_bar(self, symbol: str, bar: Any, allow_new_entries: bool | None = None) -> None:
        if allow_new_entries is None:
            allow_new_entries = self._allow_new_entries(_bar_time_utc(bar))
        for adapter in self.adapters:
            adapter.on_bar(symbol, bar, allow_new_entries=allow_new_entries)

    def _heartbeat(self, now: datetime) -> str:
        locks = ", ".join(f"{symbol}:{lock['strategy']}" for symbol, lock in self.registry.snapshot().items()) or "none"
        if self.config.runtime.dry_run:
            mode = "dry-run monitoring only"
        elif self._allow_new_entries(now):
            mode = "ready to trade"
        elif self._is_trading_window(now):
            mode = "closing positions"
        else:
            mode = "passed trading window"
        symbols = ", ".join(sorted(self.contracts)) or "none"
        return f"[{now.replace(microsecond=0).isoformat()}] heartbeat: monitoring {len(self.contracts)} stocks ({symbols}); status={mode}; locks={locks}"

    def _is_trading_window(self, timestamp: datetime) -> bool:
        if timestamp.tzinfo is None:
            timestamp = timestamp.replace(tzinfo=timezone.utc)
        local = timestamp.astimezone(ZoneInfo(self.config.runtime.trading_timezone))
        start = _parse_clock(self.config.runtime.trading_start)
        end = _parse_clock(self.config.runtime.trading_end)
        current = local.time().replace(microsecond=0)
        return start <= current < end

    def _allow_new_entries(self, timestamp: datetime) -> bool:
        if not self._is_trading_window(timestamp):
            return False
        local = self._local_time(timestamp)
        cutoff = self._closeout_start(local)
        return local < cutoff

    def _should_force_flatten(self, timestamp: datetime) -> bool:
        local = self._local_time(timestamp)
        return local >= self._closeout_start(local)

    def _force_flatten_if_needed(self, timestamp: datetime) -> None:
        if self.broker is None or not self._should_force_flatten(timestamp):
            return
        if self._last_position_check_at is not None:
            elapsed = (timestamp - self._last_position_check_at).total_seconds()
            if elapsed < max(1, self.config.runtime.post_window_position_check_seconds):
                return
        self._last_position_check_at = timestamp
        self.broker.sync_account_positions(timestamp)
        if not self.broker.has_open_positions():
            return
        local = self._local_time(timestamp)
        date_key = local.strftime("%Y-%m-%d")
        self._forced_flatten_dates.add(date_key)
        self.logger.event("trading_window_force_flatten", {"time": timestamp.isoformat(), "local_time": local.isoformat()})
        self.broker.flatten_all_positions(timestamp, reason="trading_window_close")

    def _local_time(self, timestamp: datetime) -> datetime:
        if timestamp.tzinfo is None:
            timestamp = timestamp.replace(tzinfo=timezone.utc)
        return timestamp.astimezone(ZoneInfo(self.config.runtime.trading_timezone))

    def _closeout_start(self, local_timestamp: datetime) -> datetime:
        end = _parse_clock(self.config.runtime.trading_end)
        window_end = local_timestamp.replace(hour=end.hour, minute=end.minute, second=end.second, microsecond=0)
        buffer = max(0, self.config.runtime.flatten_before_window_end_seconds)
        return window_end - timedelta(seconds=buffer)

    def _log_depth_snapshot(self, symbol: str, timestamp: datetime, bids: list[tuple[float, int]], asks: list[tuple[float, int]]) -> None:
        self.logger.csv(
            "depth_snapshots",
            {
                "timestamp": timestamp,
                "symbol": symbol,
                "bids": json.dumps([{"price": price, "size": size} for price, size in bids], separators=(",", ":")),
                "asks": json.dumps([{"price": price, "size": size} for price, size in asks], separators=(",", ":")),
            },
        )

    def _log_dom_tick(self, symbol: str, tick: Any) -> None:
        self.logger.csv(
            "dom_ticks",
            {
                "timestamp": getattr(tick, "time", None) or datetime.now(timezone.utc),
                "symbol": symbol,
                "side": _dom_side(getattr(tick, "side", None)),
                "operation": _dom_operation(getattr(tick, "operation", None)),
                "position": getattr(tick, "position", ""),
                "price": getattr(tick, "price", ""),
                "size": getattr(tick, "size", ""),
                "market_maker": getattr(tick, "marketMaker", "") or "",
            },
        )

    def _disconnect(self) -> None:
        if self.ib and self.ib.isConnected():
            for ticker in self.tickers.values():
                contract = getattr(ticker, "contract", None)
                if contract is not None:
                    self.ib.cancelMktData(contract)
            for ticker in self.depth_tickers.values():
                contract = getattr(ticker, "contract", None)
                if contract is not None:
                    self.ib.cancelMktDepth(contract, isSmartDepth=self.config.ib.smart_depth)
            self.ib.disconnect()


def _positive_float(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if number > 0 else None


def _book_levels(levels: Any, *, reverse: bool, limit: int) -> list[tuple[float, int]]:
    out = []
    for level in list(levels or [])[:limit]:
        price = _positive_float(getattr(level, "price", None))
        size = int(float(getattr(level, "size", 0) or 0))
        if price and size > 0:
            out.append((price, size))
    return sorted(out, key=lambda item: item[0], reverse=reverse)


def _dom_side(value: Any) -> str:
    return {0: "ask", 1: "bid"}.get(value, str(value) if value is not None else "")


def _dom_operation(value: Any) -> str:
    return {0: "insert", 1: "update", 2: "delete"}.get(value, str(value) if value is not None else "")


def _parse_clock(value: str):
    return datetime.strptime(value, "%H:%M:%S").time()


def _bar_time_utc(bar: Any) -> datetime:
    raw = getattr(bar, "date", None) or getattr(bar, "time", None) or getattr(bar, "timestamp", None)
    timestamp = datetime.now(timezone.utc) if raw is None else _to_datetime(raw)
    if timestamp.tzinfo is None:
        timestamp = timestamp.replace(tzinfo=timezone.utc)
    return timestamp.astimezone(timezone.utc)


def _to_datetime(value: Any) -> datetime:
    if isinstance(value, datetime):
        return value
    try:
        return pd.Timestamp(value).to_pydatetime()
    except Exception:
        return datetime.now(timezone.utc)
