from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal, ROUND_FLOOR
from typing import Any, Protocol

from .logger import MultiStrategyLogger
from .registry import PositionRegistry


class FillReceiver(Protocol):
    strategy_name: str

    def on_broker_fill(self, order_id: str, timestamp: datetime, quantity: int, price: float, commission: float = 0.0) -> None:
        ...


@dataclass
class TrackedOrder:
    strategy: str
    symbol: str
    order_id: str
    role: str
    receiver: FillReceiver
    action: str


@dataclass
class ForcedFlattenWatch:
    reason: str
    receiver: FillReceiver
    expires_at: datetime
    entry_order_ids: set[str]


class AccountPositionReceiver:
    strategy_name = "account"

    def on_broker_fill(self, order_id: str, timestamp: datetime, quantity: int, price: float, commission: float = 0.0) -> None:
        pass


PROTECTIVE_EXIT_ROLES = {"tp1", "tp2", "target", "stop"}
STOP_PROTECTION_ROLES = {"stop"}
PENDING_EXIT_ACK_STATUSES = {"", "PendingSubmit", "ApiPending"}
EXIT_CANCEL_OR_INACTIVE_STATUSES = {"PendingCancel", "ApiCancelled", "Cancelled", "Inactive"}
FORCED_FLATTEN_DONE_STATUSES = {"Filled", "Cancelled", "ApiCancelled"}
FORCED_FLATTEN_WATCH_SECONDS = 8 * 60 * 60


class SharedBroker:
    def __init__(
        self,
        ib: Any,
        contracts: dict[str, Any],
        registry: PositionRegistry,
        logger: MultiStrategyLogger,
        dry_run: bool = False,
        *,
        trailing_stop_enabled: bool = True,
        trailing_activation_bps: float = 50.0,
        trailing_distance_bps: float = 35.0,
        trailing_min_step_bps: float = 5.0,
        runner_target_enabled: bool = True,
        runner_target_r_multiple: float = 6.0,
        forced_flatten_cooldown_seconds: int = 3600,
        stop_loss_cooldown_seconds: int = 600,
        manage_account_positions: bool = True,
        quarantine_unmanaged_positions: bool = True,
        fail_closed_on_depth_permission_error: bool = False,
        depth_required_strategies: set[str] | None = None,
        software_stop_breach_enabled: bool = False,
        exit_ack_timeout_seconds: int = 3,
        exit_ack_max_wait_seconds: int = 30,
        price_tick_size: float = 0.01,
    ) -> None:
        self.ib = ib
        self.contracts = contracts
        self.account_contracts: dict[str, Any] = {}
        self.registry = registry
        self.logger = logger
        self.dry_run = dry_run
        self.trailing_stop_enabled = trailing_stop_enabled
        self.trailing_activation_bps = trailing_activation_bps
        self.trailing_distance_bps = trailing_distance_bps
        self.trailing_min_step_bps = trailing_min_step_bps
        self.runner_target_enabled = runner_target_enabled
        self.runner_target_r_multiple = runner_target_r_multiple
        self.forced_flatten_cooldown_seconds = forced_flatten_cooldown_seconds
        self.stop_loss_cooldown_seconds = max(0, stop_loss_cooldown_seconds)
        self.manage_account_positions = manage_account_positions
        self.quarantine_unmanaged_positions = quarantine_unmanaged_positions
        self.fail_closed_on_depth_permission_error = fail_closed_on_depth_permission_error
        self.depth_required_strategies = set(depth_required_strategies or {"absorption", "pullback"})
        self.software_stop_breach_enabled = software_stop_breach_enabled
        self.exit_ack_timeout_seconds = max(1, exit_ack_timeout_seconds)
        self.exit_ack_max_wait_seconds = max(self.exit_ack_timeout_seconds, exit_ack_max_wait_seconds)
        self.price_tick_size = max(float(price_tick_size), 0.0001)
        self._exit_pending_ack: dict[str, tuple[datetime, int]] = {}
        self._expected_exit_cancels: set[str] = set()
        self._exit_amendments: dict[str, datetime] = {}
        self._execution_commission_routes: dict[str, tuple[FillReceiver, datetime]] = {}
        self._seen_commission_exec_ids: set[str] = set()
        self.tracked_by_ib_id: dict[int, TrackedOrder] = {}
        self.tracked_by_ref: dict[str, TrackedOrder] = {}
        self.long_positions: dict[tuple[str, str], int] = {}
        self.short_positions: dict[tuple[str, str], int] = {}
        self.long_avg_prices: dict[tuple[str, str], float] = {}
        self.exit_reservations: dict[tuple[str, str], int] = {}
        self.exit_reservations_by_ref: dict[str, int] = {}
        self.stop_orders_by_position: dict[tuple[str, str], str] = {}
        self.pending_stop_quantities: dict[tuple[str, str], int] = {}
        self.pending_target_quantities: dict[str, int] = {}
        self.initial_stop_prices: dict[tuple[str, str], float] = {}
        self.high_watermarks: dict[tuple[str, str], float] = {}
        self.position_receivers: dict[tuple[str, str], FillReceiver] = {}
        self.pending_forced_flattens: set[tuple[str, str]] = set()
        self.forced_flatten_orders: dict[tuple[str, str], str] = {}
        self.forced_flatten_reasons: dict[tuple[str, str], str] = {}
        self.forced_flatten_watch: dict[tuple[str, str], ForcedFlattenWatch] = {}
        self.forced_flatten_reason_counts: dict[str, int] = {}
        self.stop_breach_flattened: set[tuple[str, str]] = set()
        self.unmanaged_position_flattened: set[tuple[str, str]] = set()
        self.symbol_cooldowns: dict[str, datetime] = {}
        self.unmanaged_positions: dict[str, int] = {}
        self.depth_blocked_symbols: set[str] = set()
        self.account_receiver = AccountPositionReceiver()
        self.trading_action_plans: dict[tuple[str, str], dict[str, Any]] = {}
        self.order_quantities: dict[str, int] = {}
        self.order_filled_quantities: dict[str, int] = {}
        self.current_stop_prices: dict[tuple[str, str], float] = {}
        self._seen_exec_ids: set[str] = set()
        self._bind_ib_lifecycle_events()

    def submit_pullback_entry(self, receiver: FillReceiver, order: Any, signal: Any) -> None:
        self._lock_or_raise(order.symbol, receiver.strategy_name, order.created_at, "pullback_entry")
        ib_order = self._limit_order("BUY", order.quantity, order.limit_price)
        self._place(receiver, order.symbol, order.order_id, "entry", ib_order)

    def submit_pullback_bracket(self, receiver: FillReceiver, position: Any, orders: list[Any]) -> None:
        for order in orders:
            ib_order = self._stop_order("SELL", order.quantity, order.limit_price) if order.role == "stop" else self._limit_order("SELL", order.quantity, order.limit_price)
            self._place(receiver, order.symbol, order.order_id, order.role, ib_order)

    def flatten_pullback(self, receiver: FillReceiver, order: Any) -> None:
        self._place(receiver, order.symbol, order.order_id, "flatten", self._market_order("SELL", order.quantity))

    def submit_orm_entry(self, receiver: FillReceiver, order: Any, signal: Any) -> None:
        self._lock_or_raise(order.symbol, receiver.strategy_name, order.created_at, "opening_range_entry")
        self._place(receiver, order.symbol, order.order_id, "entry", self._limit_order("BUY", order.quantity, order.price))

    def submit_orm_bracket(self, receiver: FillReceiver, symbol: str, stop_order: Any, target_order: Any) -> None:
        self._place(receiver, symbol, stop_order.order_id, "stop", self._stop_order("SELL", stop_order.quantity, stop_order.price))
        self._place(receiver, symbol, target_order.order_id, "target", self._limit_order("SELL", target_order.quantity, target_order.price))

    def flatten_orm(self, receiver: FillReceiver, order: Any) -> None:
        self._place(receiver, order.symbol, order.order_id, "flatten", self._market_order("SELL", order.quantity))

    def submit_absorption_order(self, receiver: FillReceiver, order: Any) -> None:
        if order.role == "entry":
            self._lock_or_raise(order.symbol, receiver.strategy_name, order.created_at or datetime.now(timezone.utc), "absorption_entry")
        ib_order = self._order_from_absorption(order)
        self._place(receiver, order.symbol, order.order_id, order.role, ib_order)

    def sync_protective_order_quantities(self, receiver: FillReceiver, orders: list[Any]) -> None:
        """Push strategy-side protection resizing through to live IB orders."""
        for order in orders:
            order_id = str(getattr(order, "order_id", "") or "")
            tracked = self.tracked_by_ref.get(order_id)
            if tracked is None or tracked.receiver is not receiver:
                continue
            if tracked.role not in PROTECTIVE_EXIT_ROLES or tracked.action != "SELL":
                continue
            if int(getattr(order, "filled_qty", 0) or 0) > 0:
                continue
            quantity = int(getattr(order, "qty", 0) or 0)
            if quantity <= 0:
                continue
            if tracked.role == "stop":
                self._sync_stop_quantity(tracked.strategy, tracked.symbol, quantity)
            else:
                self._sync_target_quantity(tracked.strategy, tracked.symbol, order_id, quantity)

    def sync_account_positions(self, timestamp: datetime) -> None:
        positions = [] if self.dry_run else self._account_positions()
        if not self.manage_account_positions:
            snapshot = []
            account_quantities: dict[str, int] = {}
            for position in positions:
                contract = getattr(position, "contract", None)
                symbol = str(getattr(contract, "symbol", "") or "")
                if not symbol:
                    continue
                if contract is not None:
                    self.account_contracts[symbol] = contract
                quantity = int(float(getattr(position, "position", 0) or 0))
                account_quantities[symbol] = quantity
                snapshot.append(
                    {
                        "symbol": symbol,
                        "quantity": quantity,
                        "avg_price": float(getattr(position, "avgCost", 0.0) or 0.0),
                        "sec_type": str(getattr(contract, "secType", "") or ""),
                        "con_id": getattr(contract, "conId", ""),
                    }
                )
            if self.quarantine_unmanaged_positions:
                managed_quantities: dict[str, int] = {}
                for (strategy, symbol), quantity in self.long_positions.items():
                    if strategy != "account":
                        managed_quantities[symbol] = managed_quantities.get(symbol, 0) + quantity
                for (strategy, symbol), quantity in self.short_positions.items():
                    if strategy != "account":
                        managed_quantities[symbol] = managed_quantities.get(symbol, 0) - quantity
                symbols = set(account_quantities) | set(managed_quantities) | set(self.unmanaged_positions)
                for symbol in symbols:
                    unmanaged = account_quantities.get(symbol, 0) - managed_quantities.get(symbol, 0)
                    previous = self.unmanaged_positions.get(symbol, 0)
                    if unmanaged:
                        self.unmanaged_positions[symbol] = unmanaged
                        if previous != unmanaged:
                            self.logger.event(
                                "unmanaged_position_quarantined",
                                {
                                    "symbol": symbol,
                                    "account_quantity": account_quantities.get(symbol, 0),
                                    "managed_quantity": managed_quantities.get(symbol, 0),
                                    "unmanaged_quantity": unmanaged,
                                    "time": timestamp.isoformat(),
                                },
                            )
                        if self.registry.owner(symbol) is None:
                            self.registry.lock_position(symbol, "account", timestamp, "unmanaged_position_quarantine")
                    elif previous:
                        self.unmanaged_positions.pop(symbol, None)
                        self.registry.unlock_if_owner(symbol, "account")
                        self.logger.event("unmanaged_position_quarantine_cleared", {"symbol": symbol, "time": timestamp.isoformat()})
            self.logger.event("account_positions_snapshot", {"positions": snapshot, "time": timestamp.isoformat(), "managed": False})
            return
        seen_account_symbols = set()
        synced = []
        snapshot = []
        for position in positions:
            contract = getattr(position, "contract", None)
            symbol = str(getattr(contract, "symbol", "") or "")
            if not symbol:
                continue
            if contract is not None:
                self.account_contracts[symbol] = contract
            quantity = int(float(getattr(position, "position", 0) or 0))
            seen_account_symbols.add(symbol)
            snapshot.append(
                {
                    "symbol": symbol,
                    "quantity": quantity,
                    "avg_price": float(getattr(position, "avgCost", 0.0) or 0.0),
                    "sec_type": str(getattr(contract, "secType", "") or ""),
                    "con_id": getattr(contract, "conId", ""),
                }
            )
            if any(strategy != "account" and tracked_symbol == symbol and tracked_qty > 0 for (strategy, tracked_symbol), tracked_qty in self.long_positions.items()):
                continue
            key = ("account", symbol)
            if quantity > 0:
                avg_price = float(getattr(position, "avgCost", 0.0) or 0.0)
                self.long_positions[key] = quantity
                self.short_positions.pop(key, None)
                self.long_avg_prices[key] = avg_price
                self.position_receivers[key] = self.account_receiver
                self.registry.lock_position(symbol, "account", timestamp)
                synced.append({"symbol": symbol, "quantity": quantity, "avg_price": avg_price, "side": "LONG"})
                continue
            if quantity < 0:
                avg_price = float(getattr(position, "avgCost", 0.0) or 0.0)
                self.short_positions[key] = abs(quantity)
                self.long_positions.pop(key, None)
                self.long_avg_prices[key] = avg_price
                self.position_receivers[key] = self.account_receiver
                self.registry.lock_position(symbol, "account", timestamp)
                synced.append({"symbol": symbol, "quantity": abs(quantity), "avg_price": avg_price, "side": "SHORT"})
                continue
            self._clear_position_state(key)
            self.registry.unlock_if_owner(symbol, "account")
        for key in set(list(self.long_positions) + list(self.short_positions)):
            strategy, symbol = key
            if strategy != "account" or symbol in seen_account_symbols:
                continue
            self._clear_position_state(key)
            self.registry.unlock_if_owner(symbol, "account")
        if synced:
            self.logger.event("account_positions_synced", {"positions": synced, "time": timestamp.isoformat()})
        self.logger.event("account_positions_snapshot", {"positions": snapshot, "time": timestamp.isoformat()})

    def account_position_snapshot(self) -> list[dict[str, Any]]:
        """Return authoritative, non-zero IB account positions.

        This deliberately bypasses strategy ownership. Lifecycle safety decisions
        must use the account's net inventory, not potentially stale local state.
        """
        snapshot: list[dict[str, Any]] = []
        for position in self._account_positions():
            contract = getattr(position, "contract", None)
            symbol = str(getattr(contract, "symbol", "") or "")
            quantity = int(float(getattr(position, "position", 0) or 0))
            if not symbol or quantity == 0:
                continue
            if contract is not None:
                self.account_contracts[symbol] = contract
            snapshot.append(
                {
                    "symbol": symbol,
                    "quantity": quantity,
                    "avg_price": float(getattr(position, "avgCost", 0.0) or 0.0),
                    "contract": contract,
                }
            )
        return snapshot

    def cancel_all_working_orders(self) -> None:
        """Cancel every order tracked by this process before account flattening."""
        for order_id in list(self.tracked_by_ref):
            self.cancel(order_id)

    def has_working_orders(self) -> bool:
        if self.dry_run:
            return False
        terminal = {"Filled", "Cancelled", "ApiCancelled", "Inactive"}
        for trade in self.ib.trades():
            order = getattr(trade, "order", None)
            order_id = getattr(order, "orderId", None)
            if order_id not in self.tracked_by_ib_id:
                continue
            status = str(getattr(getattr(trade, "orderStatus", None), "status", "") or "")
            if status not in terminal:
                return True
        return False

    def clear_local_state_after_account_flat(self) -> None:
        """Discard strategy inventory only after IB confirms flat and no orders work."""
        for key in set(self.long_positions) | set(self.short_positions):
            strategy, symbol = key
            self._clear_position_state(key)
            self.registry.unlock_if_owner(symbol, strategy)
        for symbol in list(self.unmanaged_positions):
            self.registry.unlock_if_owner(symbol, "account")
        self.unmanaged_positions.clear()

    def flatten_account_positions(
        self,
        timestamp: datetime,
        reason: str,
        *,
        shorts_only: bool = False,
    ) -> int:
        """Flatten IB's current net account inventory and return order count.

        Account-level flattening is intentionally independent from restored
        strategy state. This is the final safety net for stale state, crashes,
        manual positions, and any accidental short inventory.
        """
        submitted = 0
        for position in self.account_position_snapshot():
            symbol = str(position["symbol"])
            quantity = int(position["quantity"])
            if shorts_only and quantity >= 0:
                continue
            key = ("account", symbol)
            self.position_receivers[key] = self.account_receiver
            self.long_avg_prices[key] = float(position["avg_price"])
            if quantity > 0:
                self.long_positions[key] = quantity
                self.short_positions.pop(key, None)
                side = "SELL"
                close_quantity = quantity
            else:
                self.short_positions[key] = abs(quantity)
                self.long_positions.pop(key, None)
                side = "BUY"
                close_quantity = abs(quantity)
            self.registry.lock_position(symbol, "account", timestamp, reason)
            self._submit_window_flatten(key, close_quantity, side, timestamp, reason)
            submitted += 1
        return submitted

    def flatten_all_positions(self, timestamp: datetime, reason: str = "trading_window_close") -> None:
        for key, quantity in list(self.long_positions.items()):
            if quantity > 0:
                self._submit_window_flatten(key, quantity, "SELL", timestamp, reason)
        for key, quantity in list(self.short_positions.items()):
            if quantity > 0:
                self._submit_window_flatten(key, quantity, "BUY", timestamp, reason)

    def _submit_window_flatten(self, key: tuple[str, str], quantity: int, side: str, timestamp: datetime, reason: str) -> None:
        strategy, symbol = key
        if key in self.pending_forced_flattens:
            self._retry_forced_flatten(strategy, symbol, quantity, timestamp, reason)
            return
        receiver = self.position_receivers.get(key) or self._receiver_for_position(strategy, symbol)
        if receiver is None:
            self.logger.event("forced_flatten_skipped_no_receiver", {"strategy": strategy, "symbol": symbol, "quantity": quantity, "reason": reason})
            return
        order_id = f"flatten-{strategy}-{symbol}-{timestamp.strftime('%Y%m%d%H%M%S')}"
        self.pending_forced_flattens.add(key)
        self.forced_flatten_orders[key] = order_id
        self.forced_flatten_reasons[key] = reason
        self.forced_flatten_watch[key] = self._forced_flatten_watch(key, reason, receiver, timestamp)
        payload: dict = {"strategy": strategy, "symbol": symbol, "order_id": order_id, "quantity": quantity, "reason": reason, "time": timestamp.isoformat()}
        if side == "BUY":
            payload["side"] = "BUY_TO_COVER"
        self.logger.event("forced_flatten_submitted", payload)
        self._record_forced_flatten(reason)
        self._place(receiver, symbol, order_id, "flatten", self._market_order(side, quantity))

    def _record_forced_flatten(self, reason: str) -> None:
        self.forced_flatten_reason_counts[reason] = self.forced_flatten_reason_counts.get(reason, 0) + 1

    def has_open_positions(self) -> bool:
        return any(quantity > 0 for quantity in self.long_positions.values()) or any(quantity > 0 for quantity in self.short_positions.values())

    def is_symbol_cooling_down(self, symbol: str, timestamp: datetime) -> bool:
        until = self.symbol_cooldowns.get(symbol)
        if until is None:
            return False
        if timestamp < until:
            return True
        self.symbol_cooldowns.pop(symbol, None)
        return False

    def entry_block_reason(self, symbol: str, strategy: str) -> str | None:
        if self.unmanaged_positions.get(symbol, 0):
            return "unmanaged_account_position"
        if strategy in self.depth_required_strategies and symbol in self.depth_blocked_symbols:
            return "depth_permissions_unavailable"
        return None

    def is_entry_blocked(self, symbol: str, strategy: str) -> bool:
        return self.entry_block_reason(symbol, strategy) is not None

    def cancel(self, order_id: str) -> None:
        tracked = self.tracked_by_ref.get(order_id)
        if not tracked:
            return
        self._exit_pending_ack.pop(order_id, None)
        self.pending_target_quantities.pop(order_id, None)
        self._release_exit_reservation(order_id)
        if self.dry_run:
            return
        for trade in self.ib.trades():
            if getattr(getattr(trade, "order", None), "orderId", None) in self.tracked_by_ib_id:
                if self.tracked_by_ib_id[trade.order.orderId].order_id == order_id:
                    status = str(getattr(getattr(trade, "orderStatus", None), "status", "") or "")
                    if status in FORCED_FLATTEN_DONE_STATUSES | {"Inactive"}:
                        return
                    if tracked.action == "SELL" and tracked.role in PROTECTIVE_EXIT_ROLES:
                        self._expected_exit_cancels.add(order_id)
                    self.ib.cancelOrder(trade.order)

    def _place(self, receiver: FillReceiver, symbol: str, order_id: str, role: str, ib_order: Any) -> None:
        action = str(getattr(ib_order, "action", "") or "")
        if action == "BUY" and not self._allow_long_entry(receiver.strategy_name, symbol, order_id, role):
            return
        # Long-only invariant: non-flatten SELL orders require an existing long position.
        # This is a hard stop that prevents accidental shorts after a state loss.
        if action == "SELL" and role not in {"flatten"}:
            long_qty = self.long_positions.get((receiver.strategy_name, symbol), 0)
            if long_qty <= 0:
                self.logger.event("sell_rejected_no_long_position", {
                    "strategy": receiver.strategy_name, "symbol": symbol,
                    "order_id": order_id, "role": role,
                })
                return
        if action == "SELL" and not self._reserve_exit_quantity(receiver.strategy_name, symbol, order_id, role, ib_order):
            return
        tracked = TrackedOrder(receiver.strategy_name, symbol, order_id, role, receiver, action)
        self.tracked_by_ref[order_id] = tracked
        self.order_quantities[order_id] = int(float(getattr(ib_order, "totalQuantity", 0) or 0))
        self.order_filled_quantities.setdefault(order_id, 0)
        self.logger.csv(
            "orders",
            {
                "timestamp": datetime.now(timezone.utc),
                "strategy": receiver.strategy_name,
                "symbol": symbol,
                "order_id": order_id,
                "role": role,
                "action": getattr(ib_order, "action", ""),
                "order_type": getattr(ib_order, "orderType", ""),
                "quantity": getattr(ib_order, "totalQuantity", ""),
                "limit_price": getattr(ib_order, "lmtPrice", ""),
                "stop_price": getattr(ib_order, "auxPrice", ""),
                "oca_group": getattr(ib_order, "ocaGroup", ""),
                "oca_type": getattr(ib_order, "ocaType", ""),
                "dry_run": self.dry_run,
            },
        )
        self._log_trading_action(
            tracked,
            ib_order,
            event="submitted",
            timestamp=datetime.now(timezone.utc),
            price=self._order_price(ib_order),
            filled_status="no-fill",
        )
        if self.dry_run:
            return
        # Tag every order with its internal ID so we can re-identify it after a reconnect.
        try:
            setattr(ib_order, "orderRef", order_id)
        except AttributeError:
            pass
        trade = self.ib.placeOrder(self._contract_for(symbol), ib_order)
        ib_id = getattr(getattr(trade, "order", ib_order), "orderId", None)
        if ib_id is not None:
            self.tracked_by_ib_id[ib_id] = tracked
        if role in STOP_PROTECTION_ROLES:
            self._exit_pending_ack[order_id] = (datetime.now(timezone.utc), 0)
        self._log_broker_open_order(trade, source="place_order_return")
        self._log_broker_order_status(trade, source="place_order_return")
        fill_event = getattr(trade, "fillEvent", None)
        if fill_event is not None:
            fill_event += lambda trade, fill, oid=order_id: self._on_fill(oid, fill)

    def _on_fill(self, order_id: str, fill: Any) -> None:
        tracked = self.tracked_by_ref.get(order_id)
        if tracked is None:
            return
        execution = getattr(fill, "execution", None)
        if execution is None:
            return
        exec_id = getattr(execution, "execId", None)
        if exec_id:
            if exec_id in self._seen_exec_ids:
                return
            self._seen_exec_ids.add(exec_id)
        timestamp = getattr(execution, "time", None) or datetime.now(timezone.utc)
        if timestamp.tzinfo is None:
            timestamp = timestamp.replace(tzinfo=timezone.utc)
        quantity = int(float(getattr(execution, "shares", 0) or 0))
        price = float(getattr(execution, "price", 0.0) or 0.0)
        report = getattr(fill, "commissionReport", None)
        commission = float(getattr(report, "commission", 0.0) or 0.0)
        if exec_id:
            self._execution_commission_routes[exec_id] = (tracked.receiver, timestamp)
        filled_quantity = self.order_filled_quantities.get(order_id, 0) + quantity
        self.order_filled_quantities[order_id] = filled_quantity
        requested_quantity = self.order_quantities.get(order_id, filled_quantity)
        try:
            self._apply_position_fill(tracked, order_id, quantity, price, timestamp, filled_quantity, requested_quantity)
        except Exception as exc:
            # The execution is authoritative.  Record and deliver it even when
            # optional stop/target maintenance fails, otherwise broker and
            # strategy inventories diverge and the symbol remains locked.
            self.logger.event(
                "post_fill_maintenance_failed",
                {
                    "strategy": tracked.strategy,
                    "symbol": tracked.symbol,
                    "order_id": order_id,
                    "exec_id": exec_id or "",
                    "error": type(exc).__name__,
                    "message": str(exc),
                    "time": timestamp.isoformat(),
                },
            )
        self.logger.csv("fills", {"timestamp": timestamp, "strategy": tracked.strategy, "symbol": tracked.symbol, "order_id": order_id, "role": tracked.role, "quantity": quantity, "price": price})
        filled_status = "full fill" if filled_quantity >= requested_quantity else "partial fill"
        self._log_trading_action(
            tracked,
            None,
            event="filled",
            timestamp=timestamp,
            price=price,
            filled_status=filled_status,
        )
        tracked.receiver.on_broker_fill(order_id, timestamp, quantity, price, commission)

    def _log_trading_action(
        self,
        tracked: TrackedOrder,
        ib_order: Any | None,
        *,
        event: str,
        timestamp: datetime,
        price: float | str,
        filled_status: str,
    ) -> None:
        key = (tracked.strategy, tracked.symbol)
        plan = self.trading_action_plans.setdefault(key, {"tp1": "", "tp2": "", "stop": ""})
        if ib_order is not None:
            if tracked.role == "entry":
                plan.clear()
                plan.update({"tp1": "", "tp2": "", "stop": ""})
            if tracked.role == "tp1":
                plan["tp1"] = self._order_price(ib_order)
            elif tracked.role == "target":
                plan["tp1"] = self._order_price(ib_order)
            elif tracked.role == "tp2":
                plan["tp2"] = self._order_price(ib_order)
            elif tracked.role == "stop":
                plan["stop"] = self._order_price(ib_order)
        self.logger.csv(
            "trading_actions",
            {
                "timestamp": timestamp,
                "stock": tracked.symbol,
                "buy/sell": tracked.action,
                "price": price,
                "tp1": plan.get("tp1", ""),
                "tp2": plan.get("tp2", ""),
                "stop": plan.get("stop", ""),
                "filled_status": filled_status,
                "strategy": tracked.strategy,
                "order_id": tracked.order_id,
                "role": tracked.role,
                "quantity": self.order_quantities.get(tracked.order_id, ""),
                "filled_quantity": self.order_filled_quantities.get(tracked.order_id, 0),
                "event": event,
            },
        )

    def _order_price(self, ib_order: Any) -> float | str:
        order_type = str(getattr(ib_order, "orderType", "") or "")
        if order_type == "MKT":
            return "MKT"
        if order_type == "STP":
            return float(getattr(ib_order, "auxPrice", 0.0) or 0.0)
        return float(getattr(ib_order, "lmtPrice", 0.0) or 0.0)

    def _working_flatten_order_id(self, strategy: str, symbol: str, *, exclude_order_id: str) -> str | None:
        """Return an already-working flatten for this strategy position.

        Flatten orders intentionally bypass normal target reservations so they can
        close the whole position.  That also means two flatten paths can otherwise
        size themselves from the same pre-fill inventory and create a short.  Treat
        a submitted (or filled-but-not-yet-reconciled) flatten as authoritative until
        its fill clears the tracked position.  A terminal cancellation is the only
        state in which another flatten may safely replace it.
        """
        cancelled_statuses = {"Cancelled", "ApiCancelled", "Inactive"}
        trades_by_order_id = {
            getattr(getattr(trade, "order", None), "orderId", None): trade
            for trade in self.ib.trades()
        } if not self.dry_run else {}
        for candidate_id, tracked in self.tracked_by_ref.items():
            if candidate_id == exclude_order_id:
                continue
            if tracked.strategy != strategy or tracked.symbol != symbol or tracked.role != "flatten":
                continue
            ib_id = next((value for value, candidate in self.tracked_by_ib_id.items() if candidate.order_id == candidate_id), None)
            trade = trades_by_order_id.get(ib_id)
            status = str(getattr(getattr(trade, "orderStatus", None), "status", "") or "")
            if status not in cancelled_statuses:
                return candidate_id
        return None

    def _reserve_exit_quantity(self, strategy: str, symbol: str, order_id: str, role: str, ib_order: Any) -> bool:
        requested = int(float(getattr(ib_order, "totalQuantity", 0) or 0))
        key = (strategy, symbol)
        long_qty = self.long_positions.get(key, 0)
        if role == "flatten":
            working_flatten = self._working_flatten_order_id(strategy, symbol, exclude_order_id=order_id)
            if working_flatten is not None:
                self.logger.event(
                    "duplicate_flatten_rejected",
                    {
                        "strategy": strategy,
                        "symbol": symbol,
                        "order_id": order_id,
                        "working_order_id": working_flatten,
                        "requested_qty": requested,
                        "long_qty": long_qty,
                    },
                )
                return False
            self._cancel_position_exits(strategy, symbol)
            long_qty = self.long_positions.get(key, 0)
            if requested > long_qty:
                setattr(ib_order, "totalQuantity", long_qty)
                requested = long_qty
            if requested <= 0:
                self.logger.event(
                    "sell_order_rejected_no_long_inventory",
                    {"strategy": strategy, "symbol": symbol, "order_id": order_id, "role": role, "requested_qty": requested, "long_qty": long_qty, "reserved_exit_qty": self.exit_reservations.get(key, 0)},
                )
                return False
            return True
        if role == "stop":
            if requested <= 0 or long_qty <= 0:
                self.logger.event(
                    "sell_order_rejected_no_long_inventory",
                    {"strategy": strategy, "symbol": symbol, "order_id": order_id, "role": role, "requested_qty": requested, "long_qty": long_qty, "reserved_exit_qty": self.exit_reservations.get(key, 0)},
                )
                return False
            if requested > long_qty:
                setattr(ib_order, "totalQuantity", long_qty)
                self.logger.event(
                    "sell_order_reduced_to_long_inventory",
                    {"strategy": strategy, "symbol": symbol, "order_id": order_id, "role": role, "requested_qty": requested, "accepted_qty": long_qty, "long_qty": long_qty, "reserved_exit_qty": self.exit_reservations.get(key, 0)},
                )
            self.stop_orders_by_position[key] = order_id
            stop_price = float(getattr(ib_order, "auxPrice", 0.0) or 0.0)
            self.initial_stop_prices.setdefault(key, stop_price)
            self.current_stop_prices[key] = stop_price
            return True
        available = max(0, self.long_positions.get(key, 0) - self.exit_reservations.get(key, 0))
        if requested <= 0 or available <= 0:
            self.logger.event(
                "sell_order_rejected_no_long_inventory",
                {"strategy": strategy, "symbol": symbol, "order_id": order_id, "role": role, "requested_qty": requested, "long_qty": self.long_positions.get(key, 0), "reserved_exit_qty": self.exit_reservations.get(key, 0)},
            )
            return False
        if requested > available:
            setattr(ib_order, "totalQuantity", available)
            self.logger.event(
                "sell_order_reduced_to_long_inventory",
                {"strategy": strategy, "symbol": symbol, "order_id": order_id, "role": role, "requested_qty": requested, "accepted_qty": available, "long_qty": self.long_positions.get(key, 0), "reserved_exit_qty": self.exit_reservations.get(key, 0)},
            )
            requested = available
        self.exit_reservations[key] = self.exit_reservations.get(key, 0) + requested
        self.exit_reservations_by_ref[order_id] = requested
        return True

    def _allow_long_entry(self, strategy: str, symbol: str, order_id: str, role: str) -> bool:
        if role != "entry":
            return True
        long_qty = self.long_positions.get((strategy, symbol), 0)
        if long_qty <= 0:
            return True
        self.logger.event(
            "buy_order_rejected_existing_long_inventory",
            {"strategy": strategy, "symbol": symbol, "order_id": order_id, "role": role, "long_qty": long_qty},
        )
        return False

    def _mark_exit_oca(self, strategy: str, symbol: str, ib_order: Any) -> None:
        setattr(ib_order, "ocaGroup", f"{strategy}-{symbol}-exit")
        setattr(ib_order, "ocaType", 2)

    def _apply_position_fill(
        self,
        tracked: TrackedOrder,
        order_id: str,
        quantity: int,
        fill_price: float,
        timestamp: datetime,
        filled_quantity: int,
        requested_quantity: int,
    ) -> None:
        key = (tracked.strategy, tracked.symbol)
        if tracked.action == "BUY":
            if tracked.role == "flatten" and self.short_positions.get(key, 0) > 0:
                current_short = self.short_positions.get(key, 0)
                self.short_positions[key] = max(0, current_short - quantity)
                if self.short_positions[key] == 0:
                    self._clear_position_state(key)
                    self.registry.unlock_if_owner(tracked.symbol, tracked.strategy)
                return
            old_qty = self.long_positions.get(key, 0)
            old_avg = self.long_avg_prices.get(key, 0.0)
            new_qty = old_qty + quantity
            self.long_positions[key] = new_qty
            if new_qty > 0:
                self.long_avg_prices[key] = ((old_avg * old_qty) + (fill_price * quantity)) / new_qty if fill_price else old_avg
                self.position_receivers[key] = tracked.receiver
                self._sync_stop_quantity(tracked.strategy, tracked.symbol, new_qty)
                if key in self.pending_forced_flattens or self._forced_flatten_watch_active(key, timestamp, order_id):
                    self._flatten_late_long_fill(tracked.strategy, tracked.symbol, new_qty, tracked.receiver, timestamp, order_id)
            return
        if tracked.action != "SELL":
            return
        reserved = self.exit_reservations_by_ref.get(order_id, 0)
        release = min(reserved, quantity)
        if release:
            self.exit_reservations_by_ref[order_id] = reserved - release
            self.exit_reservations[key] = max(0, self.exit_reservations.get(key, 0) - release)
        current = self.long_positions.get(key, 0)
        entry_avg = self.long_avg_prices.get(key, 0.0)
        if quantity > current:
            excess = quantity - current
            self.logger.event(
                "sell_fill_exceeded_long_inventory",
                {
                    "strategy": tracked.strategy,
                    "symbol": tracked.symbol,
                    "order_id": order_id,
                    "fill_qty": quantity,
                    "long_qty_before_fill": current,
                    "short_qty_created": excess,
                },
            )
            # A long-only system must never normalize an oversell to zero and
            # forget the resulting account short. Preserve the excess as short
            # inventory and immediately submit a buy-to-cover.
            self._cancel_remaining_exits_on_ib(tracked.strategy, tracked.symbol, exclude_order_id=order_id)
            self._clear_position_state(key)
            self.short_positions[key] = excess
            self.long_avg_prices[key] = fill_price
            self.position_receivers[key] = tracked.receiver
            self.registry.lock_position(tracked.symbol, tracked.strategy, timestamp, "long_only_emergency_cover")
            self.logger.event(
                "long_only_short_emergency_cover",
                {
                    "strategy": tracked.strategy,
                    "symbol": tracked.symbol,
                    "source_order_id": order_id,
                    "quantity": excess,
                    "time": timestamp.isoformat(),
                },
            )
            self._submit_window_flatten(key, excess, "BUY", timestamp, "long_only_short_emergency_cover")
            return
        self.long_positions[key] = max(0, current - quantity)
        if tracked.role == "stop" and entry_avg > 0 and fill_price < entry_avg:
            self._start_symbol_cooldown(tracked.symbol, timestamp, "stop_loss")
        if self.long_positions[key] == 0:
            self._cancel_remaining_exits_on_ib(tracked.strategy, tracked.symbol, exclude_order_id=order_id)
            self._clear_position_state(key)
            self.registry.unlock_if_owner(tracked.symbol, tracked.strategy)
        else:
            if tracked.role == "stop":
                if filled_quantity >= requested_quantity:
                    remaining = self.long_positions.get(key, 0)
                    self.logger.event(
                        "stop_filled_position_remaining_flatten",
                        {"strategy": tracked.strategy, "symbol": tracked.symbol, "order_id": order_id, "quantity": remaining, "time": timestamp.isoformat()},
                    )
                    self._submit_window_flatten(key, remaining, "SELL", timestamp, "stop_filled_position_remaining")
                return
            self._sync_stop_quantity(tracked.strategy, tracked.symbol, self.long_positions[key])
            if tracked.role == "tp1":
                remaining_target_qty = self.exit_reservations_by_ref.get(order_id, 0)
                if remaining_target_qty > 0:
                    self._marketize_remaining_target(tracked.strategy, tracked.symbol, order_id, remaining_target_qty)
            if self.runner_target_enabled and tracked.role in {"tp2", "target"}:
                try:
                    self._promote_stale_targets(tracked.strategy, tracked.symbol, order_id, fill_price)
                except Exception as exc:
                    # Optional runner maintenance must never interrupt authoritative
                    # execution accounting or the strategy's fill callback.
                    self.logger.event(
                        "runner_target_promotion_failed",
                        {
                            "strategy": tracked.strategy,
                            "symbol": tracked.symbol,
                            "order_id": order_id,
                            "error": type(exc).__name__,
                            "message": str(exc),
                            "time": timestamp.isoformat(),
                        },
                    )

    def _release_exit_reservation(self, order_id: str) -> None:
        tracked = self.tracked_by_ref.get(order_id)
        if tracked is None:
            return
        quantity = self.exit_reservations_by_ref.pop(order_id, 0)
        if quantity <= 0:
            return
        key = (tracked.strategy, tracked.symbol)
        self.exit_reservations[key] = max(0, self.exit_reservations.get(key, 0) - quantity)

    def _cancel_position_exits(self, strategy: str, symbol: str) -> None:
        for order_id, tracked in list(self.tracked_by_ref.items()):
            if tracked.strategy != strategy or tracked.symbol != symbol or tracked.action != "SELL":
                continue
            if tracked.role not in {"tp1", "tp2", "target", "stop"}:
                continue
            self.cancel(order_id)

    def _receiver_for_position(self, strategy: str, symbol: str) -> FillReceiver | None:
        for tracked in self.tracked_by_ref.values():
            if tracked.strategy == strategy and tracked.symbol == symbol:
                return tracked.receiver
        return None

    def _cancel_remaining_exits_on_ib(self, strategy: str, symbol: str, *, exclude_order_id: str | None = None) -> None:
        for order_id in list(self.tracked_by_ref):
            if order_id == exclude_order_id:
                continue
            tracked = self.tracked_by_ref.get(order_id)
            if tracked is None or tracked.strategy != strategy or tracked.symbol != symbol:
                continue
            if tracked.action != "SELL" or tracked.role == "flatten":
                continue
            self.cancel(order_id)

    def _clear_position_state(self, key: tuple[str, str]) -> None:
        strategy, symbol = key
        self.long_positions.pop(key, None)
        self.short_positions.pop(key, None)
        self.long_avg_prices.pop(key, None)
        self.exit_reservations.pop(key, None)
        for order_id, tracked in list(self.tracked_by_ref.items()):
            if tracked.strategy == strategy and tracked.symbol == symbol:
                self.exit_reservations_by_ref.pop(order_id, None)
                self.pending_target_quantities.pop(order_id, None)
                self._exit_pending_ack.pop(order_id, None)
                self._exit_amendments.pop(order_id, None)
                self._expected_exit_cancels.discard(order_id)
                requested = self.order_quantities.get(order_id, 0)
                filled = self.order_filled_quantities.get(order_id, 0)
                # Preserve only entry orders that may still receive a late fill;
                # all terminal exits and fully-filled entries must not participate
                # in cancellation of later positions in the same symbol.
                if tracked.role != "entry" or requested <= 0 or filled >= requested:
                    self._retire_tracked_order(order_id)
        self.high_watermarks.pop(key, None)
        self.stop_orders_by_position.pop(key, None)
        self.pending_stop_quantities.pop(key, None)
        self.initial_stop_prices.pop(key, None)
        self.current_stop_prices.pop(key, None)
        self.position_receivers.pop(key, None)
        self.pending_forced_flattens.discard(key)
        self.forced_flatten_orders.pop(key, None)
        self.forced_flatten_reasons.pop(key, None)
        self.stop_breach_flattened.discard(key)
        self.unmanaged_position_flattened.discard(key)

    def _retire_tracked_order(self, order_id: str) -> None:
        tracked = self.tracked_by_ref.pop(order_id, None)
        if tracked is None:
            return
        for ib_id, candidate in list(self.tracked_by_ib_id.items()):
            if candidate is tracked or candidate.order_id == order_id:
                self.tracked_by_ib_id.pop(ib_id, None)

    def _forced_flatten_watch_active(self, key: tuple[str, str], timestamp: datetime, order_id: str | None = None) -> bool:
        watch = self.forced_flatten_watch.get(key)
        if watch is None:
            return False
        if timestamp <= watch.expires_at:
            return order_id is None or order_id in watch.entry_order_ids
        self.forced_flatten_watch.pop(key, None)
        return False

    def _flatten_late_long_fill(self, strategy: str, symbol: str, quantity: int, receiver: FillReceiver, timestamp: datetime, order_id: str) -> None:
        key = (strategy, symbol)
        watch = self.forced_flatten_watch.get(key)
        reason = self.forced_flatten_reasons.get(key) or (watch.reason if watch else "late_entry_fill_after_flatten")
        receiver = watch.receiver if watch else receiver
        self.logger.event(
            "late_entry_fill_after_flatten_detected",
            {"strategy": strategy, "symbol": symbol, "order_id": order_id, "quantity": quantity, "reason": reason, "time": timestamp.isoformat()},
        )
        order_id = self.forced_flatten_orders.get(key)
        if order_id and self.tracked_by_ref.get(order_id) is not None:
            self._retry_forced_flatten(strategy, symbol, quantity, timestamp, reason)
            return
        self.pending_forced_flattens.discard(key)
        self.forced_flatten_orders.pop(key, None)
        self.forced_flatten_reasons.pop(key, None)
        self.position_receivers[key] = receiver
        self._submit_window_flatten(key, quantity, "SELL", timestamp, reason)

    def _retry_forced_flatten(self, strategy: str, symbol: str, quantity: int, timestamp: datetime, reason: str) -> None:
        key = (strategy, symbol)
        order_id = self.forced_flatten_orders.get(key)
        if not order_id:
            self.pending_forced_flattens.discard(key)
            self.forced_flatten_reasons.pop(key, None)
            return
        tracked = self.tracked_by_ref.get(order_id)
        if tracked is None:
            self.pending_forced_flattens.discard(key)
            self.forced_flatten_orders.pop(key, None)
            self.forced_flatten_reasons.pop(key, None)
            return
        for trade in self.ib.trades():
            order = getattr(trade, "order", None)
            if order is None:
                continue
            ib_id = getattr(order, "orderId", None)
            if ib_id not in self.tracked_by_ib_id or self.tracked_by_ib_id[ib_id].order_id != order_id:
                continue
            status = str(getattr(getattr(trade, "orderStatus", None), "status", "") or "")
            if status in FORCED_FLATTEN_DONE_STATUSES:
                # The original flatten order already reached a terminal state on IB's side
                # (e.g. rejected/cancelled) without the position fully unwinding, so our
                # bookkeeping never cleared. Resubmitting via placeOrder with this orderId
                # would be treated as a "modify" of a done order by ib_insync and raises
                # AssertionError. Drop the stale tracking and submit a fresh order instead.
                self.pending_forced_flattens.discard(key)
                self.forced_flatten_orders.pop(key, None)
                self.forced_flatten_reasons.pop(key, None)
                self.logger.event(
                    "forced_flatten_retry_stale_order",
                    {"strategy": strategy, "symbol": symbol, "order_id": order_id, "status": status, "reason": reason, "time": timestamp.isoformat()},
                )
                self._submit_window_flatten(key, quantity, tracked.action, timestamp, reason)
                return
            setattr(order, "totalQuantity", quantity)
            self.order_quantities[order_id] = quantity
            self.logger.event("forced_flatten_retry", {"strategy": strategy, "symbol": symbol, "order_id": order_id, "quantity": quantity, "reason": reason, "time": timestamp.isoformat()})
            if not self.dry_run:
                self.ib.placeOrder(self._contract_for(symbol), order)
            return

    def update_trailing_stops(self, symbol: str, price: float, timestamp: datetime) -> None:
        if not self.trailing_stop_enabled or price <= 0:
            return
        for key, quantity in list(self.long_positions.items()):
            strategy, tracked_symbol = key
            if tracked_symbol != symbol or quantity <= 0:
                continue
            entry_price = self.long_avg_prices.get(key, 0.0)
            stop_order_id = self.stop_orders_by_position.get(key)
            if entry_price <= 0 or not stop_order_id:
                continue
            high = max(self.high_watermarks.get(key, entry_price), price)
            self.high_watermarks[key] = high
            if high < entry_price * (1.0 + self.trailing_activation_bps / 10_000.0):
                continue
            new_stop = self._round_price_down(high * (1.0 - self.trailing_distance_bps / 10_000.0))
            self._raise_stop(strategy, symbol, stop_order_id, new_stop, timestamp)

    def resubmit_unacknowledged_exits(self, now: datetime) -> None:
        if self.dry_run:
            return
        for order_id in list(self._exit_pending_ack):
            submitted_at, checks = self._exit_pending_ack[order_id]
            elapsed = (now - submitted_at).total_seconds()
            if elapsed < self.exit_ack_timeout_seconds * (checks + 1):
                continue
            tracked = self.tracked_by_ref.get(order_id)
            if tracked is None:
                del self._exit_pending_ack[order_id]
                continue
            ib_id = next((iid for iid, t in self.tracked_by_ib_id.items() if t.order_id == order_id), None)
            if ib_id is None:
                del self._exit_pending_ack[order_id]
                continue
            trade = next((t for t in self.ib.trades() if getattr(getattr(t, "order", None), "orderId", None) == ib_id), None)
            if trade is None:
                del self._exit_pending_ack[order_id]
                continue
            status = str(getattr(getattr(trade, "orderStatus", None), "status", "") or "")
            if self._handle_exit_order_status(trade, now=now):
                continue
            if status not in PENDING_EXIT_ACK_STATUSES:
                del self._exit_pending_ack[order_id]
                continue
            if elapsed >= self.exit_ack_max_wait_seconds:
                del self._exit_pending_ack[order_id]
                self._flatten_for_unprotected_exit(
                    tracked,
                    order_id,
                    ib_id,
                    now,
                    "exit_order_ack_timeout_flatten",
                    "exit_order_ack_timeout",
                    elapsed,
                    status,
                )
                continue
            self.logger.event(
                "exit_order_ack_pending",
                {
                    "strategy": tracked.strategy,
                    "symbol": tracked.symbol,
                    "order_id": order_id,
                    "ib_order_id": ib_id,
                    "role": tracked.role,
                    "check": checks + 1,
                    "elapsed_seconds": elapsed,
                    "status": status,
                    "time": now.isoformat(),
                },
            )
            # Do not resubmit via placeOrder here: reusing the same orderId for an
            # order IB has not yet acknowledged (still PendingSubmit/ApiPending) looks
            # to IB like a duplicate submission. IB responds with an error tied to that
            # order, and ib_insync's wrapper auto-cancels the trade on any non-warning
            # order error (see ib_insync Wrapper.error), which used to be picked up by
            # _handle_exit_order_status as an unexpected protective-exit cancellation
            # and triggered an immediate market flatten seconds after entry -- the
            # resubmit was causing the very flattens it was meant to avoid. Just keep
            # waiting; if the order never acknowledges within exit_ack_max_wait_seconds,
            # the branch above flattens safely instead.
            self._exit_pending_ack[order_id] = (submitted_at, checks + 1)

    def _handle_exit_order_status(self, trade: Any, *, now: datetime | None = None) -> bool:
        order = getattr(trade, "order", None)
        if order is None:
            return False
        ib_id = getattr(order, "orderId", None)
        order_id = getattr(order, "orderRef", None) or self._order_ref_for_ib_id(ib_id)
        if not order_id:
            return False
        tracked = self.tracked_by_ref.get(order_id)
        if tracked is None or tracked.role not in PROTECTIVE_EXIT_ROLES:
            return False
        status = str(getattr(getattr(trade, "orderStatus", None), "status", "") or "")
        if status in PENDING_EXIT_ACK_STATUSES:
            return False
        timestamp = now or datetime.now(timezone.utc)
        amendment_started = self._exit_amendments.get(order_id)
        if amendment_started is not None:
            amendment_age = (timestamp - amendment_started).total_seconds()
            if amendment_age < self.exit_ack_timeout_seconds:
                if status in EXIT_CANCEL_OR_INACTIVE_STATUSES:
                    self.logger.event(
                        "exit_order_amendment_status_pending",
                        {
                            "strategy": tracked.strategy,
                            "symbol": tracked.symbol,
                            "order_id": order_id,
                            "ib_order_id": ib_id,
                            "role": tracked.role,
                            "status": status,
                            "elapsed_seconds": amendment_age,
                            "time": timestamp.isoformat(),
                        },
                    )
                return True
            self._exit_amendments.pop(order_id, None)
        self._exit_pending_ack.pop(order_id, None)
        if status in EXIT_CANCEL_OR_INACTIVE_STATUSES:
            if order_id in self._expected_exit_cancels:
                # PendingCancel is only an acknowledgement that IB started the
                # cancellation.  Keep the marker until the terminal callback;
                # otherwise the following Cancelled event looks unsolicited and
                # can launch a second market flatten against stale long inventory.
                if status != "PendingCancel":
                    self._expected_exit_cancels.discard(order_id)
                return True
            if tracked.role not in STOP_PROTECTION_ROLES:
                self._release_exit_reservation(order_id)
                self.logger.event(
                    "exit_order_cancelled_no_flatten",
                    {
                        "strategy": tracked.strategy,
                        "symbol": tracked.symbol,
                        "order_id": order_id,
                        "ib_order_id": ib_id,
                        "role": tracked.role,
                        "status": status,
                        "time": timestamp.isoformat(),
                    },
                )
                return True
            self._flatten_for_unprotected_exit(
                tracked,
                order_id,
                ib_id,
                timestamp,
                "exit_order_cancelled_flatten",
                "exit_order_cancelled",
                None,
                status,
            )
            return True
        return True

    def _flatten_for_unprotected_exit(
        self,
        tracked: TrackedOrder,
        order_id: str,
        ib_id: Any,
        timestamp: datetime,
        event_type: str,
        reason: str,
        elapsed_seconds: float | None,
        status: str,
    ) -> None:
        key = (tracked.strategy, tracked.symbol)
        qty = self.long_positions.get(key, 0)
        if qty <= 0 or key in self.pending_forced_flattens:
            return
        payload: dict[str, Any] = {
            "strategy": tracked.strategy,
            "symbol": tracked.symbol,
            "order_id": order_id,
            "ib_order_id": ib_id,
            "role": tracked.role,
            "quantity": qty,
            "status": status,
            "time": timestamp.isoformat(),
        }
        if elapsed_seconds is not None:
            payload["elapsed_seconds"] = elapsed_seconds
        self.logger.event(event_type, payload)
        self._submit_forced_flatten(tracked.strategy, tracked.symbol, qty, timestamp, reason)

    def enforce_stop_breaches(self, symbol: str, price: float, timestamp: datetime) -> None:
        if price <= 0:
            return
        for key, quantity in list(self.long_positions.items()):
            strategy, tracked_symbol = key
            if tracked_symbol != symbol or quantity <= 0:
                continue
            if strategy == "account":
                self._enforce_unmanaged_account_position(key, quantity, price, timestamp)
                continue
            if not self.software_stop_breach_enabled:
                continue
            if key in self.stop_breach_flattened:
                continue
            stop_order_id = self.stop_orders_by_position.get(key)
            stop_price = self._current_stop_price(key)
            if not stop_order_id or stop_price <= 0 or price > stop_price:
                continue
            self.stop_breach_flattened.add(key)
            self.logger.event(
                "stop_breach_flatten_submitted",
                {
                    "strategy": strategy,
                    "symbol": symbol,
                    "quantity": quantity,
                    "price": price,
                    "stop_price": stop_price,
                    "stop_order_id": stop_order_id,
                    "time": timestamp.isoformat(),
                },
            )
            self._submit_forced_flatten(strategy, symbol, quantity, timestamp, "stop_breach")

    def _enforce_unmanaged_account_position(self, key: tuple[str, str], quantity: int, price: float, timestamp: datetime) -> None:
        if key in self.unmanaged_position_flattened:
            return
        strategy, symbol = key
        avg_price = self.long_avg_prices.get(key, 0.0)
        if avg_price <= 0 or price >= avg_price:
            return
        self.unmanaged_position_flattened.add(key)
        self.logger.event(
            "unmanaged_account_position_flatten_submitted",
            {
                "strategy": strategy,
                "symbol": symbol,
                "quantity": quantity,
                "price": price,
                "avg_price": avg_price,
                "time": timestamp.isoformat(),
            },
        )
        self._submit_forced_flatten(strategy, symbol, quantity, timestamp, "unmanaged_account_position_loss")

    def _submit_forced_flatten(self, strategy: str, symbol: str, quantity: int, timestamp: datetime, reason: str) -> None:
        key = (strategy, symbol)
        if key in self.pending_forced_flattens:
            self._retry_forced_flatten(strategy, symbol, quantity, timestamp, reason)
            return
        self._start_symbol_cooldown(symbol, timestamp, reason)
        receiver = self.position_receivers.get(key) or self._receiver_for_position(strategy, symbol)
        if receiver is None:
            self.logger.event("forced_flatten_skipped_no_receiver", {"strategy": strategy, "symbol": symbol, "quantity": quantity, "reason": reason})
            return
        order_id = f"flatten-{strategy}-{symbol}-{timestamp.strftime('%Y%m%d%H%M%S')}"
        self.pending_forced_flattens.add(key)
        self.forced_flatten_orders[key] = order_id
        self.forced_flatten_reasons[key] = reason
        self.forced_flatten_watch[key] = self._forced_flatten_watch(key, reason, receiver, timestamp)
        self.logger.event("forced_flatten_submitted", {"strategy": strategy, "symbol": symbol, "order_id": order_id, "quantity": quantity, "reason": reason, "time": timestamp.isoformat()})
        self._record_forced_flatten(reason)
        self._place(receiver, symbol, order_id, "flatten", self._market_order("SELL", quantity))

    def _forced_flatten_watch(self, key: tuple[str, str], reason: str, receiver: FillReceiver, timestamp: datetime) -> ForcedFlattenWatch:
        strategy, symbol = key
        entry_order_ids = {
            order_id
            for order_id, tracked in self.tracked_by_ref.items()
            if tracked.strategy == strategy and tracked.symbol == symbol and tracked.action == "BUY" and tracked.role == "entry"
        }
        return ForcedFlattenWatch(reason, receiver, timestamp + timedelta(seconds=FORCED_FLATTEN_WATCH_SECONDS), entry_order_ids)

    def _start_symbol_cooldown(self, symbol: str, timestamp: datetime, reason: str) -> None:
        if reason not in {"stop_breach", "unmanaged_account_position_loss", "stop_loss"}:
            return
        seconds = self.stop_loss_cooldown_seconds if reason == "stop_loss" else self.forced_flatten_cooldown_seconds
        if seconds <= 0:
            return
        until = timestamp + timedelta(seconds=seconds)
        current = self.symbol_cooldowns.get(symbol)
        if current is not None and current >= until:
            return
        self.symbol_cooldowns[symbol] = until
        self.logger.event("symbol_cooldown_started", {"symbol": symbol, "reason": reason, "until": until.isoformat(), "seconds": seconds})

    def _current_stop_price(self, key: tuple[str, str]) -> float:
        return self.current_stop_prices.get(key, 0.0)

    def _raise_stop(self, strategy: str, symbol: str, order_id: str, new_stop: float, timestamp: datetime) -> None:
        tracked = self.tracked_by_ref.get(order_id)
        if tracked is None or tracked.role != "stop":
            return
        key = (strategy, symbol)
        new_stop = self._round_price_down(new_stop)
        current_stop = self.current_stop_prices.get(key, 0.0)
        min_step = max(self.long_avg_prices.get(key, new_stop), 0.01) * self.trailing_min_step_bps / 10_000.0
        if new_stop <= current_stop + min_step:
            return
        for trade in self.ib.trades():
            order = getattr(trade, "order", None)
            if order is None:
                continue
            ib_id = getattr(order, "orderId", None)
            if ib_id not in self.tracked_by_ib_id or self.tracked_by_ib_id[ib_id].order_id != order_id:
                continue
            setattr(order, "auxPrice", new_stop)
            self.current_stop_prices[key] = new_stop
            self.logger.event("trailing_stop_updated", {"strategy": strategy, "symbol": symbol, "order_id": order_id, "old_stop": current_stop, "new_stop": new_stop, "time": timestamp.isoformat()})
            if not self.dry_run:
                self._begin_exit_amendment(order_id, timestamp)
                self.ib.placeOrder(self._contract_for(symbol), order)
            return

    def _round_price_down(self, price: float) -> float:
        tick = Decimal(str(self.price_tick_size))
        value = Decimal(str(price))
        return float((value / tick).to_integral_value(rounding=ROUND_FLOOR) * tick)

    def _begin_exit_amendment(self, order_id: str, timestamp: datetime) -> None:
        self._exit_amendments[order_id] = timestamp
        self._exit_pending_ack[order_id] = (timestamp, 0)

    def _marketize_remaining_target(self, strategy: str, symbol: str, order_id: str, remaining_qty: int) -> None:
        for trade in self.ib.trades():
            order = getattr(trade, "order", None)
            if order is None:
                continue
            ib_id = getattr(order, "orderId", None)
            if ib_id not in self.tracked_by_ib_id or self.tracked_by_ib_id[ib_id].order_id != order_id:
                continue
            old_type = str(getattr(order, "orderType", "") or "")
            if old_type == "MKT":
                return
            setattr(order, "orderType", "MKT")
            if hasattr(order, "lmtPrice"):
                setattr(order, "lmtPrice", 0.0)
            self.logger.event(
                "partial_target_marketized",
                {
                    "strategy": strategy,
                    "symbol": symbol,
                    "order_id": order_id,
                    "remaining_qty": remaining_qty,
                    "old_order_type": old_type,
                },
            )
            if not self.dry_run:
                self.ib.placeOrder(self._contract_for(symbol), order)
            return

    def _sync_stop_quantity(self, strategy: str, symbol: str, quantity: int) -> None:
        key = (strategy, symbol)
        order_id = self.stop_orders_by_position.get(key)
        if not order_id:
            return
        self.pending_stop_quantities[key] = quantity
        for trade in self.ib.trades():
            order = getattr(trade, "order", None)
            if order is None:
                continue
            ib_id = getattr(order, "orderId", None)
            if ib_id not in self.tracked_by_ib_id or self.tracked_by_ib_id[ib_id].order_id != order_id:
                continue
            if not self._can_amend_order(trade):
                self.logger.event("stop_quantity_update_pending", {"strategy": strategy, "symbol": symbol, "order_id": order_id, "new_qty": quantity, "reason": "stop_not_acknowledged_or_inactive"})
                return
            current_qty = int(float(getattr(order, "totalQuantity", 0) or 0))
            if current_qty == quantity:
                self.order_quantities[order_id] = quantity
                self.pending_stop_quantities.pop(key, None)
                return
            setattr(order, "totalQuantity", quantity)
            self.order_quantities[order_id] = quantity
            self.logger.event("stop_quantity_updated", {"strategy": strategy, "symbol": symbol, "order_id": order_id, "old_qty": current_qty, "new_qty": quantity})
            if not self.dry_run and getattr(order, "permId", 0):
                self._begin_exit_amendment(order_id, datetime.now(timezone.utc))
                self.ib.placeOrder(self._contract_for(symbol), order)
            self.pending_stop_quantities.pop(key, None)
            return
        self.logger.event("stop_quantity_update_pending", {"strategy": strategy, "symbol": symbol, "order_id": order_id, "new_qty": quantity, "reason": "stop_trade_not_found"})

    def _sync_target_quantity(self, strategy: str, symbol: str, order_id: str, quantity: int) -> None:
        tracked = self.tracked_by_ref.get(order_id)
        if tracked is None or tracked.role not in {"tp1", "tp2", "target"}:
            return
        if self.order_filled_quantities.get(order_id, 0) > 0:
            return
        key = (strategy, symbol)
        current_reserved = self.exit_reservations_by_ref.get(order_id, 0)
        other_reserved = max(0, self.exit_reservations.get(key, 0) - current_reserved)
        accepted = min(quantity, max(0, self.long_positions.get(key, 0) - other_reserved))
        if accepted <= 0:
            return
        self.pending_target_quantities[order_id] = accepted
        for trade in self.ib.trades():
            order = getattr(trade, "order", None)
            if order is None:
                continue
            ib_id = getattr(order, "orderId", None)
            candidate = self.tracked_by_ib_id.get(ib_id)
            if candidate is None or candidate.order_id != order_id:
                continue
            if not self._can_amend_order(trade):
                self.logger.event(
                    "target_quantity_update_pending",
                    {"strategy": strategy, "symbol": symbol, "order_id": order_id, "role": tracked.role, "new_qty": accepted, "reason": "target_not_acknowledged_or_inactive"},
                )
                return
            current_qty = int(float(getattr(order, "totalQuantity", 0) or 0))
            if current_qty != accepted:
                setattr(order, "totalQuantity", accepted)
                self.logger.event(
                    "target_quantity_updated",
                    {"strategy": strategy, "symbol": symbol, "order_id": order_id, "role": tracked.role, "old_qty": current_qty, "new_qty": accepted},
                )
                if not self.dry_run:
                    self._begin_exit_amendment(order_id, datetime.now(timezone.utc))
                    self.ib.placeOrder(self._contract_for(symbol), order)
            self.order_quantities[order_id] = accepted
            self.exit_reservations_by_ref[order_id] = accepted
            self.exit_reservations[key] = other_reserved + accepted
            self.pending_target_quantities.pop(order_id, None)
            return
        self.logger.event(
            "target_quantity_update_pending",
            {"strategy": strategy, "symbol": symbol, "order_id": order_id, "role": tracked.role, "new_qty": accepted, "reason": "target_trade_not_found"},
        )

    def _apply_pending_stop_quantity(self, order_id: str) -> None:
        tracked = self.tracked_by_ref.get(order_id)
        if tracked is None or tracked.role != "stop":
            return
        key = (tracked.strategy, tracked.symbol)
        quantity = self.pending_stop_quantities.get(key)
        if quantity is None:
            return
        self._sync_stop_quantity(tracked.strategy, tracked.symbol, quantity)

    def _apply_pending_target_quantity(self, order_id: str) -> None:
        tracked = self.tracked_by_ref.get(order_id)
        quantity = self.pending_target_quantities.get(order_id)
        if tracked is None or quantity is None:
            return
        self._sync_target_quantity(tracked.strategy, tracked.symbol, order_id, quantity)

    def _can_amend_order(self, trade: Any) -> bool:
        order = getattr(trade, "order", None)
        status = getattr(trade, "orderStatus", None)
        if order is None or not getattr(order, "permId", 0):
            return False
        order_status = str(getattr(status, "status", "") or "")
        if order_status in {"Filled", "Cancelled", "ApiCancelled", "Inactive"}:
            return False
        return True

    def _contract_for(self, symbol: str) -> Any:
        contract = self.contracts.get(symbol) or self.account_contracts.get(symbol)
        if contract is None:
            raise RuntimeError(f"No contract available for {symbol}")
        return contract

    def _account_positions(self) -> list[Any]:
        try:
            positions = self.ib.reqPositions()
            if positions:
                return list(positions)
        except Exception as exc:
            self.logger.event("account_positions_request_failed", {"method": "reqPositions", "error": str(exc)})
        try:
            return list(self.ib.positions())
        except Exception as exc:
            self.logger.event("account_positions_request_failed", {"method": "positions", "error": str(exc)})
            return []

    def _promote_stale_targets(self, strategy: str, symbol: str, filled_order_id: str, fill_price: float) -> None:
        key = (strategy, symbol)
        entry_price = self.long_avg_prices.get(key, 0.0)
        initial_stop = self.initial_stop_prices.get(key, 0.0)
        risk = entry_price - initial_stop
        if entry_price <= 0 or risk <= 0:
            return
        runner_price = round(entry_price + risk * self.runner_target_r_multiple, 4)
        if runner_price <= fill_price:
            runner_price = round(fill_price + risk, 4)
        remaining_qty = self.long_positions.get(key, 0)
        for trade in self.ib.trades():
            order = getattr(trade, "order", None)
            if order is None:
                continue
            ib_id = getattr(order, "orderId", None)
            tracked = self.tracked_by_ib_id.get(ib_id)
            if tracked is None:
                continue
            if tracked.strategy != strategy or tracked.symbol != symbol or tracked.order_id == filled_order_id:
                continue
            if tracked.action != "SELL" or tracked.role not in {"tp1", "target"}:
                continue
            requested = self.order_quantities.get(tracked.order_id, 0)
            locally_filled = self.order_filled_quantities.get(tracked.order_id, 0)
            if requested > 0 and locally_filled >= requested:
                continue
            status = str(getattr(getattr(trade, "orderStatus", None), "status", "") or "")
            remaining = int(float(getattr(getattr(trade, "orderStatus", None), "remaining", 0) or 0))
            if status in FORCED_FLATTEN_DONE_STATUSES | {"Inactive"} or remaining <= 0:
                continue
            current_price = float(getattr(order, "lmtPrice", 0.0) or 0.0)
            if current_price <= 0 or current_price > fill_price:
                continue
            old_qty = int(float(getattr(order, "totalQuantity", 0) or 0))
            setattr(order, "lmtPrice", runner_price)
            if remaining_qty > 0 and old_qty != remaining_qty:
                setattr(order, "totalQuantity", remaining_qty)
            self.logger.event(
                "runner_target_promoted",
                {
                    "strategy": strategy,
                    "symbol": symbol,
                    "order_id": tracked.order_id,
                    "filled_order_id": filled_order_id,
                    "old_price": current_price,
                    "new_price": runner_price,
                    "old_qty": old_qty,
                    "new_qty": getattr(order, "totalQuantity", old_qty),
                    "r_multiple": self.runner_target_r_multiple,
                },
            )
            if not self.dry_run:
                try:
                    self.ib.placeOrder(self._contract_for(symbol), order)
                except Exception as exc:
                    self.logger.event(
                        "runner_target_promotion_failed",
                        {
                            "strategy": strategy,
                            "symbol": symbol,
                            "order_id": tracked.order_id,
                            "filled_order_id": filled_order_id,
                            "error": type(exc).__name__,
                            "message": str(exc),
                        },
                    )

    # ------------------------------------------------------------------
    # Restart / reconnect support
    # ------------------------------------------------------------------

    def confirm_fills_on_reconnect(self) -> None:
        """Replay any fills we may have missed while the connection was down.

        ib_insync repopulates each Trade's .fills list from the server when the
        session reconnects.  We iterate those fills and apply any that have an
        execId we have not seen before, preventing double-counting.
        """
        if self.dry_run:
            return
        for trade in self.ib.trades():
            ref = getattr(getattr(trade, "order", None), "orderRef", None)
            if not ref or ref not in self.tracked_by_ref:
                continue
            for fill in getattr(trade, "fills", []) or []:
                exec_id = getattr(getattr(fill, "execution", None), "execId", None)
                if exec_id and exec_id not in self._seen_exec_ids:
                    self._on_fill(ref, fill)

    def reconcile_on_startup(
        self,
        timestamp: datetime,
        records: list,
        cooldowns: dict[str, datetime],
    ) -> None:
        """Restore broker state from a persisted state file on restart.

        For each record:
        - If IBKR still shows the position → restore qty, avg, stop, and lock registry.
        - If IBKR no longer has the position → it was closed while we were down; log and skip.

        After this call, sync_account_positions() should still be called so that
        any unmanaged account positions are also discovered.
        """
        for symbol, until in cooldowns.items():
            if until > timestamp:
                self.symbol_cooldowns[symbol] = until

        if not records:
            return

        ibkr_longs: dict[str, tuple[int, float]] = {}
        for pos in self._account_positions():
            contract = getattr(pos, "contract", None)
            sym = str(getattr(contract, "symbol", "") or "")
            qty = int(float(getattr(pos, "position", 0) or 0))
            avg = float(getattr(pos, "avgCost", 0.0) or 0.0)
            if sym and qty > 0:
                ibkr_longs[sym] = (qty, avg)

        working_by_ref: dict[str, Any] = {}
        for trade in self.ib.trades():
            ref = getattr(getattr(trade, "order", None), "orderRef", None)
            status = str(getattr(getattr(trade, "orderStatus", None), "status", "") or "")
            if ref and status not in {"Filled", "Cancelled", "Inactive"}:
                working_by_ref[ref] = trade

        for record in records:
            key = (record.strategy, record.symbol)
            if record.symbol not in ibkr_longs:
                self.logger.event("startup_position_gone", {
                    "strategy": record.strategy, "symbol": record.symbol,
                    "persisted_qty": record.quantity, "time": timestamp.isoformat(),
                })
                continue

            qty, avg = ibkr_longs[record.symbol]
            self.long_positions[key] = qty
            self.long_avg_prices[key] = avg
            self.position_receivers[key] = self.account_receiver
            if record.initial_stop is not None:
                self.initial_stop_prices[key] = record.initial_stop
            if record.current_stop is not None:
                self.current_stop_prices[key] = record.current_stop
            if record.high_watermark is not None:
                self.high_watermarks[key] = record.high_watermark
            if record.stop_order_id:
                self.stop_orders_by_position[key] = record.stop_order_id
                if record.stop_order_id in working_by_ref:
                    trade = working_by_ref[record.stop_order_id]
                    tracked = TrackedOrder(
                        record.strategy, record.symbol, record.stop_order_id,
                        "stop", self.account_receiver, "SELL",
                    )
                    self.tracked_by_ref[record.stop_order_id] = tracked
                    ib_id = getattr(getattr(trade, "order", None), "orderId", None)
                    if ib_id is not None:
                        self.tracked_by_ib_id[ib_id] = tracked
                    fill_event = getattr(trade, "fillEvent", None)
                    if fill_event is not None:
                        fill_event += lambda t, f, oid=record.stop_order_id: self._on_fill(oid, f)

            self.registry.lock_position(record.symbol, record.strategy, timestamp, "restored_from_state")
            self.logger.event("startup_position_restored", {
                "strategy": record.strategy, "symbol": record.symbol,
                "quantity": qty, "avg_price": avg,
                "current_stop": record.current_stop,
                "stop_order_id": record.stop_order_id,
                "time": timestamp.isoformat(),
            })

    def bind_position_receiver(self, strategy: str, symbol: str, receiver: FillReceiver) -> bool:
        """Let an adapter claim ownership of a position restored from state.

        Called after adapters are built so fill callbacks route to the right adapter
        rather than the fallback account_receiver installed by reconcile_on_startup.
        Returns True if the position existed and was re-bound, False otherwise.
        """
        key = (strategy, symbol)
        if key not in self.long_positions or self.long_positions[key] <= 0:
            return False
        self.position_receivers[key] = receiver
        for order_id in list(self.tracked_by_ref):
            tracked = self.tracked_by_ref[order_id]
            if tracked.strategy == strategy and tracked.symbol == symbol:
                new_tracked = TrackedOrder(
                    tracked.strategy, tracked.symbol, tracked.order_id,
                    tracked.role, receiver, tracked.action,
                )
                self.tracked_by_ref[order_id] = new_tracked
                for ib_id, existing in list(self.tracked_by_ib_id.items()):
                    if existing.order_id == order_id:
                        self.tracked_by_ib_id[ib_id] = new_tracked
        return True

    def _lock_or_raise(self, symbol: str, strategy: str, timestamp: datetime, reason: str) -> None:
        blocked_reason = self.entry_block_reason(symbol, strategy)
        if blocked_reason:
            raise RuntimeError(f"{symbol} entry blocked for {strategy}: {blocked_reason}")
        if not self.registry.lock_entry_order(symbol, strategy, timestamp, reason):
            owner = self.registry.owner(symbol)
            if owner != strategy:
                raise RuntimeError(f"{symbol} is locked by {owner}; {strategy} cannot submit")

    def _limit_order(self, side: str, qty: int, price: float):
        from ib_insync import LimitOrder

        return LimitOrder(side, qty, price, tif="DAY", outsideRth=False)

    def _market_order(self, side: str, qty: int):
        from ib_insync import MarketOrder

        return MarketOrder(side, qty, tif="DAY", outsideRth=False)

    def _stop_order(self, side: str, qty: int, stop_price: float):
        from ib_insync import StopOrder

        return StopOrder(side, qty, stop_price, tif="DAY", outsideRth=False)

    def _order_from_absorption(self, order: Any):
        if order.order_type == "MKT":
            return self._market_order(order.side, order.qty)
        if order.order_type == "STP":
            return self._stop_order(order.side, order.qty, order.stop_price)
        return self._limit_order(order.side, order.qty, order.price)

    # ------------------------------------------------------------------
    # Broker lifecycle audit logging
    # ------------------------------------------------------------------

    def _bind_ib_lifecycle_events(self) -> None:
        if self.dry_run:
            return
        bindings = {
            "openOrderEvent": self._on_ib_open_order_event,
            "orderStatusEvent": self._on_ib_order_status_event,
            "execDetailsEvent": self._on_ib_exec_details_event,
            "commissionReportEvent": self._on_ib_commission_report_event,
            "errorEvent": self._on_ib_error_event,
        }
        for event_name, handler in bindings.items():
            event = getattr(self.ib, event_name, None)
            if event is None:
                continue
            try:
                event += handler
            except Exception as exc:
                self.logger.event("broker_lifecycle_event_bind_failed", {"event": event_name, "error": str(exc)})

    def _on_ib_open_order_event(self, *args: Any) -> None:
        trade = self._trade_from_event_args(args)
        if trade is None:
            self._log_broker_event_args("broker_open_orders", "openOrderEvent", args)
            return
        self._log_broker_open_order(trade, source="openOrderEvent")
        order = getattr(trade, "order", None)
        if order and getattr(order, "permId", 0):
            ib_id = getattr(order, "orderId", None)
            ref = getattr(order, "orderRef", None) or self._order_ref_for_ib_id(ib_id)
            if ref not in self._exit_amendments:
                self._exit_pending_ack.pop(ref, None)
            if ref:
                self._apply_pending_stop_quantity(ref)
                self._apply_pending_target_quantity(ref)

    def _on_ib_order_status_event(self, *args: Any) -> None:
        trade = self._trade_from_event_args(args)
        if trade is None:
            self._log_broker_event_args("broker_order_status", "orderStatusEvent", args)
            return
        self._log_broker_order_status(trade, source="orderStatusEvent")
        self._handle_exit_order_status(trade)
        order = getattr(trade, "order", None)
        if order and getattr(order, "permId", 0):
            ref = getattr(order, "orderRef", None) or self._order_ref_for_ib_id(getattr(order, "orderId", None))
            if ref:
                self._apply_pending_stop_quantity(ref)
                self._apply_pending_target_quantity(ref)

    def _on_ib_exec_details_event(self, *args: Any) -> None:
        trade, fill = self._trade_and_fill_from_event_args(args)
        if fill is None:
            self._log_broker_event_args("broker_executions", "execDetailsEvent", args)
            return
        self._log_broker_execution(trade, fill, source="execDetailsEvent")
        order = getattr(trade, "order", None)
        execution = getattr(fill, "execution", None)
        ref = getattr(order, "orderRef", None) or self._order_ref_for_ib_id(getattr(execution, "orderId", None))
        if ref:
            self._on_fill(ref, fill)

    def _on_ib_commission_report_event(self, *args: Any) -> None:
        report = args[-1] if args else None
        self._log_broker_commission(report, source="commissionReportEvent")
        exec_id = str(getattr(report, "execId", "") or "")
        if not exec_id or exec_id in self._seen_commission_exec_ids:
            return
        route = self._execution_commission_routes.pop(exec_id, None)
        if route is None:
            return
        self._seen_commission_exec_ids.add(exec_id)
        receiver, timestamp = route
        callback = getattr(receiver, "on_broker_commission", None)
        if callback is not None:
            callback(timestamp, float(getattr(report, "commission", 0.0) or 0.0))

    def _on_ib_error_event(self, reqId: Any = None, errorCode: Any = None, errorString: Any = None, contract: Any = None) -> None:
        tracked = self.tracked_by_ib_id.get(reqId)
        payload: dict[str, Any] = {
            "ib_req_id": reqId,
            "error_code": errorCode,
            "error_string": str(errorString) if errorString is not None else "",
            "symbol": str(getattr(contract, "symbol", "") or ""),
        }
        if tracked is not None:
            payload.update({"strategy": tracked.strategy, "order_id": tracked.order_id, "role": tracked.role, "tracked_symbol": tracked.symbol})
        self.logger.event("broker_error", payload)
        try:
            code = int(errorCode)
        except (TypeError, ValueError):
            code = 0
        symbol = payload["symbol"] or (tracked.symbol if tracked is not None else "")
        if code == 2152 and symbol and self.fail_closed_on_depth_permission_error:
            if symbol not in self.depth_blocked_symbols:
                self.depth_blocked_symbols.add(symbol)
                self.logger.event(
                    "depth_strategy_symbol_blocked",
                    {"symbol": symbol, "reason": "depth_permissions_unavailable", "error_code": code},
                )
        self._handle_forced_flatten_modify_error(reqId, errorCode, tracked)

    def _handle_forced_flatten_modify_error(self, req_id: Any, error_code: Any, tracked: TrackedOrder | None) -> None:
        try:
            code = int(error_code)
        except (TypeError, ValueError):
            return
        if code != 104 or tracked is None or tracked.role != "flatten":
            return
        key = (tracked.strategy, tracked.symbol)
        if self.forced_flatten_orders.get(key) != tracked.order_id:
            return
        quantity = self.long_positions.get(key, 0)
        if quantity <= 0:
            return
        reason = self.forced_flatten_reasons.get(key, "forced_flatten_modify_filled")
        self.pending_forced_flattens.discard(key)
        self.forced_flatten_orders.pop(key, None)
        self.forced_flatten_reasons.pop(key, None)
        # Error 104 confirms IB considers the old flatten done. Retire its local
        # tracking before submitting the replacement so the duplicate-flatten
        # guard does not mistake the completed order for a live one.
        self._retire_tracked_order(tracked.order_id)
        timestamp = datetime.now(timezone.utc)
        self.logger.event(
            "forced_flatten_modify_filled_resubmit",
            {
                "strategy": tracked.strategy,
                "symbol": tracked.symbol,
                "order_id": tracked.order_id,
                "ib_order_id": req_id,
                "quantity": quantity,
                "reason": reason,
                "time": timestamp.isoformat(),
            },
        )
        self._submit_window_flatten(key, quantity, tracked.action, timestamp, reason)

    def _trade_from_event_args(self, args: tuple[Any, ...]) -> Any | None:
        for arg in args:
            if hasattr(arg, "order") and hasattr(arg, "orderStatus"):
                return arg
        return None

    def _trade_and_fill_from_event_args(self, args: tuple[Any, ...]) -> tuple[Any | None, Any | None]:
        trade = self._trade_from_event_args(args)
        fill = None
        for arg in args:
            if hasattr(arg, "execution"):
                fill = arg
                break
        return trade, fill

    def _log_broker_open_order(self, trade: Any, *, source: str) -> None:
        order = getattr(trade, "order", None)
        if order is None:
            return
        self.logger.csv("broker_open_orders", {**self._broker_order_row(trade), "source": source})

    def _log_broker_order_status(self, trade: Any, *, source: str) -> None:
        order = getattr(trade, "order", None)
        status = getattr(trade, "orderStatus", None)
        if order is None and status is None:
            return
        self.logger.csv("broker_order_status", {**self._broker_order_row(trade), "source": source})

    def _log_broker_execution(self, trade: Any | None, fill: Any, *, source: str) -> None:
        execution = getattr(fill, "execution", None)
        if execution is None:
            return
        contract = getattr(fill, "contract", None) or getattr(trade, "contract", None)
        order = getattr(trade, "order", None)
        report = getattr(fill, "commissionReport", None)
        self.logger.csv(
            "broker_executions",
            {
                "timestamp": getattr(execution, "time", None) or datetime.now(timezone.utc),
                "source": source,
                "symbol": getattr(contract, "symbol", ""),
                "order_id": getattr(order, "orderRef", "") or self._order_ref_for_ib_id(getattr(execution, "orderId", None)),
                "ib_order_id": getattr(execution, "orderId", ""),
                "perm_id": getattr(execution, "permId", getattr(order, "permId", "")),
                "exec_id": getattr(execution, "execId", ""),
                "side": getattr(execution, "side", ""),
                "shares": getattr(execution, "shares", ""),
                "price": getattr(execution, "price", ""),
                "avg_price": getattr(execution, "avgPrice", ""),
                "exchange": getattr(execution, "exchange", ""),
                "commission": getattr(report, "commission", ""),
            },
        )

    def _log_broker_commission(self, report: Any, *, source: str) -> None:
        if report is None:
            return
        self.logger.csv(
            "broker_commissions",
            {
                "timestamp": datetime.now(timezone.utc),
                "source": source,
                "exec_id": getattr(report, "execId", ""),
                "commission": getattr(report, "commission", ""),
                "currency": getattr(report, "currency", ""),
                "realized_pnl": getattr(report, "realizedPNL", ""),
                "yield": getattr(report, "yield_", getattr(report, "yield", "")),
                "yield_redemption_date": getattr(report, "yieldRedemptionDate", ""),
            },
        )

    def _broker_order_row(self, trade: Any) -> dict[str, Any]:
        order = getattr(trade, "order", None)
        status = getattr(trade, "orderStatus", None)
        contract = getattr(trade, "contract", None)
        ib_order_id = getattr(order, "orderId", "")
        return {
            "timestamp": datetime.now(timezone.utc),
            "symbol": getattr(contract, "symbol", ""),
            "order_id": getattr(order, "orderRef", "") or self._order_ref_for_ib_id(ib_order_id),
            "ib_order_id": ib_order_id,
            "perm_id": getattr(order, "permId", ""),
            "client_id": getattr(order, "clientId", ""),
            "action": getattr(order, "action", ""),
            "order_type": getattr(order, "orderType", ""),
            "quantity": getattr(order, "totalQuantity", ""),
            "limit_price": getattr(order, "lmtPrice", ""),
            "stop_price": getattr(order, "auxPrice", ""),
            "oca_group": getattr(order, "ocaGroup", ""),
            "oca_type": getattr(order, "ocaType", ""),
            "status": getattr(status, "status", ""),
            "filled": getattr(status, "filled", ""),
            "remaining": getattr(status, "remaining", ""),
            "avg_fill_price": getattr(status, "avgFillPrice", ""),
            "last_fill_price": getattr(status, "lastFillPrice", ""),
            "why_held": getattr(status, "whyHeld", ""),
        }

    def _order_ref_for_ib_id(self, ib_order_id: Any) -> str:
        if ib_order_id is None:
            return ""
        tracked = self.tracked_by_ib_id.get(ib_order_id)
        return tracked.order_id if tracked is not None else ""

    def _log_broker_event_args(self, csv_name: str, source: str, args: tuple[Any, ...]) -> None:
        self.logger.csv(
            csv_name,
            {
                "timestamp": datetime.now(timezone.utc),
                "source": source,
                "raw_args": [str(arg) for arg in args],
            },
        )
