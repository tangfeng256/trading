from __future__ import annotations

import pandas as pd


def ema(values: pd.Series, span: int) -> pd.Series:
    return values.ewm(span=span, adjust=False, min_periods=1).mean()


def vwap(frame: pd.DataFrame) -> pd.Series:
    typical = (frame["high"] + frame["low"] + frame["close"]) / 3.0
    dollars = typical * frame["volume"]
    volume = frame["volume"].replace(0, pd.NA).cumsum()
    return dollars.cumsum() / volume


def atr(frame: pd.DataFrame, period: int = 14) -> pd.Series:
    prev_close = frame["close"].shift(1)
    ranges = pd.concat(
        [
            frame["high"] - frame["low"],
            (frame["high"] - prev_close).abs(),
            (frame["low"] - prev_close).abs(),
        ],
        axis=1,
    ).max(axis=1)
    return ranges.rolling(period, min_periods=1).mean()


def rvol(frame: pd.DataFrame, period: int = 20) -> pd.Series:
    avg = frame["volume"].rolling(period, min_periods=1).mean().shift(1)
    return frame["volume"] / avg.replace(0, pd.NA)


def add_indicators(frame: pd.DataFrame) -> pd.DataFrame:
    out = frame.copy()
    out["ema9"] = ema(out["close"], 9)
    out["ema20"] = ema(out["close"], 20)
    out["ema50"] = ema(out["close"], 50)
    out["vwap_calc"] = out["vwap"] if "vwap" in out and out["vwap"].notna().any() else vwap(out)
    out["atr14"] = atr(out, 14)
    out["rvol"] = rvol(out, 20).fillna(1.0)
    out["rolling_high_20"] = out["high"].rolling(20, min_periods=1).max()
    out["rolling_low_20"] = out["low"].rolling(20, min_periods=1).min()
    return out
