import csv
import json

from multi_strategy.replay import PositionState, replay_run


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def _write_csv(path, rows):
    headers = list(rows[0])
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=headers)
        writer.writeheader()
        writer.writerows(rows)


def _make_run_dir(tmp_path):
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    return run_dir


# ---------------------------------------------------------------------------
# main integration test
# ---------------------------------------------------------------------------

def test_replay_reconstructs_positions_duplicates_and_warnings(tmp_path):
    run_dir = _make_run_dir(tmp_path)

    _write_csv(
        run_dir / "orders.csv",
        [
            {"timestamp": "2026-05-26T14:29:21+00:00", "strategy": "absorption", "symbol": "TSLA", "order_id": "entry-1", "role": "entry", "action": "BUY", "quantity": "50", "limit_price": "426.72", "stop_price": "", "dry_run": "False"},
            {"timestamp": "2026-05-26T14:29:30+00:00", "strategy": "absorption", "symbol": "TSLA", "order_id": "entry-1", "role": "entry", "action": "BUY", "quantity": "50", "limit_price": "426.72", "stop_price": "", "dry_run": "False"},
            {"timestamp": "2026-05-26T14:35:00+00:00", "strategy": "absorption", "symbol": "TSLA", "order_id": "tp-2",    "role": "tp2",   "action": "SELL", "quantity": "25", "limit_price": "427.72", "stop_price": "", "dry_run": "False"},
        ],
    )
    _write_csv(
        run_dir / "fills.csv",
        [
            {"timestamp": "2026-05-26T14:29:22+00:00", "strategy": "absorption", "symbol": "TSLA", "order_id": "entry-1", "role": "entry", "quantity": "50", "price": "426.72"},
            {"timestamp": "2026-05-26T14:35:01+00:00", "strategy": "absorption", "symbol": "TSLA", "order_id": "tp-2",    "role": "tp2",   "quantity": "60", "price": "427.72"},
        ],
    )
    (run_dir / "events.jsonl").write_text(
        "\n".join([
            json.dumps({"logged_at": "2026-05-26T14:28:17+00:00", "type": "depth_subscription_limit", "selected": ["NVDA"], "l1_only": ["TQQQ"]}),
            json.dumps({"logged_at": "2026-05-26T14:30:00+00:00", "type": "risk_reject", "strategy": "absorption", "symbol": "NVDA", "reason": "stop_distance_too_small"}),
            json.dumps({"time":      "2026-05-26T14:31:00+00:00", "type": "strategy_skip_locked", "strategy": "pullback", "symbol": "TSLA", "owner": "absorption"}),
        ]),
        encoding="utf-8",
    )
    (run_dir / "decisions.csv").write_text(
        "strategy,symbol,approved,reason\n"
        "absorption,TSLA,,trigger_confirmed,,2026-05-26T14:29:21+00:00,TRIGGER,True\n",
        encoding="utf-8",
    )

    summary = replay_run(run_dir)

    assert summary["orders"] == 3
    assert summary["fills"] == 2
    assert summary["duplicate_order_ids"] == {"entry-1": 2}
    assert summary["risk_rejects"] == {"stop_distance_too_small": 1}
    assert summary["strategy_skips"] == {"pullback": 1}
    assert summary["approved_decisions"] == {"absorption": 1}
    assert summary["positions"][0]["quantity"] == 0
    assert summary["positions"][0]["realized_pnl"] == 50.0
    assert any("entry-1 was submitted 2 times" in w for w in summary["warnings"])
    assert any("oversell_qty=10" in w for w in summary["warnings"])
    assert (run_dir / "replay_timeline.csv").exists()
    assert (run_dir / "replay_summary.json").exists()


# ---------------------------------------------------------------------------
# lifecycle events appear in timeline
# ---------------------------------------------------------------------------

def test_replay_includes_forced_flatten_and_stop_breach_in_timeline(tmp_path):
    run_dir = _make_run_dir(tmp_path)
    (run_dir / "orders.csv").write_text("timestamp,strategy,symbol,order_id,role,action,quantity,limit_price,stop_price,dry_run\n", encoding="utf-8")
    (run_dir / "fills.csv").write_text("timestamp,strategy,symbol,order_id,role,quantity,price\n", encoding="utf-8")
    (run_dir / "decisions.csv").write_text("strategy,symbol,approved\n", encoding="utf-8")
    (run_dir / "events.jsonl").write_text(
        "\n".join([
            json.dumps({"logged_at": "2026-06-02T14:59:00+00:00", "type": "forced_flatten_submitted", "strategy": "absorption", "symbol": "NVDA", "quantity": 100, "reason": "trading_window_close"}),
            json.dumps({"logged_at": "2026-06-02T14:45:00+00:00", "type": "stop_breach_flatten_submitted", "strategy": "pullback", "symbol": "TSLA", "quantity": 50, "price": 210.0, "stop_price": 211.0}),
            json.dumps({"logged_at": "2026-06-02T14:50:00+00:00", "type": "stale_entry_order_locks_expired", "symbols": ["AMD"]}),
        ]),
        encoding="utf-8",
    )

    summary = replay_run(run_dir)

    timeline_path = run_dir / "replay_timeline.csv"
    import csv as _csv
    with timeline_path.open() as f:
        rows = list(_csv.DictReader(f))
    event_types = [r["event"] for r in rows]
    assert "forced_flatten_submitted" in event_types
    assert "stop_breach_flatten_submitted" in event_types
    assert "stale_entry_order_locks_expired" in event_types
    details = {r["event"]: r["details"] for r in rows}
    assert "trading_window_close" in details["forced_flatten_submitted"]
    assert "AMD" in details["stale_entry_order_locks_expired"]


# ---------------------------------------------------------------------------
# short position P&L
# ---------------------------------------------------------------------------

def test_replay_correctly_tracks_pnl_for_account_short_position(tmp_path):
    run_dir = _make_run_dir(tmp_path)
    _write_csv(
        run_dir / "orders.csv",
        [{"timestamp": "2026-06-02T14:59:00+00:00", "strategy": "account", "symbol": "NVDA", "order_id": "flatten-account-NVDA", "role": "flatten", "action": "BUY", "quantity": "4", "limit_price": "", "stop_price": "", "dry_run": "False"}],
    )
    _write_csv(
        run_dir / "fills.csv",
        [{"timestamp": "2026-06-02T14:59:00+00:00", "strategy": "account", "symbol": "NVDA", "order_id": "flatten-account-NVDA", "role": "flatten", "quantity": "4", "price": "226.60"}],
    )
    (run_dir / "decisions.csv").write_text("strategy,symbol,approved\n", encoding="utf-8")
    (run_dir / "events.jsonl").write_text(
        json.dumps({
            "logged_at": "2026-06-02T13:20:00+00:00",
            "type": "account_positions_synced",
            "positions": [{"symbol": "NVDA", "quantity": 4, "avg_price": 226.59, "side": "SHORT"}],
        }),
        encoding="utf-8",
    )

    summary = replay_run(run_dir)

    nvda = next(p for p in summary["positions"] if p["symbol"] == "NVDA")
    assert nvda["side"] == "SHORT"
    assert nvda["quantity"] == 0
    assert round(nvda["realized_pnl"], 4) == round((226.59 - 226.60) * 4, 4)
    assert not any("open" in w for w in summary["warnings"])


def test_replay_warns_on_unclosed_short_position(tmp_path):
    run_dir = _make_run_dir(tmp_path)
    (run_dir / "orders.csv").write_text("timestamp,strategy,symbol,order_id,role,action,quantity,limit_price,stop_price,dry_run\n", encoding="utf-8")
    (run_dir / "fills.csv").write_text("timestamp,strategy,symbol,order_id,role,quantity,price\n", encoding="utf-8")
    (run_dir / "decisions.csv").write_text("strategy,symbol,approved\n", encoding="utf-8")
    (run_dir / "events.jsonl").write_text(
        json.dumps({
            "logged_at": "2026-06-02T13:20:00+00:00",
            "type": "account_positions_synced",
            "positions": [{"symbol": "NVDA", "quantity": 4, "avg_price": 226.59, "side": "SHORT"}],
        }),
        encoding="utf-8",
    )

    summary = replay_run(run_dir)

    assert any("open short exposure remains" in w for w in summary["warnings"])


# ---------------------------------------------------------------------------
# PositionState unit tests
# ---------------------------------------------------------------------------

def test_position_state_apply_cover_reduces_short_and_computes_pnl():
    pos = PositionState(quantity=10, avg_price=100.0)

    pnl, overshoot = pos.apply_cover(6, 98.0)

    assert pos.quantity == 4
    assert round(pnl, 4) == round((100.0 - 98.0) * 6, 4)
    assert overshoot == 0
    assert round(pos.realized_pnl, 4) == round(pnl, 4)


def test_position_state_apply_cover_detects_overshoot():
    pos = PositionState(quantity=3, avg_price=100.0)

    pnl, overshoot = pos.apply_cover(5, 99.0)

    assert pos.quantity == 0
    assert overshoot == 2
    assert round(pnl, 4) == round((100.0 - 99.0) * 3, 4)
