"""
In the machine_trading monorepo, add_strategy_paths() is a no-op: all packages
are available on PYTHONPATH via pyproject.toml src layout.  These tests verify
the stub behaves correctly (doesn't modify sys.path, doesn't crash).
"""
import sys

import multi_strategy.paths as paths_module
from pathlib import Path


def test_add_strategy_paths_is_a_no_op():
    before = list(sys.path)
    paths_module.add_strategy_paths()
    assert sys.path == before


def test_add_strategy_paths_is_idempotent():
    before = list(sys.path)
    paths_module.add_strategy_paths()
    paths_module.add_strategy_paths()
    assert sys.path == before


def test_add_strategy_paths_does_not_print_anything(capsys):
    paths_module.add_strategy_paths()
    assert capsys.readouterr().out == ""


def test_strategy_root_constants_point_inside_machine_trading():
    # Each root should resolve to somewhere under the machine_trading src/ tree.
    mt_root = Path(__file__).resolve().parents[1]  # machine_trading/
    for attr in ("ABSORPTION_ROOT", "PULLBACK_ROOT", "OPENING_RANGE_ROOT"):
        path = getattr(paths_module, attr)
        assert str(mt_root) in str(path), f"{attr} points outside machine_trading: {path}"
