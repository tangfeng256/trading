from __future__ import annotations

import argparse
import threading
from dataclasses import fields, is_dataclass
from datetime import datetime, timezone
from typing import Iterable

from orm_ignition.backtest import run_backtest
from orm_ignition.config import AppConfig, load_config_with_overrides
from orm_ignition.execution_manager import ExecutionManager
from orm_ignition.ib_client import IBClient
from orm_ignition.logger import AuditLogger
from orm_ignition.market_state import MarketState
from orm_ignition.replay import run_replay
from orm_ignition.risk_manager import RiskManager
from orm_ignition.scanner import Scanner
from orm_ignition.signal_engine import SignalEngine


def main() -> int:
    parser = argparse.ArgumentParser(description="Opening Range Momentum Ignition")
    sub = parser.add_subparsers(dest="cmd", required=True)
    for name in ("live", "paper"):
        p = sub.add_parser(name)
        p.add_argument("--config", required=True)
        _add_override_args(p)
        _add_config_field_args(p)
    p_backtest = sub.add_parser("backtest")
    p_backtest.add_argument("--config", required=True)
    p_backtest.add_argument("--bars", required=True)
    _add_override_args(p_backtest)
    _add_config_field_args(p_backtest)
    p_replay = sub.add_parser("replay")
    p_replay.add_argument("--run-dir", required=True)
    p_replay.add_argument("--chart", action="store_true")
    args = parser.parse_args()

    if args.cmd == "backtest":
        config = _load_config_or_exit(parser, args.config, _config_overrides(args))
        out = run_backtest(config, args.bars)
        print(f"Backtest complete: {out}")
        return 0
    if args.cmd == "replay":
        out = run_replay(args.run_dir, chart=args.chart)
        print(f"Replay timeline: {out}")
        return 0

    config = _load_config_or_exit(parser, args.config, _config_overrides(args))
    paper = args.cmd == "paper"

    logger = AuditLogger(config.logging.base_dir, config.logging.run_id, config.logging.write_book_snapshots)
    market = MarketState(config.strategy.symbols, config.strategy.or_start, config.strategy.or_end)
    risk = RiskManager(config.risk)
    ib = IBClient(config.ib, config.strategy.symbols, paper=paper)
    execution = ExecutionManager(risk, config.risk, config.strategy, logger, broker=ib)
    engine = SignalEngine(config.strategy, Scanner(config.strategy))

    def on_bar(bar):
        market.on_bar(bar)
        logger.bar(bar)
        state = market.state(bar.symbol)
        signal, decision = engine.evaluate(state, list(market.symbols.values()))
        logger.decision(bar.symbol, "signal", signal is not None, str(decision.get("reason", "")), decision)
        if signal:
            logger.signal(signal)
            execution.on_signal(signal, state.quote)
        execution.reconcile(bar.timestamp)

    ib.quote_callback = market.on_quote
    ib.book_callback = lambda book: (market.on_book(book), logger.book(book))
    ib.bar_callback = on_bar
    ib.error_callback = lambda msg: logger.event("ib_error", {"message": msg})
    ib.fill_callback = execution.on_fill
    ib.connect()
    ib.subscribe_market_data()
    ib.subscribe_bars()
    if config.strategy.use_l2:
        ib.subscribe_depth()
    mode = "Paper" if paper else "Live"
    print(f"{mode} trading started. Run dir: {logger.run_dir}")
    stop_heartbeat = _start_heartbeat(config.strategy.symbols)
    try:
        ib.run()
    finally:
        stop_heartbeat.set()
        ib.disconnect()
    return 0


def _start_heartbeat(symbols: Iterable[str], interval_seconds: float = 30.0) -> threading.Event:
    stop_event = threading.Event()
    symbol_list = tuple(symbols)

    def loop() -> None:
        while not stop_event.wait(interval_seconds):
            print(_heartbeat_message(symbol_list), flush=True)

    threading.Thread(target=loop, name="orm-heartbeat", daemon=True).start()
    return stop_event


def _heartbeat_message(symbols: Iterable[str]) -> str:
    timestamp = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
    symbol_list = ", ".join(symbols)
    return f"[{timestamp}] Heartbeat: monitoring {symbol_list}; ready to trade."


def _add_override_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--set",
        action="append",
        default=[],
        metavar="SECTION.FIELD=VALUE",
        help="Override a config value. Can be repeated, for example --set strategy.symbols=NVDA,MU.",
    )


def _add_config_field_args(parser: argparse.ArgumentParser) -> None:
    for field_name, section_name in _config_field_sections().items():
        option = f"--{field_name}"
        aliases = [option]
        dashed = option.replace("_", "-")
        if dashed != option:
            aliases.append(dashed)
        kwargs = {
            "dest": _config_arg_dest(section_name, field_name),
            "default": None,
            "metavar": "VALUE",
            "help": f"Override config value {section_name}.{field_name}.",
        }
        if isinstance(getattr(getattr(AppConfig(), section_name), field_name), bool):
            kwargs["nargs"] = "?"
            kwargs["const"] = "true"
        parser.add_argument(*aliases, **kwargs)


def _config_overrides(args: argparse.Namespace) -> list[str]:
    overrides = list(args.set)
    for field_name, section_name in _config_field_sections().items():
        value = getattr(args, _config_arg_dest(section_name, field_name), None)
        if value is not None:
            overrides.append(f"{section_name}.{field_name}={value}")
    return overrides


def _config_field_sections() -> dict[str, str]:
    config = AppConfig()
    sections: dict[str, str] = {}
    for section_field in fields(config):
        section_name = section_field.name
        section = getattr(config, section_name)
        if not is_dataclass(section):
            continue
        for config_field in fields(section):
            if config_field.name in sections:
                raise RuntimeError(f"Ambiguous config CLI field: {config_field.name}")
            sections[config_field.name] = section_name
    return sections


def _config_arg_dest(section_name: str, field_name: str) -> str:
    return f"config__{section_name}__{field_name}"


def _load_config_or_exit(parser: argparse.ArgumentParser, path: str, overrides: list[str]):
    try:
        return load_config_with_overrides(path, overrides)
    except ValueError as exc:
        parser.error(str(exc))


if __name__ == "__main__":
    raise SystemExit(main())
