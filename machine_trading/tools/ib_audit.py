from __future__ import annotations

import argparse
import csv
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def main() -> int:
    parser = argparse.ArgumentParser(description="Read-only IBKR audit for order status, executions, and historical ticks.")
    parser.add_argument("--run-dir", default="runs/20260611_132033")
    parser.add_argument("--symbol", default="TSLA")
    parser.add_argument("--order-ref", default="tp1-8")
    parser.add_argument("--target-price", type=float, default=382.9106)
    parser.add_argument("--start", default="20260611 09:36:00 US/Eastern")
    parser.add_argument("--end", default="20260611 09:50:00 US/Eastern")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=7497)
    parser.add_argument("--client-id", type=int, default=99)
    parser.add_argument("--exchange", default="SMART")
    parser.add_argument("--currency", default="USD")
    parser.add_argument("--primary-exchange", default="NASDAQ")
    parser.add_argument("--all-orders", action="store_true", help="Include non-API TWS orders in completed-order request.")
    args = parser.parse_args()

    try:
        from ib_insync import ExecutionFilter, IB, Stock
    except ImportError as exc:
        raise SystemExit("ib_insync is not installed in this Python environment") from exc

    out_dir = Path(args.run_dir) / "ib_audit"
    out_dir.mkdir(parents=True, exist_ok=True)

    ib = IB()
    summary: dict[str, Any] = {
        "symbol": args.symbol,
        "order_ref": args.order_ref,
        "target_price": args.target_price,
        "start": args.start,
        "end": args.end,
        "host": args.host,
        "port": args.port,
        "client_id": args.client_id,
        "generated_at": datetime.now(timezone.utc).isoformat(),
    }
    try:
        ib.connect(args.host, args.port, clientId=args.client_id, timeout=8)
        contract = Stock(args.symbol, args.exchange, args.currency, primaryExchange=args.primary_exchange)
        qualified = ib.qualifyContracts(contract)
        if qualified:
            contract = qualified[0]

        completed = _safe_call(lambda: ib.reqCompletedOrders(not args.all_orders), [])
        open_trades = _safe_call(ib.reqOpenOrders, [])
        exec_filter = ExecutionFilter()
        exec_filter.symbol = args.symbol
        exec_filter.secType = "STK"
        executions = _safe_call(lambda: ib.reqExecutions(exec_filter), [])
        trade_ticks = _safe_call(
            lambda: ib.reqHistoricalTicks(
                contract,
                startDateTime=args.start,
                endDateTime=args.end,
                numberOfTicks=1000,
                whatToShow="TRADES",
                useRth=True,
                ignoreSize=False,
                miscOptions=[],
            ),
            [],
        )
        bid_ask_ticks = _safe_call(
            lambda: ib.reqHistoricalTicks(
                contract,
                startDateTime=args.start,
                endDateTime=args.end,
                numberOfTicks=1000,
                whatToShow="BID_ASK",
                useRth=True,
                ignoreSize=False,
                miscOptions=[],
            ),
            [],
        )

        _write_trades(out_dir / "completed_orders.csv", list(completed) + list(ib.trades()))
        _write_trades(out_dir / "open_orders.csv", open_trades)
        _write_executions(out_dir / "executions.csv", executions)
        _write_trade_ticks(out_dir / "historical_trades.csv", trade_ticks)
        _write_bid_ask_ticks(out_dir / "historical_bid_ask.csv", bid_ask_ticks)

        matching_orders = [
            _trade_row(trade)
            for trade in list(completed) + list(open_trades) + list(ib.trades())
            if _trade_row(trade).get("order_ref") == args.order_ref
        ]
        near_trade_ticks = [
            _tick_last_row(tick)
            for tick in trade_ticks
            if abs(float(getattr(tick, "price", 0.0) or 0.0) - args.target_price) <= 0.25
        ]
        crossing_trade_ticks = [
            _tick_last_row(tick)
            for tick in trade_ticks
            if float(getattr(tick, "price", 0.0) or 0.0) >= args.target_price
        ]
        crossing_bid_ticks = [
            _tick_bid_ask_row(tick)
            for tick in bid_ask_ticks
            if float(getattr(tick, "priceBid", 0.0) or 0.0) >= args.target_price
        ]
        _write_dicts(out_dir / "target_order_matches.csv", matching_orders)
        _write_dicts(out_dir / "ticks_near_target.csv", near_trade_ticks)
        _write_dicts(out_dir / "trade_ticks_at_or_above_target.csv", crossing_trade_ticks)
        _write_dicts(out_dir / "bid_ticks_at_or_above_target.csv", crossing_bid_ticks)

        summary.update(
            {
                "connected": True,
                "qualified_contract": _contract_row(contract),
                "completed_order_count": len(list(completed)),
                "open_order_count": len(list(open_trades)),
                "execution_count": len(list(executions)),
                "trade_tick_count": len(list(trade_ticks)),
                "bid_ask_tick_count": len(list(bid_ask_ticks)),
                "target_order_match_count": len(matching_orders),
                "trade_ticks_at_or_above_target_count": len(crossing_trade_ticks),
                "bid_ticks_at_or_above_target_count": len(crossing_bid_ticks),
                "first_trade_tick_at_or_above_target": crossing_trade_ticks[0] if crossing_trade_ticks else None,
                "first_bid_tick_at_or_above_target": crossing_bid_ticks[0] if crossing_bid_ticks else None,
                "output_dir": str(out_dir),
            }
        )
    except Exception as exc:
        summary.update({"connected": False, "error": type(exc).__name__, "message": str(exc)})
        (out_dir / "summary.json").write_text(json.dumps(summary, indent=2, default=str), encoding="utf-8")
        raise
    finally:
        if ib.isConnected():
            ib.disconnect()

    (out_dir / "summary.json").write_text(json.dumps(summary, indent=2, default=str), encoding="utf-8")
    print(json.dumps(summary, indent=2, default=str))
    return 0


def _safe_call(fn, default):
    try:
        return fn()
    except Exception:
        return default


def _write_trades(path: Path, trades: list[Any]) -> None:
    rows = [_trade_row(trade) for trade in trades]
    _write_dicts(path, rows)


def _trade_row(trade: Any) -> dict[str, Any]:
    order = getattr(trade, "order", None)
    status = getattr(trade, "orderStatus", None)
    contract = getattr(trade, "contract", None)
    return {
        "symbol": getattr(contract, "symbol", ""),
        "order_id": getattr(order, "orderId", ""),
        "perm_id": getattr(order, "permId", ""),
        "order_ref": getattr(order, "orderRef", ""),
        "action": getattr(order, "action", ""),
        "order_type": getattr(order, "orderType", ""),
        "total_quantity": getattr(order, "totalQuantity", ""),
        "limit_price": getattr(order, "lmtPrice", ""),
        "stop_price": getattr(order, "auxPrice", ""),
        "oca_group": getattr(order, "ocaGroup", ""),
        "status": getattr(status, "status", ""),
        "filled": getattr(status, "filled", ""),
        "remaining": getattr(status, "remaining", ""),
        "avg_fill_price": getattr(status, "avgFillPrice", ""),
        "last_fill_price": getattr(status, "lastFillPrice", ""),
        "why_held": getattr(status, "whyHeld", ""),
    }


def _write_executions(path: Path, fills: list[Any]) -> None:
    rows = []
    for fill in fills:
        execution = getattr(fill, "execution", None)
        contract = getattr(fill, "contract", None)
        report = getattr(fill, "commissionReport", None)
        rows.append(
            {
                "time": getattr(execution, "time", ""),
                "symbol": getattr(contract, "symbol", ""),
                "order_id": getattr(execution, "orderId", ""),
                "perm_id": getattr(execution, "permId", ""),
                "exec_id": getattr(execution, "execId", ""),
                "side": getattr(execution, "side", ""),
                "shares": getattr(execution, "shares", ""),
                "price": getattr(execution, "price", ""),
                "avg_price": getattr(execution, "avgPrice", ""),
                "exchange": getattr(execution, "exchange", ""),
                "commission": getattr(report, "commission", ""),
            }
        )
    _write_dicts(path, rows)


def _write_trade_ticks(path: Path, ticks: list[Any]) -> None:
    _write_dicts(path, [_tick_last_row(tick) for tick in ticks])


def _write_bid_ask_ticks(path: Path, ticks: list[Any]) -> None:
    _write_dicts(path, [_tick_bid_ask_row(tick) for tick in ticks])


def _tick_last_row(tick: Any) -> dict[str, Any]:
    return {
        "time": getattr(tick, "time", ""),
        "price": getattr(tick, "price", ""),
        "size": getattr(tick, "size", ""),
        "exchange": getattr(tick, "exchange", ""),
        "special_conditions": getattr(tick, "specialConditions", ""),
    }


def _tick_bid_ask_row(tick: Any) -> dict[str, Any]:
    return {
        "time": getattr(tick, "time", ""),
        "bid_price": getattr(tick, "priceBid", ""),
        "ask_price": getattr(tick, "priceAsk", ""),
        "bid_size": getattr(tick, "sizeBid", ""),
        "ask_size": getattr(tick, "sizeAsk", ""),
    }


def _contract_row(contract: Any) -> dict[str, Any]:
    return {
        "symbol": getattr(contract, "symbol", ""),
        "con_id": getattr(contract, "conId", ""),
        "exchange": getattr(contract, "exchange", ""),
        "primary_exchange": getattr(contract, "primaryExchange", ""),
        "currency": getattr(contract, "currency", ""),
    }


def _write_dicts(path: Path, rows: list[dict[str, Any]]) -> None:
    if rows:
        columns = list(dict.fromkeys(key for row in rows for key in row))
    else:
        columns = ["empty"]
        rows = []
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=columns, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


if __name__ == "__main__":
    raise SystemExit(main())
