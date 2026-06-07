"""
Machine Trading — unified entry point.

Run any strategy or the multi-strategy orchestrator:

  python run.py absorption    paper   --config configs/absorption.sample.json
  python run.py pullback      paper   --config configs/pullback.sample.json
  python run.py opening_range paper   --config configs/opening_range.sample.json
  python run.py orchestrator  paper   --config configs/orchestrator.sample.json [--set k=v ...]

  python run.py absorption    live    --config configs/absorption.sample.json
  python run.py orchestrator  live    --config configs/orchestrator.sample.json

  python run.py orchestrator  replay  --run-dir runs/20260602_093000
  python run.py opening_range backtest --config configs/opening_range.sample.json --bars bars.csv

Available strategies
--------------------
  absorption    Bid-side absorption reversal (L2 depth + tape)
  pullback      Intraday pullback trend-following (bar-based)
  opening_range Opening range momentum breakout (OR + reignition)
  orchestrator  All three strategies on one account with shared position locking
"""
from __future__ import annotations

import runpy
import sys

STRATEGIES: dict[str, str] = {
    "absorption":    "absorption.main",
    "pullback":      "pullback_trend.main",
    "opening_range": "orm_ignition.main",
    "orchestrator":  "multi_strategy.main",
}


def main() -> None:
    if len(sys.argv) < 2 or sys.argv[1] not in STRATEGIES:
        print(__doc__)
        print("Available strategies:", ", ".join(STRATEGIES))
        sys.exit(1)

    strategy = sys.argv[1]
    # Replace argv[0] with a descriptive name; the strategy's argparse sees the rest.
    sys.argv = [f"run.py {strategy}", *sys.argv[2:]]
    runpy.run_module(STRATEGIES[strategy], run_name="__main__", alter_sys=True)


if __name__ == "__main__":
    main()
