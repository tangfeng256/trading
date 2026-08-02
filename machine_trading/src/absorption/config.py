from __future__ import annotations

import json
from dataclasses import dataclass, field
from dataclasses import replace
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class IBConfig:
    host: str = "127.0.0.1"
    port: int = 7497
    client_id: int = 17
    connect_timeout_sec: int = 15


@dataclass(frozen=True)
class MarketConfig:
    exchange: str = "SMART"
    currency: str = "USD"
    primary_exchange: str = "NASDAQ"
    depth_rows: int = 5
    tick_size: float = 0.01


@dataclass(frozen=True)
class StrategyConfig:
    trade_start: str = "09:30:00"
    trade_end: str = "11:30:00"
    feature_interval_ms: int = 500
    min_absorption_score: float = 0.70
    min_exhaustion_score: float = 0.60
    min_trigger_score: float = 0.65
    min_breakout_ticks: int = 2
    max_spread_bps: float = 6.0
    absorption_window_sec: int = 10
    exhaustion_window_sec: int = 5
    trigger_window_sec: int = 3
    max_hold_minutes: int = 30


@dataclass(frozen=True)
class RiskConfig:
    account_equity: float = 50_000.0
    risk_per_trade_pct: float = 0.0025
    max_daily_loss_pct: float = 0.01
    max_notional: float = 25_000.0
    max_trades_per_day: int = 3
    min_stop_bps: float = 5.0
    max_stop_bps: float = 60.0


@dataclass(frozen=True)
class ExecutionConfig:
    entry_order_type: str = "LMT"
    entry_price_offset_ticks: int = 1
    stale_entry_seconds: int = 8
    tp1_r_multiple: float = 1.0
    tp1_fraction: float = 0.5
    tp2_r_multiple: float = 2.0
    move_stop_to_breakeven_after_tp1: bool = True
    protective_order_delay_seconds: float = 1.0


@dataclass(frozen=True)
class LoggingConfig:
    root: str = "runs"
    log_depth: bool = True
    log_tape: bool = True
    log_features: bool = True


@dataclass(frozen=True)
class AppConfig:
    ib: IBConfig = field(default_factory=IBConfig)
    symbols: list[str] = field(default_factory=lambda: ["NVDA", "TSLA", "MU", "TQQQ"])
    market: MarketConfig = field(default_factory=MarketConfig)
    strategy: StrategyConfig = field(default_factory=StrategyConfig)
    risk: RiskConfig = field(default_factory=RiskConfig)
    execution: ExecutionConfig = field(default_factory=ExecutionConfig)
    logging: LoggingConfig = field(default_factory=LoggingConfig)


def _coerce(cls: type, payload: dict[str, Any]) -> Any:
    valid = cls.__dataclass_fields__.keys()  # type: ignore[attr-defined]
    return cls(**{k: v for k, v in payload.items() if k in valid})


def load_config(path: str | Path | None) -> AppConfig:
    if path is None:
        return AppConfig()
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    return AppConfig(
        ib=_coerce(IBConfig, data.get("ib", {})),
        symbols=list(data.get("symbols", AppConfig().symbols)),
        market=_coerce(MarketConfig, data.get("market", {})),
        strategy=_coerce(StrategyConfig, data.get("strategy", {})),
        risk=_coerce(RiskConfig, data.get("risk", {})),
        execution=_coerce(ExecutionConfig, data.get("execution", {})),
        logging=_coerce(LoggingConfig, data.get("logging", {})),
    )


def apply_overrides(config: AppConfig, overrides: list[str] | None) -> AppConfig:
    if not overrides:
        return config
    current = config
    for item in overrides:
        if "=" not in item:
            raise ValueError(f"override must use key=value format: {item}")
        key, raw_value = item.split("=", 1)
        section, _, field_name = key.partition(".")
        if not section or not field_name:
            raise ValueError(f"override must use section.field=value format: {item}")
        section_obj = getattr(current, section, None)
        if section_obj is None or not hasattr(section_obj, "__dataclass_fields__"):
            raise ValueError(f"unknown config section: {section}")
        if field_name not in section_obj.__dataclass_fields__:
            raise ValueError(f"unknown config field: {key}")
        value = _parse_override_value(raw_value)
        updated_section = replace(section_obj, **{field_name: value})
        current = replace(current, **{section: updated_section})
    return current


def _parse_override_value(value: str) -> Any:
    lowered = value.lower()
    if lowered == "true":
        return True
    if lowered == "false":
        return False
    try:
        return int(value)
    except ValueError:
        pass
    try:
        return float(value)
    except ValueError:
        return value
