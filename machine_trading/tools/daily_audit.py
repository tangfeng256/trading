from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from multi_strategy.audit import audit_run  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description="Summarize a trading run, including no-trade signal and rejection reasons.")
    parser.add_argument("--run-dir", required=True, help="Run folder, for example runs/20260612_131855")
    parser.add_argument("--timezone", default="America/New_York", help="Timezone for local start/end fields.")
    parser.add_argument("--no-write", action="store_true", help="Print the audit without writing daily_audit_* outputs.")
    args = parser.parse_args()

    summary = audit_run(args.run_dir, trading_timezone=args.timezone, write_outputs=not args.no_write)
    print(json.dumps(summary, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
