"""
MACD-based signal utilities.
Used by scanner.py to compute multi-timeframe signals for each symbol.
"""

import pandas as pd


def macd_state(df: pd.DataFrame) -> tuple[int, bool]:
    """
    Compute MACD (12/26/9) on a OHLC DataFrame.

    Returns:
        crossover  : +1 = fresh bullish cross, -1 = fresh bearish cross, 0 = no cross
        is_bullish : True if MACD line is currently above signal line
    """
    if df is None or len(df) < 26:
        return 0, False

    d = df.copy()
    d["ema12"] = d["close"].ewm(span=12, adjust=False).mean()
    d["ema26"] = d["close"].ewm(span=26, adjust=False).mean()
    d["macd"]  = d["ema12"] - d["ema26"]
    d["msig"]  = d["macd"].ewm(span=9, adjust=False).mean()

    mc, sc   = float(d["macd"].iloc[-1]), float(d["msig"].iloc[-1])
    mp, sp   = float(d["macd"].iloc[-2]), float(d["msig"].iloc[-2])

    bullish = mc > sc
    if mc > sc and mp <= sp:
        cross = 1
    elif mc < sc and mp >= sp:
        cross = -1
    else:
        cross = 0

    return cross, bullish


def macd_histogram(df: pd.DataFrame) -> float:
    """Latest MACD histogram value (macd - signal), normalised by price."""
    if df is None or len(df) < 26:
        return 0.0
    d = df.copy()
    d["ema12"] = d["close"].ewm(span=12, adjust=False).mean()
    d["ema26"] = d["close"].ewm(span=26, adjust=False).mean()
    d["macd"]  = d["ema12"] - d["ema26"]
    d["msig"]  = d["macd"].ewm(span=9, adjust=False).mean()
    hist  = float(d["macd"].iloc[-1]) - float(d["msig"].iloc[-1])
    price = float(d["close"].iloc[-1])
    return abs(hist / price) * 100 if price else 0.0   # as % of price
