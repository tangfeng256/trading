from datetime import datetime, timedelta, timezone

from multi_strategy.registry import PositionRegistry


NOW = datetime(2026, 5, 26, 14, 0, tzinfo=timezone.utc)


def test_symbol_locked_by_one_strategy_blocks_others_until_flat():
    registry = PositionRegistry(lock_on_entry_order=True)

    assert registry.lock_entry_order("NVDA", "opening_range", NOW)
    assert registry.is_available("NVDA", "opening_range")
    assert not registry.is_available("NVDA", "pullback")
    assert not registry.lock_entry_order("NVDA", "absorption", NOW)

    registry.lock_position("NVDA", "opening_range", NOW)
    registry.unlock_if_owner("NVDA", "pullback")
    assert registry.owner("NVDA") == "opening_range"

    registry.unlock_if_owner("NVDA", "opening_range")
    assert registry.is_available("NVDA", "pullback")


def test_lock_on_entry_order_false_skips_order_lock_but_position_lock_still_works():
    registry = PositionRegistry(lock_on_entry_order=False)

    # order-submit lock is bypassed — symbol stays free
    assert registry.lock_entry_order("NVDA", "opening_range", NOW)
    assert registry.is_available("NVDA", "pullback")

    # position lock is always enforced
    assert registry.lock_position("NVDA", "opening_range", NOW)
    assert not registry.is_available("NVDA", "pullback")


def test_entry_order_upgrades_to_open():
    registry = PositionRegistry(lock_on_entry_order=True)

    assert registry.lock_entry_order("NVDA", "opening_range", NOW)
    assert registry.lock_position("NVDA", "opening_range", NOW)

    lock = registry.snapshot()["NVDA"]
    assert lock["state"] == "OPEN"


def test_state_downgrade_is_blocked():
    registry = PositionRegistry(lock_on_entry_order=True)

    registry.lock_position("NVDA", "opening_range", NOW)
    result = registry.lock_entry_order("NVDA", "opening_range", NOW)

    assert result is False
    assert registry.snapshot()["NVDA"]["state"] == "OPEN"


def test_expire_stale_entry_orders_unlocks_timed_out_symbols():
    timeout = timedelta(minutes=2)
    registry = PositionRegistry(lock_on_entry_order=True, entry_order_timeout=timeout)

    registry.lock_entry_order("NVDA", "opening_range", NOW)
    registry.lock_entry_order("TSLA", "pullback", NOW)
    registry.lock_position("AMD", "absorption", NOW)

    later = NOW + timedelta(minutes=3)
    expired = registry.expire_stale_entry_orders(later)

    assert set(expired) == {"NVDA", "TSLA"}
    assert registry.is_available("NVDA", "pullback")
    assert registry.is_available("TSLA", "opening_range")
    assert not registry.is_available("AMD", "opening_range")  # OPEN lock survives


def test_expire_stale_entry_orders_respects_timeout():
    timeout = timedelta(minutes=2)
    registry = PositionRegistry(lock_on_entry_order=True, entry_order_timeout=timeout)

    registry.lock_entry_order("NVDA", "opening_range", NOW)

    just_before = NOW + timedelta(minutes=1, seconds=59)
    expired = registry.expire_stale_entry_orders(just_before)

    assert expired == []
    assert not registry.is_available("NVDA", "pullback")


def test_owner_returns_none_for_unlocked_symbol():
    registry = PositionRegistry()
    assert registry.owner("NVDA") is None


def test_is_available_without_strategy_arg():
    registry = PositionRegistry()
    assert registry.is_available("NVDA")
    registry.lock_position("NVDA", "opening_range", NOW)
    assert not registry.is_available("NVDA")
