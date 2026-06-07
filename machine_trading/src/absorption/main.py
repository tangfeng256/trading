from __future__ import annotations

import argparse
import json
import sys
import time
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from absorption.backtest import run_backtest
from absorption.config import apply_overrides, load_config
from absorption.ib_client import IBClient
from absorption.live_engine import LiveTradingEngine
from absorption.logger import RunLogger
from absorption.replay import replay_run


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m absorption.main")
    sub = parser.add_subparsers(dest="command", required=True)
    for name in ("live", "paper", "backtest"):
        p = sub.add_parser(name)
        p.add_argument("--config", default="config.sample.json")
        p.add_argument("--set", action="append", default=[], help="Override config value, for example strategy.trade_end=10:45:00")
        if name in {"live", "paper"}:
            p.add_argument("--port", type=int, help="Override IBKR API port")
            p.add_argument("--client", type=int, help="Override IBKR API client id")
    sub.add_parser("replay").add_argument("--run-dir", required=True)
    sub.choices["backtest"].add_argument("--data", required=True)
    return parser


def _apply_connection_args(config, args):
    port = getattr(args, "port", None)
    client_id = getattr(args, "client", None)
    if port is None and client_id is None:
        return config
    ib = config.ib
    if port is not None:
        ib = replace(ib, port=port)
    if client_id is not None:
        ib = replace(ib, client_id=client_id)
    return replace(config, ib=ib)


def _run_ib_session(config, logger: RunLogger, mode: str, paper: bool) -> int:
    client = IBClient(config.ib, paper=paper)
    engine = None
    status = "starting"
    heartbeat_interval_sec = 30
    try:
        ib = client.connect()
        engine = LiveTradingEngine(config, ib, logger, submit_orders=True)
        engine.start()
        status = "connected"
        logger.event(
            "session_started",
            {
                "mode": mode,
                "symbols": config.symbols,
                "host": config.ib.host,
                "port": config.ib.port,
                "client_id": config.ib.client_id,
            },
        )
        print(f"{mode.title()} strategy connected. Run folder: {logger.run_dir}")
        print("Press Ctrl+C to stop.")
        next_heartbeat = time.monotonic()
        while ib.isConnected():
            now = time.monotonic()
            if now >= next_heartbeat:
                timestamp = datetime.now(timezone.utc).isoformat(timespec="seconds")
                print(
                    f"[{timestamp}] {mode} heartbeat: connected to "
                    f"{config.ib.host}:{config.ib.port}, monitoring {', '.join(config.symbols)}"
                )
                next_heartbeat = now + heartbeat_interval_sec
            if hasattr(ib, "waitOnUpdate"):
                ib.waitOnUpdate(timeout=1)
            else:
                time.sleep(1)
            engine.poll()
        status = "disconnected"
        return 0
    except KeyboardInterrupt:
        status = "stopped"
        print(f"\n{mode.title()} strategy stopping. Run folder: {logger.run_dir}")
        return 0
    except Exception:
        status = "failed"
        raise
    finally:
        if engine:
            engine.stop()
        client.disconnect()
        logger.finalize(
            {
                "mode": mode,
                "status": status,
                "symbols": config.symbols,
                "run_dir": str(logger.run_dir),
                "ib": {
                    "host": config.ib.host,
                    "port": config.ib.port,
                    "client_id": config.ib.client_id,
                },
            }
        )


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.command == "replay":
        print(json.dumps(replay_run(args.run_dir), indent=2))
        return 0
    config = _apply_connection_args(apply_overrides(load_config(args.config), getattr(args, "set", [])), args)
    if args.command == "backtest":
        print(json.dumps(run_backtest(config, args.data), indent=2))
        return 0
    logger = RunLogger(config.logging.root)
    if args.command == "live":
        return _run_ib_session(config, logger, mode="live", paper=False)
    if args.command == "paper":
        return _run_ib_session(config, logger, mode="paper", paper=True)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
