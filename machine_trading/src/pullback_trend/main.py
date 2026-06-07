from __future__ import annotations

import argparse

from pullback_trend.backtest import run_backtest
from pullback_trend.config import load_config, load_config_with_overrides
from pullback_trend.execution import run_live, run_paper
from pullback_trend.replay import run_replay


def main() -> int:
    parser = argparse.ArgumentParser(description="Intraday Pullback Trend Following")
    sub = parser.add_subparsers(dest="cmd", required=True)

    backtest = sub.add_parser("backtest")
    backtest.add_argument("--config", required=True)
    backtest.add_argument("--bars", required=True)
    backtest.add_argument("--set", action="append", default=[])

    replay = sub.add_parser("replay")
    replay.add_argument("--run-dir", required=True)

    paper = sub.add_parser("paper")
    _add_broker_args(paper)

    live = sub.add_parser("live")
    _add_broker_args(live)

    args = parser.parse_args()
    if args.cmd == "backtest":
        run_dir = run_backtest(load_config_with_overrides(args.config, args.set), args.bars)
        print(f"Backtest complete: {run_dir}")
        return 0
    if args.cmd == "replay":
        path = run_replay(args.run_dir)
        print(f"Replay timeline: {path}")
        return 0
    if args.cmd == "paper":
        config = _broker_config(args)
        run_dir = run_paper(config, dry_run=args.dry_run)
        print(f"Paper session started: {run_dir}")
        return 0
    if args.cmd == "live":
        config = _broker_config(args)
        run_dir = run_live(config, dry_run=args.dry_run)
        print(f"Live session started: {run_dir}")
        return 0
    raise AssertionError("unreachable")


def _add_broker_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--config", required=True)
    parser.add_argument("--port", type=int, default=None)
    parser.add_argument("--client-id", type=int, default=None)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--set", action="append", default=[])


def _broker_config(args: argparse.Namespace):
    config = load_config_with_overrides(args.config, args.set)
    if args.port is not None:
        config.ib.port = args.port
    if args.client_id is not None:
        config.ib.client_id = args.client_id
    return config


if __name__ == "__main__":
    raise SystemExit(main())
