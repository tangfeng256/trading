from __future__ import annotations

# In the machine_trading monorepo all strategy packages live under src/ and are
# available on PYTHONPATH via pyproject.toml.  sys.path injection is no longer
# needed.  These stubs keep existing call-sites in adapters.py, runner.py, and
# test_runner.py working without modification.

from pathlib import Path

TRADING_ROOT = Path(__file__).resolve().parents[2]          # machine_trading/
ABSORPTION_ROOT = TRADING_ROOT / "src" / "absorption"
PULLBACK_ROOT = TRADING_ROOT / "src" / "pullback_trend"
OPENING_RANGE_ROOT = TRADING_ROOT / "src" / "orm_ignition"


def add_strategy_paths() -> None:
    """No-op: packages are on PYTHONPATH via pyproject.toml src layout."""
    pass
