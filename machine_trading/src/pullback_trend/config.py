from __future__ import annotations

import json
from dataclasses import dataclass, field, fields, is_dataclass
from pathlib import Path
from typing import Any


@dataclass
class StrategyConfig:
    symbols: list[str] = field(default_factory=lambda: ["NVDA", "TSLA", "MU", "META", "AVGO", "MSFT", "QQQ", "TQQQ"])
    market_symbol: str = "QQQ"
    trade_windows: list[list[str]] = field(default_factory=lambda: [["09:35", "11:30"], ["14:00", "15:30"]])
    regular_session_start: str = "09:30"
    regular_session_end: str = "16:00"
    max_hold_minutes: int = 30
    no_progress_minutes: int = 8
    min_score: float = 0.72
    min_rvol: float = 1.5
    max_spread_bps: float = 8.0
    min_volume: int = 50_000
    pullback_lookback: int = 6
    stabilization_lookback: int = 3
    breakout_lookback: int = 3
    support_tolerance_bps: float = 16.0
    prior_breakout_lookback: int = 20
    atr_compression_floor_bps: float = 5.0
    min_market_rvol: float = 0.8
    use_l2: bool = False
    min_l2_total_size: float = 100.0
    min_l2_imbalance: float = -0.10


@dataclass
class RiskConfig:
    account_size: float = 50_000.0
    risk_per_trade_pct: float = 0.0035
    max_daily_loss_pct: float = 0.01
    max_trades_per_day: int = 3
    max_consecutive_losses: int = 2
    max_open_positions: int = 1
    max_position_notional: float = 45_000.0
    min_stop_bps: float = 8.0
    max_stop_bps: float = 120.0
    atr_stop_multiple: float = 0.35
    entry_slippage_bps: float = 2.0
    commission_per_share: float = 0.005

    @property
    def risk_dollars(self) -> float:
        return self.account_size * self.risk_per_trade_pct

    @property
    def max_daily_loss_dollars(self) -> float:
        return self.account_size * self.max_daily_loss_pct


@dataclass
class ExecutionConfig:
    limit_offset_bps: float = 2.0
    entry_stale_seconds: int = 20
    tp1_r: float = 0.5
    tp2_r: float = 1.2
    tp1_fraction: float = 0.5
    breakeven_offset_bps: float = 1.0
    live_trading_enabled: bool = False


@dataclass
class IBConfig:
    host: str = "127.0.0.1"
    port: int = 7497
    client_id: int = 21
    exchange: str = "SMART"
    currency: str = "USD"
    market_data_type: int = 1
    request_streaming_quotes: bool = False
    market_depth_rows: int = 5
    smart_depth: bool = False
    heartbeat_seconds: int = 30
    historical_duration: str = "2 D"
    bar_size: str = "1 min"
    use_rth: bool = True


@dataclass
class LoggingConfig:
    base_dir: str = "runs"
    run_id: str | None = None


@dataclass
class AppConfig:
    strategy: StrategyConfig = field(default_factory=StrategyConfig)
    risk: RiskConfig = field(default_factory=RiskConfig)
    execution: ExecutionConfig = field(default_factory=ExecutionConfig)
    ib: IBConfig = field(default_factory=IBConfig)
    logging: LoggingConfig = field(default_factory=LoggingConfig)


def load_config(path: str | Path) -> AppConfig:
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    return AppConfig(
        strategy=_coerce(StrategyConfig, data.get("strategy", {})),
        risk=_coerce(RiskConfig, data.get("risk", {})),
        execution=_coerce(ExecutionConfig, data.get("execution", {})),
        ib=_coerce(IBConfig, data.get("ib", {})),
        logging=_coerce(LoggingConfig, data.get("logging", {})),
    )


def _coerce(cls: type, data: dict) -> Any:
    valid = {f.name for f in fields(cls)}
    return cls(**{k: v for k, v in data.items() if k in valid})


def load_config_with_overrides(path: str | Path, overrides: list[str] | None = None) -> AppConfig:
    config = load_config(path)
    for override in overrides or []:
        section_name, field_name, raw = _split_override(override)
        section = getattr(config, section_name, None)
        if section is None or not is_dataclass(section):
            raise ValueError(f"Unknown config section {section_name!r}")
        names = {field.name for field in fields(section)}
        if field_name not in names:
            raise ValueError(f"Unknown config field {section_name}.{field_name}")
        current = getattr(section, field_name)
        setattr(section, field_name, _parse_value(raw, current))
    return config


def _split_override(value: str) -> tuple[str, str, str]:
    if "=" not in value or "." not in value.split("=", 1)[0]:
        raise ValueError("Overrides must look like section.field=value")
    key, raw = value.split("=", 1)
    section, field_name = key.split(".", 1)
    return section.strip(), field_name.strip(), raw.strip()


def _parse_value(raw: str, current: Any) -> Any:
    if isinstance(current, bool):
        return raw.lower() in {"1", "true", "yes", "on"}
    if isinstance(current, int) and not isinstance(current, bool):
        return int(raw)
    if isinstance(current, float):
        return float(raw)
    if isinstance(current, list):
        return json.loads(raw) if raw.startswith("[") else [item.strip() for item in raw.split(",") if item.strip()]
    if raw.lower() in {"none", "null"}:
        return None
    return raw
