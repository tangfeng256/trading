import json
from datetime import datetime, timezone

from multi_strategy.logger import MultiStrategyLogger, _safe


def test_event_logged_at_does_not_collide_with_payload_time(tmp_path):
    logger = MultiStrategyLogger(tmp_path)

    logger.event("test_event", {"time": "payload-time", "value": 42})

    row = json.loads((logger.run_dir / "events.jsonl").read_text().splitlines()[0])
    assert row["time"] == "payload-time"
    assert "logged_at" in row
    assert row["logged_at"] != "payload-time"


def test_event_without_payload_time_uses_logged_at(tmp_path):
    logger = MultiStrategyLogger(tmp_path)

    logger.event("test_event", {"value": 42})

    row = json.loads((logger.run_dir / "events.jsonl").read_text().splitlines()[0])
    assert "logged_at" in row
    assert "time" not in row


def test_csv_writes_header_only_once(tmp_path):
    logger = MultiStrategyLogger(tmp_path)

    logger.csv("orders", {"symbol": "NVDA", "qty": 100})
    logger.csv("orders", {"symbol": "TSLA", "qty": 50})

    lines = (logger.run_dir / "orders.csv").read_text().splitlines()
    assert lines[0] == "symbol,qty"
    assert lines.count("symbol,qty") == 1
    assert len(lines) == 3  # header + 2 data rows


def test_csv_skips_header_when_file_already_has_content(tmp_path):
    run_id = "existing_run"
    run_dir = tmp_path / run_id
    run_dir.mkdir()
    csv_path = run_dir / "orders.csv"
    csv_path.write_text("symbol,qty\nNVDA,100\n", encoding="utf-8")

    logger = MultiStrategyLogger(tmp_path, run_id=run_id)
    logger.csv("orders", {"symbol": "TSLA", "qty": 50})

    lines = csv_path.read_text().splitlines()
    assert lines.count("symbol,qty") == 1  # header not duplicated
    assert lines[-1] == "TSLA,50"


def test_safe_serializes_set_deterministically():
    result1 = _safe({"TSLA", "NVDA", "AMD"})
    result2 = _safe({"TSLA", "NVDA", "AMD"})

    assert result1 == result2
    assert result1 == sorted(result1)


def test_safe_preserves_list_order():
    result = _safe(["TSLA", "NVDA", "AMD"])

    assert result == ["TSLA", "NVDA", "AMD"]


def test_safe_handles_datetime_and_nested_structures():
    ts = datetime(2026, 6, 2, 14, 0, tzinfo=timezone.utc)

    result = _safe({"ts": ts, "items": [ts, 1, None], "flag": True})

    assert result["ts"] == "2026-06-02T14:00:00+00:00"
    assert result["items"][0] == "2026-06-02T14:00:00+00:00"
    assert result["items"][1] == 1
    assert result["items"][2] is None
    assert result["flag"] is True


def test_safe_falls_back_to_str_for_unknown_types():
    class _Custom:
        def __str__(self):
            return "custom-repr"

    assert _safe(_Custom()) == "custom-repr"


def test_finalize_writes_summary_json(tmp_path):
    logger = MultiStrategyLogger(tmp_path)

    logger.finalize({"mode": "paper", "symbols": ["NVDA"]})

    summary = json.loads((logger.run_dir / "summary.json").read_text())
    assert summary["mode"] == "paper"
    assert summary["symbols"] == ["NVDA"]
