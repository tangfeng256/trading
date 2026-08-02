from __future__ import annotations

import json
from dataclasses import dataclass, field, fields, is_dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, get_args, get_origin


@dataclass
class StrategyConfig:
    symbols: List[str] = field(
        default_factory=lambda: ["NVDA", "TSLA", "MU", "AAPL", "MSFT", "META", "QQQ", "TQQQ"]
    )
    or_start: str = "09:30"
    or_end: str = "09:45"
    trade_start: str = "09:45"
    trade_end: str = "11:30"
    max_hold_minutes: int = 30
    max_spread_bps: float = 8.0
    min_volume: int = 100_000
    min_rel_volume: float = 1.2
    min_or_move_bps: float = 35.0
    min_reignite_volume_mult: float = 1.25
    close_location_min: float = 0.65
    or_reclaim_tolerance_bps: float = 8.0
    volatility_buffer_mult: float = 0.25
    market_symbols: List[str] = field(default_factory=lambda: ["QQQ", "SPY"])
    market_negative_bps: float = -45.0
    use_l2: bool = False


@dataclass
class IBConfig:
    host: str = "127.0.0.1"
    paper_port: int = 7497
    live_port: int = 7496
    client_id: int = 31
    exchange: str = "SMART"
    currency: str = "USD"
    market_data_type: int = 1
    depth_rows: int = 5
    smart_depth: bool = False
    reconnect_attempts: int = 5
    reconnect_sleep_seconds: float = 3.0


@dataclass
class RiskConfig:
    account_size: float = 50_000.0
    risk_per_trade_pct: float = 0.003
    max_daily_loss_pct: float = 0.01
    max_trades_per_day: int = 3
    max_total_positions: int = 3
    max_position_notional: float = 25_000.0
    min_stop_bps: float = 8.0
    max_stop_bps: float = 120.0
    entry_stale_seconds: int = 8
    max_slippage_bps: float = 5.0
    tp1_fraction: float = 0.5
    tp1_r: float = 1.0
    tp2_r: float = 2.0
    breakeven_buffer_bps: float = 2.0
    commission_per_share: float = 0.005
    slippage_bps: float = 2.0
    kill_switch_file: str = "KILL_SWITCH"

    @property
    def risk_dollars(self) -> float:
        return self.account_size * self.risk_per_trade_pct

    @property
    def max_daily_loss_dollars(self) -> float:
        return self.account_size * self.max_daily_loss_pct


@dataclass
class LoggingConfig:
    base_dir: str = "runs"
    run_id: Optional[str] = None
    write_book_snapshots: bool = True


@dataclass
class AppConfig:
    strategy: StrategyConfig = field(default_factory=StrategyConfig)
    ib: IBConfig = field(default_factory=IBConfig)
    risk: RiskConfig = field(default_factory=RiskConfig)
    logging: LoggingConfig = field(default_factory=LoggingConfig)


def load_config(path: str | Path) -> AppConfig:
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    return config_from_dict(data)


def load_config_with_overrides(path: str | Path, overrides: List[str] | None = None) -> AppConfig:
    config = load_config(path)
    apply_overrides(config, overrides or [])
    return config


def config_from_dict(data: Dict[str, Any]) -> AppConfig:
    return AppConfig(
        strategy=_coerce(StrategyConfig, data.get("strategy", {})),
        ib=_coerce(IBConfig, data.get("ib", {})),
        risk=_coerce(RiskConfig, data.get("risk", {})),
        logging=_coerce(LoggingConfig, data.get("logging", {})),
    )


def _coerce(cls: type, data: Dict[str, Any]) -> Any:
    valid = {f.name for f in fields(cls)}
    return cls(**{k: v for k, v in data.items() if k in valid})


def apply_overrides(config: AppConfig, overrides: List[str]) -> None:
    for override in overrides:
        path, raw_value = _split_override(override)
        target, field_name = _resolve_override_target(config, path)
        current_value = getattr(target, field_name)
        field_type = next(field.type for field in fields(target) if field.name == field_name)
        setattr(target, field_name, _parse_override_value(raw_value, current_value, field_type))


def _split_override(override: str) -> tuple[List[str], str]:
    if "=" not in override:
        raise ValueError(f"Invalid override {override!r}. Expected section.field=value.")
    name, value = override.split("=", 1)
    path = [part.strip() for part in name.split(".") if part.strip()]
    if len(path) != 2:
        raise ValueError(f"Invalid override {override!r}. Expected section.field=value.")
    return path, value.strip()


def _resolve_override_target(config: AppConfig, path: List[str]) -> tuple[Any, str]:
    section_name, field_name = path
    if not hasattr(config, section_name):
        raise ValueError(f"Unknown config section {section_name!r}.")
    target = getattr(config, section_name)
    if not is_dataclass(target):
        raise ValueError(f"Config section {section_name!r} cannot be overridden.")
    valid_fields = {field.name for field in fields(target)}
    if field_name not in valid_fields:
        raise ValueError(f"Unknown config field {section_name}.{field_name}.")
    return target, field_name


def _parse_override_value(raw_value: str, current_value: Any, field_type: Any) -> Any:
    if raw_value.lower() in {"null", "none"}:
        if _allows_none(field_type) or current_value is None:
            return None
        raise ValueError("This config field does not accept null.")
    if isinstance(current_value, bool):
        return _parse_bool(raw_value)
    if isinstance(current_value, int) and not isinstance(current_value, bool):
        return int(raw_value)
    if isinstance(current_value, float):
        return float(raw_value)
    if isinstance(current_value, list):
        return _parse_list(raw_value, current_value)
    if isinstance(current_value, str) or current_value is None:
        return raw_value
    return json.loads(raw_value)


def _parse_bool(raw_value: str) -> bool:
    value = raw_value.lower()
    if value in {"1", "true", "yes", "y", "on"}:
        return True
    if value in {"0", "false", "no", "n", "off"}:
        return False
    raise ValueError(f"Invalid boolean value {raw_value!r}.")


def _parse_list(raw_value: str, current_value: List[Any]) -> List[Any]:
    if raw_value.startswith("["):
        parsed = json.loads(raw_value)
        if not isinstance(parsed, list):
            raise ValueError("List override JSON must produce a list.")
        return parsed
    values = [value.strip() for value in raw_value.split(",") if value.strip()]
    if not current_value:
        return values
    item_type = type(current_value[0])
    return [item_type(value) for value in values]


def _allows_none(field_type: Any) -> bool:
    origin = get_origin(field_type)
    return type(None) in get_args(field_type) if origin is not None else False
