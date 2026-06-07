from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
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


class AccountPositionReceiver:
    strategy_name = "account"

    def on_broker_fill(self, order_id: str, timestamp: datetime, quantity: int, price: float, commission: float = 0.0) -> None:
        pass


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
        manage_account_positions: bool = True,
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
        self.manage_account_positions = manage_account_positions
        self.tracked_by_ib_id: dict[int, TrackedOrder] = {}
        self.tracked_by_ref: dict[str, TrackedOrder] = {}
        self.long_positions: dict[tuple[str, str], int] = {}
        self.short_positions: dict[tuple[str, str], int] = {}
        self.long_avg_prices: dict[tuple[str, str], float] = {}
        self.exit_reservations: dict[tuple[str, str], int] = {}
        self.exit_reservations_by_ref: dict[str, int] = {}
        self.stop_orders_by_position: dict[tuple[str, str], str] = {}
        self.initial_stop_prices: dict[tuple[str, str], float] = {}
        self.high_watermarks: dict[tuple[str, str], float] = {}
        self.position_receivers: dict[tuple[str, str], FillReceiver] = {}
        self.pending_forced_flattens: set[tuple[str, str]] = set()
        self.forced_flatten_orders: dict[tuple[str, str], str] = {}
        self.stop_breach_flattened: set[tuple[str, str]] = set()
        self.unmanaged_position_flattened: set[tuple[str, str]] = set()
        self.symbol_cooldowns: dict[str, datetime] = {}
        self.account_receiver = AccountPositionReceiver()
        self.trading_action_plans: dict[tuple[str, str], dict[str, Any]] = {}
        self.order_quantities: dict[str, int] = {}
        self.order_filled_quantities: dict[str, int] = {}
        self.current_stop_prices: dict[tuple[str, str], float] = {}
        self._seen_exec_ids: set[str] = set()

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

    def sync_account_positions(self, timestamp: datetime) -> None:
        positions = [] if self.dry_run else self._account_positions()
        if not self.manage_account_positions:
            snapshot = []
            for position in positions:
                contract = getattr(position, "contract", None)
                symbol = str(getattr(contract, "symbol", "") or "")
                if not symbol:
                    continue
                if contract is not None:
                    self.account_contracts[symbol] = contract
                snapshot.append(
                    {
                        "symbol": symbol,
                        "quantity": int(float(getattr(position, "position", 0) or 0)),
                        "avg_price": float(getattr(position, "avgCost", 0.0) or 0.0),
                        "sec_type": str(getattr(contract, "secType", "") or ""),
                        "con_id": getattr(contract, "conId", ""),
                    }
                )
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
        payload: dict = {"strategy": strategy, "symbol": symbol, "order_id": order_id, "quantity": quantity, "reason": reason, "time": timestamp.isoformat()}
        if side == "BUY":
            payload["side"] = "BUY_TO_COVER"
        self.logger.event("forced_flatten_submitted", payload)
        self._place(receiver, symbol, order_id, "flatten", self._market_order(side, quantity))

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

    def cancel(self, order_id: str) -> None:
        tracked = self.tracked_by_ref.get(order_id)
        if not tracked:
            return
        self._release_exit_reservation(order_id)
        if self.dry_run:
            return
        for trade in self.ib.trades():
            if getattr(getattr(trade, "order", None), "orderId", None) in self.tracked_by_ib_id:
                if self.tracked_by_ib_id[trade.order.orderId].order_id == order_id:
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
            self._seen_exec_ids.add(exec_id)
        timestamp = getattr(execution, "time", None) or datetime.now(timezone.utc)
        if timestamp.tzinfo is None:
            timestamp = timestamp.replace(tzinfo=timezone.utc)
        quantity = int(float(getattr(execution, "shares", 0) or 0))
        price = float(getattr(execution, "price", 0.0) or 0.0)
        report = getattr(fill, "commissionReport", None)
        commission = float(getattr(report, "commission", 0.0) or 0.0)
        self._apply_position_fill(tracked, order_id, quantity, price)
        self.logger.csv("fills", {"timestamp": timestamp, "strategy": tracked.strategy, "symbol": tracked.symbol, "order_id": order_id, "role": tracked.role, "quantity": quantity, "price": price})
        filled_quantity = self.order_filled_quantities.get(order_id, 0) + quantity
        self.order_filled_quantities[order_id] = filled_quantity
        requested_quantity = self.order_quantities.get(order_id, filled_quantity)
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

    def _reserve_exit_quantity(self, strategy: str, symbol: str, order_id: str, role: str, ib_order: Any) -> bool:
        requested = int(float(getattr(ib_order, "totalQuantity", 0) or 0))
        key = (strategy, symbol)
        long_qty = self.long_positions.get(key, 0)
        if role == "flatten":
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
            self._mark_exit_oca(strategy, symbol, ib_order)
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
        self._mark_exit_oca(strategy, symbol, ib_order)
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

    def _apply_position_fill(self, tracked: TrackedOrder, order_id: str, quantity: int, fill_price: float) -> None:
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
            return
        if tracked.action != "SELL":
            return
        reserved = self.exit_reservations_by_ref.get(order_id, 0)
        release = min(reserved, quantity)
        if release:
            self.exit_reservations_by_ref[order_id] = reserved - release
            self.exit_reservations[key] = max(0, self.exit_reservations.get(key, 0) - release)
        current = self.long_positions.get(key, 0)
        if quantity > current:
            self.logger.event(
                "sell_fill_exceeded_long_inventory",
                {"strategy": tracked.strategy, "symbol": tracked.symbol, "order_id": order_id, "fill_qty": quantity, "long_qty_before_fill": current},
            )
        self.long_positions[key] = max(0, current - quantity)
        if self.long_positions[key] == 0:
            self._clear_position_state(key)
            self.registry.unlock_if_owner(tracked.symbol, tracked.strategy)
        else:
            self._sync_stop_quantity(tracked.strategy, tracked.symbol, self.long_positions[key])
            if tracked.role == "tp1":
                remaining_target_qty = self.exit_reservations_by_ref.get(order_id, 0)
                if remaining_target_qty > 0:
                    self._marketize_remaining_target(tracked.strategy, tracked.symbol, order_id, remaining_target_qty)
            if self.runner_target_enabled and tracked.role in {"tp2", "target"}:
                self._promote_stale_targets(tracked.strategy, tracked.symbol, order_id, fill_price)

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

    def _clear_position_state(self, key: tuple[str, str]) -> None:
        strategy, symbol = key
        self.long_positions.pop(key, None)
        self.short_positions.pop(key, None)
        self.long_avg_prices.pop(key, None)
        self.exit_reservations.pop(key, None)
        for order_id, tracked in list(self.tracked_by_ref.items()):
            if tracked.strategy == strategy and tracked.symbol == symbol:
                self.exit_reservations_by_ref.pop(order_id, None)
        self.high_watermarks.pop(key, None)
        self.stop_orders_by_position.pop(key, None)
        self.initial_stop_prices.pop(key, None)
        self.current_stop_prices.pop(key, None)
        self.position_receivers.pop(key, None)
        self.pending_forced_flattens.discard(key)
        self.forced_flatten_orders.pop(key, None)
        self.stop_breach_flattened.discard(key)
        self.unmanaged_position_flattened.discard(key)

    def _retry_forced_flatten(self, strategy: str, symbol: str, quantity: int, timestamp: datetime, reason: str) -> None:
        order_id = self.forced_flatten_orders.get((strategy, symbol))
        if not order_id:
            self.pending_forced_flattens.discard((strategy, symbol))
            return
        tracked = self.tracked_by_ref.get(order_id)
        if tracked is None:
            self.pending_forced_flattens.discard((strategy, symbol))
            self.forced_flatten_orders.pop((strategy, symbol), None)
            return
        for trade in self.ib.trades():
            order = getattr(trade, "order", None)
            if order is None:
                continue
            ib_id = getattr(order, "orderId", None)
            if ib_id not in self.tracked_by_ib_id or self.tracked_by_ib_id[ib_id].order_id != order_id:
                continue
            setattr(order, "totalQuantity", quantity)
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
            new_stop = round(high * (1.0 - self.trailing_distance_bps / 10_000.0), 4)
            self._raise_stop(strategy, symbol, stop_order_id, new_stop, timestamp)

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
        self.logger.event("forced_flatten_submitted", {"strategy": strategy, "symbol": symbol, "order_id": order_id, "quantity": quantity, "reason": reason, "time": timestamp.isoformat()})
        self._place(receiver, symbol, order_id, "flatten", self._market_order("SELL", quantity))

    def _start_symbol_cooldown(self, symbol: str, timestamp: datetime, reason: str) -> None:
        if reason not in {"stop_breach", "unmanaged_account_position_loss"}:
            return
        if self.forced_flatten_cooldown_seconds <= 0:
            return
        until = timestamp + timedelta(seconds=self.forced_flatten_cooldown_seconds)
        current = self.symbol_cooldowns.get(symbol)
        if current is not None and current >= until:
            return
        self.symbol_cooldowns[symbol] = until
        self.logger.event("symbol_cooldown_started", {"symbol": symbol, "reason": reason, "until": until.isoformat(), "seconds": self.forced_flatten_cooldown_seconds})

    def _current_stop_price(self, key: tuple[str, str]) -> float:
        return self.current_stop_prices.get(key, 0.0)

    def _raise_stop(self, strategy: str, symbol: str, order_id: str, new_stop: float, timestamp: datetime) -> None:
        tracked = self.tracked_by_ref.get(order_id)
        if tracked is None or tracked.role != "stop":
            return
        key = (strategy, symbol)
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
                self.ib.placeOrder(self._contract_for(symbol), order)
            return

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
        order_id = self.stop_orders_by_position.get((strategy, symbol))
        if not order_id:
            return
        for trade in self.ib.trades():
            order = getattr(trade, "order", None)
            if order is None:
                continue
            ib_id = getattr(order, "orderId", None)
            if ib_id not in self.tracked_by_ib_id or self.tracked_by_ib_id[ib_id].order_id != order_id:
                continue
            current_qty = int(float(getattr(order, "totalQuantity", 0) or 0))
            if current_qty == quantity:
                return
            setattr(order, "totalQuantity", quantity)
            self.logger.event("stop_quantity_updated", {"strategy": strategy, "symbol": symbol, "order_id": order_id, "old_qty": current_qty, "new_qty": quantity})
            if not self.dry_run:
                self.ib.placeOrder(self._contract_for(symbol), order)

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
                self.ib.placeOrder(self._contract_for(symbol), order)

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
                    self._seen_exec_ids.add(exec_id)
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
