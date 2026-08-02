import json

import pytest

from multi_strategy.config import AppConfig, RuntimeConfig, apply_overrides, load_config


def test_config_overrides_parse_lists_and_bools():
    config = load_config(None, ["runtime.symbols=NVDA,AMD", "runtime.dry_run=true"])

    assert config.runtime.symbols == ["NVDA", "AMD"]
    assert config.runtime.dry_run is True


def test_smart_depth_defaults_to_enabled_for_smart_stock_depth():
    config = AppConfig()

    assert config.ib.exchange == "SMART"
    assert config.ib.smart_depth is True
    assert config.ib.max_depth_requests == 3
    assert config.ib.trailing_stop_enabled is True
    assert config.ib.trailing_activation_bps == 50.0
    assert config.ib.runner_target_enabled is True
    assert config.ib.runner_target_r_multiple == 6.0
    assert config.runtime.trading_timezone == "America/New_York"
    assert config.runtime.trading_start == "09:30:00"
    assert config.runtime.trading_end == "13:00:00"
    assert config.runtime.flatten_before_window_end_seconds == 60
    assert config.runtime.post_window_position_check_seconds == 30
    assert config.ib.fail_closed_on_depth_permission_error is False
    assert config.runtime.stop_loss_cooldown_seconds == 600
    assert config.runtime.auto_stop_after_window_seconds == 120
    assert config.runtime.quarantine_unmanaged_positions is True
    assert config.runtime.reconcile_account_positions is True
    assert config.runtime.startup_position_action == "prompt"
    assert config.runtime.position_flatten_timeout_seconds == 60


def test_load_config_raises_on_unknown_section_key(tmp_path):
    cfg = tmp_path / "config.json"
    cfg.write_text(json.dumps({"ib": {"traling_stop_enabled": True}}), encoding="utf-8")

    with pytest.raises(ValueError, match="traling_stop_enabled"):
        load_config(cfg)


def test_load_config_raises_on_unknown_runtime_key(tmp_path):
    cfg = tmp_path / "config.json"
    cfg.write_text(json.dumps({"runtime": {"typo_field": "bad"}}), encoding="utf-8")

    with pytest.raises(ValueError, match="typo_field"):
        load_config(cfg)


def test_apply_overrides_raises_on_unknown_section():
    config = AppConfig()

    with pytest.raises(ValueError, match="unknown config section"):
        apply_overrides(config, ["nonexistent.field=value"])


def test_apply_overrides_raises_on_unknown_field():
    config = AppConfig()

    with pytest.raises(ValueError, match="unknown config field"):
        apply_overrides(config, ["runtime.nonexistent_field=value"])


def test_config_overrides_parse_int_float_and_str():
    config = load_config(None, [
        "ib.depth_rows=10",
        "ib.trailing_activation_bps=75.5",
        "ib.exchange=NYSE",
    ])

    assert config.ib.depth_rows == 10
    assert isinstance(config.ib.depth_rows, int)
    assert config.ib.trailing_activation_bps == 75.5
    assert config.ib.exchange == "NYSE"


def test_runtime_config_raises_on_invalid_trading_start_format():
    with pytest.raises(ValueError, match="trading_start"):
        RuntimeConfig(trading_start="9:30")


def test_runtime_config_raises_on_invalid_trading_end_format():
    with pytest.raises(ValueError, match="trading_end"):
        RuntimeConfig(trading_end="11:00")


def test_runtime_config_rejects_invalid_startup_position_action():
    with pytest.raises(ValueError, match="startup_position_action"):
        RuntimeConfig(startup_position_action="ignore")
