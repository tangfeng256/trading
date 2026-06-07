from __future__ import annotations

import csv
import json
from datetime import datetime
from pathlib import Path
from typing import Any

from .config import AppConfig
from .risk_manager import RiskManager
from .signal_engine import SignalEngine


def run_backtest(config: AppConfig, data_dir: str | Path, output_dir: str | Path = "backtest_output") -> dict[str, Any]:
    """Event-driven skeleton using recorded feature CSVs.

    The same signal engine and risk manager are used as live. For rich tick/depth
    logs, first run replay to reconstruct feature rows.
    """
    data_path = Path(data_dir)
    out = Path(output_dir)
    out.mkdir(parents=True, exist_ok=True)
    signal_engine = SignalEngine(config.strategy, config.market.tick_size)
    risk = RiskManager(config.risk, config.strategy)
    decisions: list[dict] = []
    trades: list[dict] = []
    equity = config.risk.account_equity
    equity_curve: list[dict] = []

    for file in sorted(data_path.glob("*features*.csv")):
        with file.open(newline="", encoding="utf-8") as f:
            for row in csv.DictReader(f):
                features = _coerce_feature_row(row)
                symbol = features.get("symbol") or file.stem.split("_")[0]
                signal, decision = signal_engine.evaluate(symbol, features)
                decisions.append(decision)
                if signal:
                    rd = risk.approve(signal)
                    if rd.approved:
                        risk.mark_trade_opened(symbol, signal.timestamp)
                        risk.mark_trade_closed(symbol, signal.timestamp, 0.0)
                        trades.append({"timestamp": signal.timestamp.isoformat(), "symbol": symbol, "qty": rd.qty, "entry": signal.entry_ref_price, "stop": signal.stop_price})
                equity_curve.append({"timestamp": features["timestamp"].isoformat(), "equity": equity})

    _write_csv(out / "trades.csv", trades)
    _write_csv(out / "decision_report.csv", decisions)
    _write_csv(out / "equity_curve.csv", equity_curve)
    summary = {"trades": len(trades), "ending_equity": equity, "decisions": len(decisions)}
    (out / "summary.json").write_text(json.dumps(summary, indent=2, default=str), encoding="utf-8")
    return summary


def _coerce_feature_row(row: dict[str, str]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for key, value in row.items():
        if key == "timestamp":
            out[key] = datetime.fromisoformat(value)
        elif value in {"", "None"}:
            out[key] = None
        else:
            try:
                out[key] = float(value)
            except ValueError:
                out[key] = value
    return out


def _write_csv(path: Path, rows: list[dict]) -> None:
    fieldnames = sorted({key for row in rows for key in row.keys()})
    with path.open("w", newline="", encoding="utf-8") as f:
        if not fieldnames:
            f.write("")
            return
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)
