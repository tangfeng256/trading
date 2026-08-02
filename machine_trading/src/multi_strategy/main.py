from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parent))

from multi_strategy.config import load_config
from multi_strategy.replay import replay_run
from multi_strategy.runner import MultiStrategyRunner


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run multiple intraday strategies with one per-symbol position lock.")
    sub = parser.add_subparsers(dest="command", required=True)
    for name in ("paper", "live"):
        p = sub.add_parser(name)
        p.add_argument("--config", default="config.sample.json")
        p.add_argument("--set", action="append", default=[], help="Override config, for example runtime.symbols=NVDA,MU")
        p.add_argument("--dry-run", action="store_true", help="Monitor and log signals without placing broker orders.")
        p.add_argument(
            "--startup-position-action",
            choices=("prompt", "close", "abort", "continue"),
            help="How to handle account positions found at startup (short positions cannot continue).",
        )
    replay = sub.add_parser("replay")
    replay.add_argument("--run-dir", required=True, help="Combined multi-strategy run folder to replay.")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.command == "replay":
        print(json.dumps(replay_run(args.run_dir), indent=2))
        return 0
    config = load_config(args.config, args.set)
    if args.dry_run:
        config.runtime.dry_run = True
    if args.startup_position_action:
        config.runtime.startup_position_action = args.startup_position_action
    run_dir = MultiStrategyRunner(config, mode=args.command).run()
    print(f"Run folder: {run_dir}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
