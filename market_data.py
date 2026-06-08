"""
Shared yfinance data layer with in-process caching.

Granularity notes (yfinance free):
  interval="1d"  period up to "60d"  → daily candles
  interval="1h"  period up to "730d" → hourly candles (60-day max in practice)
"""

import time
import pandas as pd
import yfinance as yf

# Per-symbol cache: {symbol -> {"daily": df, "hourly": df, "ts": float}}
_cache: dict[str, dict] = {}
_CACHE_TTL = 240   # 4 minutes — scanner runs every 5 min


def _normalise(df: pd.DataFrame, symbol: str) -> pd.DataFrame | None:
    """Flatten MultiIndex columns, lowercase, rename Date/Datetime → datetime."""
    if df is None or df.empty:
        return None
    if isinstance(df.columns, pd.MultiIndex):
        # yfinance 0.2+ returns (metric, ticker) pairs
        df = df.xs(symbol, axis=1, level=1) if symbol in df.columns.get_level_values(1) \
             else df.droplevel(1, axis=1)
    df.columns = [str(c).lower().replace(" ", "_") for c in df.columns]
    df = df.reset_index()
    for col in df.columns:
        if col.lower() in ("date", "datetime", "index"):
            df = df.rename(columns={col: "datetime"})
            break
    df["datetime"] = pd.to_datetime(df["datetime"])
    df = df.sort_values("datetime").reset_index(drop=True)
    required = {"open", "high", "low", "close", "volume"}
    if not required.issubset(set(df.columns)):
        return None
    return df[["datetime", "open", "high", "low", "close", "volume"]]


def fetch(symbol: str, period: str, interval: str) -> pd.DataFrame | None:
    """Download data from Yahoo Finance with silent error handling."""
    try:
        df = yf.download(
            symbol, period=period, interval=interval,
            progress=False, auto_adjust=True,
        )
        return _normalise(df, symbol)
    except Exception as e:
        print(f"  [market_data] {symbol} {interval}: {e}")
        return None


def get_ohlc(symbol: str) -> tuple[pd.DataFrame | None, pd.DataFrame | None]:
    """
    Returns (df_daily, df_hourly) for a symbol, using cache.
    Fetches fresh data if cache is older than CACHE_TTL seconds.
    """
    now = time.time()
    cached = _cache.get(symbol)
    if cached and now - cached["ts"] < _CACHE_TTL:
        return cached["daily"], cached["hourly"]

    df_d = fetch(symbol, period="60d",  interval="1d")
    df_h = fetch(symbol, period="60d",  interval="1h")

    _cache[symbol] = {"daily": df_d, "hourly": df_h, "ts": now}
    return df_d, df_h


def get_price(symbol: str) -> float | None:
    """Latest price via yfinance fast_info, falls back to last daily close."""
    try:
        t = yf.Ticker(symbol)
        p = t.fast_info.last_price
        if p and not pd.isna(p):
            return float(p)
    except Exception:
        pass
    # fallback: use cached daily last close
    cached = _cache.get(symbol)
    if cached and cached["daily"] is not None:
        return float(cached["daily"]["close"].iloc[-1])
    return None


def invalidate(symbol: str):
    _cache.pop(symbol, None)
