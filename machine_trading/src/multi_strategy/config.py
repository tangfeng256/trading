from __future__ import annotations

import json
from dataclasses import dataclass, field, fields, is_dataclass
from datetime import datetime
from pathlib import Path
from typing import Any


@dataclass
class IBConfig:
    host: str = "127.0.0.1"
    paper_port: int = 7497
    live_port: int = 7496
    client_id: int = 41
    exchange: str = "SMART"
    currency: str = "USD"
    primary_exchange: str = "NASDAQ"
    market_data_type: int = 1
    depth_rows: int = 5
    smart_depth: bool = True
    max_depth_requests: int = 3
    depth_symbols: list[str] = field(default_factory=list)
    trailing_stop_enabled: bool = True
    trailing_activation_bps: float = 50.0
    trailing_distance_bps: float = 35.0
    trailing_min_step_bps: float = 5.0
    runner_target_enabled: bool = True
    runner_target_r_multiple: float = 6.0
    historical_duration: str = "2 D"
    bar_size: str = "1 min"
    use_rth: bool = True
    heartbeat_seconds: int = 30


@dataclass
class StrategyFiles:
    absorption: str = "configs/absorption.json"
    pullback: str = "configs/pullback.json"
    opening_range: str = "configs/opening_range.json"


@dataclass
class RuntimeConfig:
    symbols: list[str] = field(default_factory=lambda: ["NVDA", "TSLA", "AMD", "TQQQ"])
    enabled_strategies: list[str] = field(default_factory=lambda: ["opening_range", "pullback", "absorption"])
    strategy_priority: list[str] = field(default_factory=lambda: ["opening_range", "pullback", "absorption"])
    trading_timezone: str = "America/New_York"
    trading_start: str = "09:30:00"
    trading_end: str = "11:00:00"
    flatten_before_window_end_seconds: int = 60
    post_window_position_check_seconds: int = 30
    forced_flatten_cooldown_seconds: int = 3600
    lock_on_entry_order: bool = True
    manage_account_positions: bool = False
    live_trading_enabled: bool = False
    dry_run: bool = False
    log_root: str = "runs"

    def __post_init__(self) -> None:
        for attr in ("trading_start", "trading_end"):
            val = getattr(self, attr)
            try:
                datetime.strptime(val, "%H:%M:%S")
            except ValueError:
                raise ValueError(f"runtime.{attr} must be HH:MM:SS, got: {val!r}")


@dataclass
class AppConfig:
    ib: IBConfig = field(default_factory=IBConfig)
    strategy_files: StrategyFiles = field(default_factory=StrategyFiles)
    runtime: RuntimeConfig = field(default_factory=RuntimeConfig)


def load_config(path: str | Path | None, overrides: list[str] | None = None) -> AppConfig:
    config = AppConfig()
    if path:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        config = AppConfig(
            ib=_from_dict(IBConfig, data.get("ib", {}), path),
            strategy_files=_from_dict(StrategyFiles, data.get("strategy_files", {}), path),
            runtime=_from_dict(RuntimeConfig, data.get("runtime", {}), path),
        )
    apply_overrides(config, overrides or [])
    return config


def _from_dict(cls: type, data: dict, source: str | Path) -> Any:
    known = {f.name for f in fields(cls)}
    unknown = set(data) - known
    if unknown:
        raise ValueError(f"{cls.__name__} in {source}: unknown keys: {', '.join(sorted(unknown))}")
    return cls(**data)


def apply_overrides(config: AppConfig, overrides: list[str]) -> None:
    for item in overrides:
        if "=" not in item or "." not in item.split("=", 1)[0]:
            raise ValueError(f"override must look like section.field=value: {item}")
        key, raw = item.split("=", 1)
        section_name, field_name = key.split(".", 1)
        section = getattr(config, section_name, None)
        if section is None or not is_dataclass(section):
            raise ValueError(f"unknown config section: {section_name}")
        names = {field.name for field in fields(section)}
        if field_name not in names:
            raise ValueError(f"unknown config field: {key}")
        current = getattr(section, field_name)
        setattr(section, field_name, _parse(raw, current))


def _parse(raw: str, current: Any) -> Any:
    if isinstance(current, bool):
        return raw.lower() in {"1", "true", "yes", "on"}
    if isinstance(current, int) and not isinstance(current, bool):
        return int(raw)
    if isinstance(current, float):
        return float(raw)
    if isinstance(current, list):
        return json.loads(raw) if raw.startswith("[") else [item.strip() for item in raw.split(",") if item.strip()]
    return raw
