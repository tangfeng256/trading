import csv
import json

from multi_strategy.audit import audit_run
from multi_strategy.replay import replay_run


def _write_headers(path, headers):
    path.write_text(",".join(headers) + "\n", encoding="utf-8")


def _write_csv(path, rows):
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)


def test_daily_audit_explains_no_trade_day_with_legacy_decision_rows(tmp_path):
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    (run_dir / "decisions.csv").write_text(
        "\n".join(
            [
                "strategy,symbol,timestamp,phase,passed,reason,mid,spread,delta,absorption_score,exhaustion_score,trigger_score",
                "absorption,NVDA,2026-06-12T14:08:05+00:00,IDLE,False,waiting_for_absorption,205.365,0.01,0,0.56,0.53,0.15",
                "pullback,NVDA,,,,market_regime_ok;trend_ok;volume_not_declining;break_stabilization_high,,,,,,,True,0.94",
                "opening_range,TQQQ,,,,signal,,,,,,,True,",
            ]
        ),
        encoding="utf-8",
    )
    (run_dir / "events.jsonl").write_text(
        "\n".join(
            [
                json.dumps({"logged_at": "2026-06-12T13:19:17+00:00", "type": "session_started"}),
                json.dumps({"logged_at": "2026-06-12T13:19:17+00:00", "type": "depth_subscription_limit", "selected": ["NVDA"], "l1_only": ["TQQQ"]}),
                json.dumps({"logged_at": "2026-06-12T14:08:05+00:00", "type": "risk_reject", "strategy": "pullback", "symbol": "NVDA", "reason": "entry_slippage_too_high"}),
            ]
        ),
        encoding="utf-8",
    )
    nested = run_dir / "opening_range" / "20260612_143605"
    nested.mkdir(parents=True)
    _write_csv(
        nested / "decisions.csv",
        [
            {
                "time": "2026-06-12T14:36:05+00:00",
                "symbol": "TQQQ",
                "phase": "risk",
                "passed": "False",
                "reason": "stop_too_wide",
                "features": "{}",
            }
        ],
    )
    _write_headers(run_dir / "orders.csv", ["timestamp", "strategy", "symbol", "order_id"])
    _write_headers(run_dir / "fills.csv", ["timestamp", "strategy", "symbol", "order_id"])
    _write_csv(run_dir / "dom_ticks.csv", [{"timestamp": "2026-06-12T14:08:05+00:00", "symbol": "NVDA"}])
    _write_csv(run_dir / "depth_snapshots.csv", [{"timestamp": "2026-06-12T14:08:05+00:00", "symbol": "NVDA"}])

    summary = audit_run(run_dir)

    assert summary["assessment"]["status"] == "no_trades"
    assert summary["assessment"]["approved_signals"] == 2
    assert summary["assessment"]["blocking_rejections"] == 2
    assert summary["assessment"]["replay_recommended"] is True
    assert summary["strategies"]["pullback"]["approved"] == 1
    assert summary["strategies"]["opening_range"]["approved"] == 1
    assert summary["risk_rejects"]["entry_slippage_too_high"] == 1
    assert summary["risk_rejects"]["stop_too_wide"] == 1
    assert any(row["blocked_reason"] == "entry_slippage_too_high" for row in summary["signal_candidates"])
    assert any(row["blocked_reason"] == "stop_too_wide" for row in summary["signal_candidates"])
    assert any("extra unnamed columns" in warning for warning in summary["operational_warnings"])
    assert (run_dir / "daily_audit_summary.json").exists()
    assert (run_dir / "daily_audit_candidates.csv").exists()


def test_replay_writes_daily_audit_outputs(tmp_path):
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    _write_headers(run_dir / "orders.csv", ["timestamp", "strategy", "symbol", "order_id", "role", "action", "quantity", "limit_price", "stop_price", "dry_run"])
    _write_headers(run_dir / "fills.csv", ["timestamp", "strategy", "symbol", "order_id", "role", "quantity", "price"])
    _write_headers(run_dir / "decisions.csv", ["strategy", "symbol", "approved", "reason"])
    (run_dir / "events.jsonl").write_text("", encoding="utf-8")

    summary = replay_run(run_dir)

    assert summary["daily_audit"]["assessment"]["status"] == "no_trades"
    assert (run_dir / "daily_audit_summary.json").exists()
    assert (run_dir / "daily_audit_candidates.csv").exists()


def test_daily_audit_reports_unique_operational_entry_blocks(tmp_path):
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    _write_headers(run_dir / "decisions.csv", ["strategy", "symbol", "approved", "reason"])
    _write_headers(run_dir / "orders.csv", ["timestamp", "strategy", "symbol", "order_id"])
    _write_headers(run_dir / "fills.csv", ["timestamp", "strategy", "symbol", "order_id"])
    events = [
        {"type": "depth_strategy_symbol_blocked", "symbol": "TSLA", "reason": "depth_permissions_unavailable"},
        {"type": "unmanaged_position_quarantined", "symbol": "NVDA"},
        {"type": "strategy_skip_entry_blocked", "strategy": "pullback", "symbol": "TSLA", "reason": "depth_permissions_unavailable"},
        {"type": "strategy_skip_entry_blocked", "strategy": "pullback", "symbol": "TSLA", "reason": "depth_permissions_unavailable"},
        {"type": "strategy_skip_entry_blocked", "strategy": "opening_range", "symbol": "NVDA", "reason": "unmanaged_account_position"},
    ]
    (run_dir / "events.jsonl").write_text("\n".join(json.dumps(event) for event in events), encoding="utf-8")

    summary = audit_run(run_dir, write_outputs=False)

    assert summary["assessment"]["blocking_rejections"] == 2
    assert summary["assessment"]["primary_explanation"] == "strategy entry paths were operationally blocked before an order could be submitted"
    assert summary["risk_rejects"] == {"depth_permissions_unavailable": 1, "unmanaged_account_position": 1}
    assert any("IBKR depth permissions" in warning for warning in summary["operational_warnings"])
    assert any("unmanaged account positions" in warning for warning in summary["operational_warnings"])
