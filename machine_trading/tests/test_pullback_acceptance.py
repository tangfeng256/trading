from __future__ import annotations

from datetime import datetime, timedelta
from types import SimpleNamespace
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd

from pullback_trend.backtest import run_backtest
from pullback_trend.config import AppConfig, LoggingConfig, StrategyConfig, load_config_with_overrides
from pullback_trend.execution import _heartbeat_line, run_live
from pullback_trend.replay import run_replay


def write_bars(path: Path) -> None:
    start = datetime(2026, 5, 25, 13, 35, tzinfo=ZoneInfo("UTC"))
    rows = []
    for symbol, base in [("QQQ", 400), ("NVDA", 100)]:
        for i in range(36):
            close = base + i * (0.16 if symbol == "NVDA" else 0.08)
            if symbol == "NVDA" and i >= 25:
                close = [103.9, 103.6, 103.45, 103.5, 103.7, 104.05, 104.3, 104.7, 105.0, 105.3, 105.5][i - 25]
            rows.append({"symbol": symbol, "timestamp": start + timedelta(minutes=i), "open": close - 0.05, "high": close + 0.15, "low": close - 0.10, "close": close, "volume": 200000 if symbol == "NVDA" else 300000})
    pd.DataFrame(rows).to_csv(path, index=False)


def test_backtest_replay_and_dry_run_live_generate_logs(tmp_path):
    bars = tmp_path / "bars.csv"
    write_bars(bars)
    config = AppConfig(strategy=StrategyConfig(min_volume=1000, min_rvol=1.0, min_score=0.6), logging=LoggingConfig(base_dir=str(tmp_path), run_id="bt"))
    run_dir = run_backtest(config, bars)
    assert (run_dir / "decisions.csv").exists()
    assert (run_dir / "orders.csv").exists()
    assert (run_dir / "trades.csv").exists()
    assert (run_dir / "summary.json").exists()
    timeline = run_replay(run_dir)
    assert timeline.exists()
    dry_dir = run_live(AppConfig(logging=LoggingConfig(base_dir=str(tmp_path), run_id="dry")), dry_run=True)
    assert (dry_dir / "summary.json").exists()


def test_ib_streaming_quotes_can_be_enabled_with_override(tmp_path):
    config_path = tmp_path / "config.json"
    config_path.write_text("{}", encoding="utf-8")

    config = load_config_with_overrides(config_path, ["ib.request_streaming_quotes=true"])

    assert config.ib.request_streaming_quotes is True


def test_heartbeat_reports_monitoring_and_l2_state():
    config = AppConfig(strategy=StrategyConfig(symbols=["NVDA"], market_symbol="QQQ", use_l2=True))
    now = datetime(2026, 5, 25, 14, 0, tzinfo=ZoneInfo("UTC"))
    histories = {"NVDA": pd.DataFrame([{"timestamp": now}])}
    depth_tickers = {
        "NVDA": SimpleNamespace(
            domBids=[SimpleNamespace(price=100.0, size=200, marketMaker="")],
            domAsks=[SimpleNamespace(price=100.02, size=150, marketMaker="")],
        )
    }
    positions = SimpleNamespace(positions={})

    line = _heartbeat_line("paper", config, ["NVDA", "QQQ"], histories, depth_tickers, positions, {"NVDA": now})

    assert "heartbeat: ready_to_trade" in line
    assert "monitoring 1/2 symbols" in line
    assert "L2 1/1" in line
