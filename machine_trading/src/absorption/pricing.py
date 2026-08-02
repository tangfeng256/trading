from __future__ import annotations

from decimal import Decimal, ROUND_CEILING, ROUND_FLOOR, ROUND_HALF_UP


def round_to_tick(price: float, tick_size: float, mode: str = "nearest") -> float:
    tick = Decimal(str(tick_size))
    if tick <= 0:
        return float(price)
    value = Decimal(str(price))
    rounding = {
        "down": ROUND_FLOOR,
        "up": ROUND_CEILING,
        "nearest": ROUND_HALF_UP,
    }.get(mode)
    if rounding is None:
        raise ValueError(f"unknown tick rounding mode: {mode}")
    ticks = (value / tick).to_integral_value(rounding=rounding)
    return float(ticks * tick)
