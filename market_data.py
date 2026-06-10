"""
Shared yfinance data layer with in-process caching.

Fetch strategy:
  batch_scan_populate() submits all 60 + SPY as parallel individual downloads
  (ThreadPoolExecutor, 10 workers).  concurrent.futures.wait(timeout=45) sets
  the wall clock; executor.shutdown(wait=False) ensures we never block on a
  hung thread.  socket.setdefaulttimeout(15) makes the underlying network calls
  actually raise instead of hanging indefinitely.
"""

import concurrent.futures
import socket
import sys
import time
import threading
from datetime import datetime

import pandas as pd
import yfinance as yf

# Socket-level timeout — propagates into every yfinance network call.
# Without this, ThreadPoolExecutor.shutdown(wait=False) abandons the thread
# but the underlying socket blocks until the OS gives up (could be minutes).
socket.setdefaulttimeout(15)

_PARALLEL_WORKERS = 10
_BATCH_WALL_SECS  = 45   # give up on any unfinished parallel downloads

# ── Per-symbol OHLC cache ─────────────────────────────────────────────────
_cache: dict[str, dict] = {}
_CACHE_TTL  = 240
_cache_lock = threading.Lock()

# ── Earnings cache ────────────────────────────────────────────────────────
_earnings_cache: dict[str, bool] = {}


# ── Normalisation helpers ─────────────────────────────────────────────────

def _normalise(df: pd.DataFrame, symbol: str) -> pd.DataFrame | None:
    if df is None or df.empty:
        return None
    if isinstance(df.columns, pd.MultiIndex):
        df = df.xs(symbol, axis=1, level=1) \
            if symbol in df.columns.get_level_values(1) \
            else df.droplevel(1, axis=1)
    return _normalise_flat(df)


def _normalise_flat(df: pd.DataFrame) -> pd.DataFrame | None:
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
    if not {"open", "high", "low", "close", "volume"}.issubset(set(df.columns)):
        return None
    return df[["datetime", "open", "high", "low", "close", "volume"]]


# ── Core per-symbol fetch ─────────────────────────────────────────────────

def _fetch_simple(symbol: str, period: str, interval: str) -> pd.DataFrame | None:
    """
    Bare yf.download call — no ThreadPoolExecutor wrapper.
    socket.setdefaulttimeout(15) provides the real timeout; any exception
    (including socket timeout) is caught and returns None.
    """
    try:
        print(f"  [fetch] {symbol} {interval} {period}", flush=True)
        df = yf.download(
            symbol, period=period, interval=interval,
            progress=False, auto_adjust=True,
        )
        result = _normalise(df, symbol)
        rows = len(result) if result is not None else 0
        print(f"  [fetch] {symbol} {interval} → {rows} rows", flush=True)
        return result
    except Exception as e:
        print(f"  [fetch] {symbol} {interval} ERROR: {e}", flush=True)
        return None


def fetch(symbol: str, period: str, interval: str) -> pd.DataFrame | None:
    return _fetch_simple(symbol, period, interval)


# ── Batch parallel prefetch ───────────────────────────────────────────────

def batch_scan_populate(symbols: list[str]) -> None:
    """
    Fetch daily (30d) + hourly (5d) for all symbols + SPY in parallel.

    Uses ThreadPoolExecutor(10 workers) + concurrent.futures.wait(timeout=45).
    executor.shutdown(wait=False) means we never block on a hung download —
    the socket.setdefaulttimeout(15) will eventually kill those threads naturally.
    """
    all_syms = list(dict.fromkeys(symbols + ["SPY"]))
    now = time.time()
    print(
        f"  [market_data] batch prefetch start — {len(all_syms)} symbols"
        f"  ({_PARALLEL_WORKERS} workers, {_BATCH_WALL_SECS}s wall)",
        flush=True,
    )

    def _fetch_both(sym: str):
        df_d = _fetch_simple(sym, "30d", "1d")
        df_h = _fetch_simple(sym, "5d",  "1h")
        return sym, df_d, df_h

    executor = concurrent.futures.ThreadPoolExecutor(max_workers=_PARALLEL_WORKERS)
    future_to_sym = {executor.submit(_fetch_both, sym): sym for sym in all_syms}

    done, not_done = concurrent.futures.wait(
        future_to_sym.keys(), timeout=_BATCH_WALL_SECS
    )
    # Critical: don't block on hung threads — socket timeout will clean them up
    executor.shutdown(wait=False)

    populated = 0
    with _cache_lock:
        for future in done:
            sym = future_to_sym[future]
            try:
                _, df_d, df_h = future.result()
                _cache[sym] = {"daily": df_d, "hourly": df_h, "ts": now}
                if df_d is not None:
                    populated += 1
            except Exception as e:
                print(f"  [market_data] {sym} result error: {e}", flush=True)
                _cache[sym] = {"daily": None, "hourly": None, "ts": now}

        for future in not_done:
            sym = future_to_sym[future]
            print(f"  [market_data] {sym}: still running at {_BATCH_WALL_SECS}s wall — marking no_data", flush=True)
            _cache[sym] = {"daily": None, "hourly": None, "ts": now}

    elapsed = time.time() - now
    print(
        f"  [market_data] batch done — {populated}/{len(all_syms)} populated"
        f"  in {elapsed:.1f}s  ({len(not_done)} timed out)",
        flush=True,
    )


# ── Cache-aware OHLC getter ───────────────────────────────────────────────

def get_ohlc(symbol: str) -> tuple[pd.DataFrame | None, pd.DataFrame | None]:
    now = time.time()
    with _cache_lock:
        cached = _cache.get(symbol)
    if cached and now - cached["ts"] < _CACHE_TTL:
        return cached["daily"], cached["hourly"]

    df_d = _fetch_simple(symbol, "30d", "1d")
    df_h = _fetch_simple(symbol, "5d",  "1h")
    with _cache_lock:
        _cache[symbol] = {"daily": df_d, "hourly": df_h, "ts": now}
    return df_d, df_h


def get_price(symbol: str) -> float | None:
    """Latest price via yf.download (1-minute bar), falls back to cached daily close."""
    try:
        df = yf.download(symbol, period="1d", interval="1m",
                         progress=False, auto_adjust=True)
        df_n = _normalise(df, symbol)
        if df_n is not None and len(df_n):
            return float(df_n["close"].iloc[-1])
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
    # SPY needs 60d for the 50-day SMA (batch only fetches 30d for other symbols)
    df = _fetch_simple("SPY", "60d", "1d")
    with _cache_lock:
        if "SPY" not in _cache:
            _cache["SPY"] = {"daily": None, "hourly": None, "ts": 0.0}
        _cache["SPY"]["daily"] = df
        _cache["SPY"]["ts"] = time.time()
    return df


def get_spy_regime() -> dict:
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
    return {"regime": regime, "spy_price": round(price, 2), "spy_50sma": round(sma50, 2)}


# ── Relative strength ─────────────────────────────────────────────────────

def get_relative_strength(symbol: str) -> float:
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
    return _earnings_cache.get(symbol, False)


def _check_earnings(symbol: str) -> bool:
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
                if 0 <= (pd.Timestamp(d).date() - today).days <= 4:
                    return True
            except Exception:
                pass
    except Exception:
        pass
    return False


def refresh_earnings_cache(symbols: list[str]) -> None:
    print(f"  [market_data] earnings refresh start — {len(symbols)} symbols", flush=True)
    new_cache: dict[str, bool] = {}
    for sym in symbols:
        new_cache[sym] = _check_earnings(sym)
        time.sleep(0.1)
    _earnings_cache.update(new_cache)
    flagged = [s for s, v in new_cache.items() if v]
    print(f"  [market_data] earnings refresh done — upcoming: {flagged or 'none'}", flush=True)
