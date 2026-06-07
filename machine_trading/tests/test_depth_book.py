from datetime import datetime, timedelta, timezone

import pytest

from absorption.depth_book import DepthBook


def test_l2_book_insert_update_delete_shifts_levels_correctly():
    book = DepthBook("NVDA", max_depth=5)
    now = datetime.now(timezone.utc)
    book.apply_update(0, 0, "bid", 100.00, 100, timestamp=now)
    book.apply_update(1, 0, "bid", 99.99, 90, timestamp=now)
    book.apply_update(1, 0, "bid", 99.995, 80, timestamp=now)
    assert [level.price for level in book.bids] == [100.00, 99.995, 99.99]
    book.apply_update(1, 1, "bid", 99.995, 120, timestamp=now)
    assert book.bids[1].size == 120
    book.apply_update(0, 2, "bid", 0, 0, timestamp=now)
    assert [level.price for level in book.bids] == [99.995, 99.99]


def test_spread_and_microprice_are_calculated_correctly():
    book = DepthBook("NVDA")
    now = datetime.now(timezone.utc)
    book.apply_update(0, 0, "bid", 100.00, 300, timestamp=now)
    book.apply_update(0, 0, "ask", 100.02, 100, timestamp=now)
    assert book.spread() == pytest.approx(0.02)
    assert book.mid() == pytest.approx(100.01)
    assert book.microprice() == pytest.approx((100.02 * 300 + 100.00 * 100) / 400)


def test_replenishment_events_are_pruned_after_retention_window():
    book = DepthBook("NVDA", replenishment_retention_sec=10)
    base = datetime(2026, 6, 2, 14, 0, 0, tzinfo=timezone.utc)

    # replenishment event at t=0
    book.apply_update(0, 0, "bid", 100.00, 100, timestamp=base)
    book.apply_update(0, 1, "bid", 100.00, 150, timestamp=base)  # +50 replenishment
    assert book.recent_replenishment(base) == 50.0

    # advance 11 seconds — first event (at base) is now outside the 10s retention window
    later = base + timedelta(seconds=11)
    book.apply_update(0, 1, "bid", 100.00, 160, timestamp=later)  # +10 replenishment, triggers prune
    assert len(book.replenishment_events) == 1                     # first event pruned
    assert book.recent_replenishment(base) == 10.0                 # only the +10 at `later` survives


def test_replenishment_events_use_timezone_aware_timestamps():
    book = DepthBook("NVDA")
    book.apply_update(0, 0, "bid", 100.00, 100)           # no timestamp provided
    book.apply_update(0, 1, "bid", 100.00, 150)           # replenishment, no timestamp
    ts, _price, _size = book.replenishment_events[0]
    assert ts.tzinfo is not None                          # must be timezone-aware
