"""
Shared yfinance data layer with in-process caching.

New in v2:
  - batch_scan_populate() fetches all 60 symbols + SPY in 2 API calls
  - get_spy_regime()       → BULLISH / NEUTRAL / BEARISH based on 50-day SMA
  - get_relative_strength()→ 20-day return vs SPY (positive = outperforming)
  - get_earnings_flag()    → True if earnings within ~2 trading days
  - refresh_earnings_cache() → called in a background thread every 4 hours
"""

import time
import threading
from datetime import datetime

import pandas as pd
import yfinance as yf

# ── Per-symbol OHLC cache ─────────────────────────────────────────────────
# symbol → {"daily": df, "hourly": df, "ts": float}
_cache: dict[str, dict] = {}
_CACHE_TTL = 240   # 4 minutes — scanner runs every 5 min
_cache_lock = threading.Lock()

# ── Earnings cache ────────────────────────────────────────────────────────
_earnings_cache: dict[str, bool] = {}


# ── Normalisation helpers ─────────────────────────────────────────────────

def _normalise(df: pd.DataFrame, symbol: str) -> pd.DataFrame | None:
    """Flatten MultiIndex columns (single-ticker download), lowercase, rename."""
    if df is None or df.empty:
        return None
    if isinstance(df.columns, pd.MultiIndex):
        df = df.xs(symbol, axis=1, level=1) \
            if symbol in df.columns.get_level_values(1) \
            else df.droplevel(1, axis=1)
    return _normalise_flat(df)


def _normalise_flat(df: pd.DataFrame) -> pd.DataFrame | None:
    """Normalise a flat (already-extracted) per-symbol DataFrame."""
    if df is None or df.empty:
        return None
    df = df.copy()
    df.columns = [str(c).lower().replace(" ", "_") for c in df.columns]
    df = df.reset_index()
    for col in df.columns:
        if col.lower() in ("date", "datetime", "index"):
            df = df.rename(columns={col: "datetime"})
            break
    df["datetime"] = pd.to_datetime(df["datetime"])
    if df["datetime"].dt.tz is not None:
        df["datetime"] = df["datetime"].dt.tz_convert(None)
    df = df.sort_values("datetime").reset_index(drop=True)
    required = {"open", "high", "low", "close", "volume"}
    if not required.issubset(set(df.columns)):
        return None
    return df[["datetime", "open", "high", "low", "close", "volume"]]


# ── Batch prefetch ────────────────────────────────────────────────────────

def batch_scan_populate(symbols: list[str]) -> None:
    """
    Download daily + hourly OHLC for all symbols + SPY in two API calls,
    then populate the cache.  Subsequent get_ohlc() calls use the cache.
    """
    now = time.time()
    all_syms = list(dict.fromkeys(symbols + ["SPY"]))  # deduplicate, keep order

    for interval, key in [("1d", "daily"), ("1h", "hourly")]:
        try:
            raw = yf.download(
                tickers=all_syms,
                period="60d",
                interval=interval,
                progress=False,
                auto_adjust=True,
                group_by="ticker",
                threads=True,
            )
            if raw is None or raw.empty:
                continue

            top_level = list(raw.columns.get_level_values(0).unique()) \
                if isinstance(raw.columns, pd.MultiIndex) else []

            with _cache_lock:
                for sym in all_syms:
                    try:
                        if isinstance(raw.columns, pd.MultiIndex) and sym in top_level:
                            df_sym = raw[sym].copy()
                            df_norm = _normalise_flat(df_sym)
                        else:
                            df_norm = None

                        if sym not in _cache:
                            _cache[sym] = {"daily": None, "hourly": None, "ts": 0.0}
                        _cache[sym][key] = df_norm
                        _cache[sym]["ts"] = now
                    except Exception as e:
                        print(f"  [market_data] extract {sym} {interval}: {e}")

        except Exception as e:
            print(f"  [market_data] batch {interval} failed: {e}")


# ── Individual fetch (cache fallback) ────────────────────────────────────

def fetch(symbol: str, period: str, interval: str) -> pd.DataFrame | None:
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
    now = time.time()
    with _cache_lock:
        cached = _cache.get(symbol)
    if cached and now - cached["ts"] < _CACHE_TTL:
        return cached["daily"], cached["hourly"]

    df_d = fetch(symbol, period="60d", interval="1d")
    df_h = fetch(symbol, period="60d", interval="1h")
    with _cache_lock:
        _cache[symbol] = {"daily": df_d, "hourly": df_h, "ts": now}
    return df_d, df_h


def get_price(symbol: str) -> float | None:
    try:
        t = yf.Ticker(symbol)
        p = t.fast_info.last_price
        if p and not pd.isna(p):
            return float(p)
    except Exception:
        pass
    with _cache_lock:
        cached = _cache.get(symbol)
    if cached and cached["daily"] is not None:
        return float(cached["daily"]["close"].iloc[-1])
    return None


def invalidate(symbol: str) -> None:
    with _cache_lock:
        _cache.pop(symbol, None)


# ── SPY regime ────────────────────────────────────────────────────────────

def _get_spy_daily() -> pd.DataFrame | None:
    with _cache_lock:
        cached = _cache.get("SPY")
    if cached and cached["daily"] is not None:
        return cached["daily"]
    df = fetch("SPY", period="90d", interval="1d")
    with _cache_lock:
        if "SPY" not in _cache:
            _cache["SPY"] = {"daily": None, "hourly": None, "ts": 0.0}
        _cache["SPY"]["daily"] = df
        _cache["SPY"]["ts"] = time.time()
    return df


def get_spy_regime() -> dict:
    """Return BULLISH / NEUTRAL / BEARISH based on SPY vs its 50-day SMA."""
    df = _get_spy_daily()
    if df is None or len(df) < 50:
        return {"regime": "NEUTRAL", "spy_price": 0.0, "spy_50sma": 0.0}
    sma50 = float(df["close"].rolling(50).mean().iloc[-1])
    price = float(df["close"].iloc[-1])
    if price > sma50 * 1.005:
        regime = "BULLISH"
    elif price < sma50 * 0.995:
        regime = "BEARISH"
    else:
        regime = "NEUTRAL"
    return {
        "regime":    regime,
        "spy_price": round(price, 2),
        "spy_50sma": round(sma50, 2),
    }


# ── Relative strength ─────────────────────────────────────────────────────

def get_relative_strength(symbol: str) -> float:
    """20-day return of symbol minus SPY 20-day return. Positive = outperforming."""
    df_spy = _get_spy_daily()
    with _cache_lock:
        cached = _cache.get(symbol)
    df_sym = cached["daily"] if cached else None
    if df_spy is None or df_sym is None:
        return 0.0
    if len(df_spy) < 21 or len(df_sym) < 21:
        return 0.0
    spy_ret = (df_spy["close"].iloc[-1] / df_spy["close"].iloc[-21] - 1) * 100
    sym_ret = (df_sym["close"].iloc[-1] / df_sym["close"].iloc[-21] - 1) * 100
    return round(float(sym_ret - spy_ret), 2)


# ── Earnings cache ────────────────────────────────────────────────────────

def get_earnings_flag(symbol: str) -> bool:
    """Return cached earnings-soon flag. False until first refresh completes."""
    return _earnings_cache.get(symbol, False)


def _check_earnings(symbol: str) -> bool:
    """Check yfinance calendar for earnings within 4 calendar days (~2 trading days)."""
    try:
        t = yf.Ticker(symbol)
        cal = t.calendar
        if cal is None:
            return False
        today = datetime.now().date()
        dates: list = []
        if isinstance(cal, pd.DataFrame):
            if "Earnings Date" in cal.index:
                row = cal.loc["Earnings Date"]
                dates = list(row.values) if hasattr(row, "values") else [row]
        elif isinstance(cal, dict):
            val = cal.get("Earnings Date", [])
            dates = list(val) if hasattr(val, "__iter__") and not isinstance(val, str) else [val]
        for d in dates:
            try:
                diff = (pd.Timestamp(d).date() - today).days
                if 0 <= diff <= 4:
                    return True
            except Exception:
                pass
    except Exception:
        pass
    return False


def refresh_earnings_cache(symbols: list[str]) -> None:
    """Refresh earnings flags for all symbols. Call from a background thread."""
    print(f"  [market_data] refreshing earnings cache for {len(symbols)} symbols…")
    new_cache: dict[str, bool] = {}
    for sym in symbols:
        new_cache[sym] = _check_earnings(sym)
        time.sleep(0.1)
    _earnings_cache.update(new_cache)
    flagged = [s for s, v in new_cache.items() if v]
    print(f"  [market_data] earnings cache done — upcoming: {flagged or 'none'}")
