from __future__ import annotations

# Single-threaded: runs entirely on the ib_insync asyncio event loop.
# Do not call from threads without adding a lock around _locks mutations.

from dataclasses import dataclass
from datetime import datetime, timedelta


_STATE_RANK = {"ENTRY_ORDER": 1, "OPEN": 2}


@dataclass
class SymbolLock:
    symbol: str
    strategy: str
    state: str
    timestamp: datetime
    reason: str = ""


class PositionRegistry:
    def __init__(
        self,
        lock_on_entry_order: bool = True,
        entry_order_timeout: timedelta = timedelta(minutes=2),
    ) -> None:
        # lock_on_entry_order: if True, reserve the symbol at order submission
        # so competing strategies are blocked before the fill arrives.
        # Position locks (lock_position) are always enforced regardless of this flag.
        self.lock_on_entry_order = lock_on_entry_order
        self.entry_order_timeout = entry_order_timeout
        self._locks: dict[str, SymbolLock] = {}

    def is_available(self, symbol: str, strategy: str | None = None) -> bool:
        lock = self._locks.get(symbol)
        return lock is None or (strategy is not None and lock.strategy == strategy)

    def owner(self, symbol: str) -> str | None:
        lock = self._locks.get(symbol)
        return lock.strategy if lock else None

    def lock_entry_order(self, symbol: str, strategy: str, timestamp: datetime, reason: str = "entry_order") -> bool:
        if not self.lock_on_entry_order:
            return True
        return self._lock(symbol, strategy, "ENTRY_ORDER", timestamp, reason)

    def lock_position(self, symbol: str, strategy: str, timestamp: datetime, reason: str = "position_open") -> bool:
        return self._lock(symbol, strategy, "OPEN", timestamp, reason)

    def unlock_if_owner(self, symbol: str, strategy: str) -> None:
        lock = self._locks.get(symbol)
        if lock and lock.strategy == strategy:
            del self._locks[symbol]

    def expire_stale_entry_orders(self, now: datetime) -> list[str]:
        """Remove ENTRY_ORDER locks whose order was never filled within the timeout.

        Returns the list of symbols that were unlocked so callers can log them.
        """
        expired = [
            symbol
            for symbol, lock in self._locks.items()
            if lock.state == "ENTRY_ORDER" and (now - lock.timestamp) > self.entry_order_timeout
        ]
        for symbol in expired:
            del self._locks[symbol]
        return expired

    def snapshot(self) -> dict[str, dict]:
        return {symbol: lock.__dict__.copy() for symbol, lock in self._locks.items()}

    def _lock(self, symbol: str, strategy: str, state: str, timestamp: datetime, reason: str) -> bool:
        lock = self._locks.get(symbol)
        if lock and lock.strategy != strategy:
            return False
        # Prevent state downgrade (e.g. OPEN -> ENTRY_ORDER from a coding mistake).
        if lock and _STATE_RANK.get(state, 0) < _STATE_RANK.get(lock.state, 0):
            return False
        self._locks[symbol] = SymbolLock(symbol, strategy, state, timestamp, reason)
        return True
