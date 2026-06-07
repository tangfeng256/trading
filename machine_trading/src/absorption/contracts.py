from __future__ import annotations

from .config import MarketConfig


def stock_contract(symbol: str, market: MarketConfig):
    try:
        from ib_insync import Stock
    except ImportError as exc:
        raise RuntimeError("ib_insync is required for live IBKR contracts") from exc
    return Stock(symbol, market.exchange, market.currency, primaryExchange=market.primary_exchange)
