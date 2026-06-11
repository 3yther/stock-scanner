"""
market_data.py — Alpha Vantage backend.

Endpoints used:
  Daily   : TIME_SERIES_DAILY   (compact = last 100 trading days)
  Intraday: TIME_SERIES_INTRADAY interval=60min (compact = last 100 hours)

Rate limit: Alpha Vantage free tier = 5 requests/minute → 12 s between requests.
  60 symbols × 2 endpoints = 120 requests → ~24 min cold scan.
  Cache TTL = 4 hours: after the cold scan every cycle is instant until cache expires.
  With 25 req/day free plan only 1 cold scan is possible; upgrade to 500 req/day
  standard plan ($50/mo) for production use.

Set ALPHA_VANTAGE_KEY in your .env file or Railway environment variables.
"""

import os
import threading
import time
from datetime import datetime

import pandas as pd
import requests as _http

# ── Config ────────────────────────────────────────────────────────────────
ALPHA_VANTAGE_KEY = os.getenv("ALPHA_VANTAGE_KEY", "")
_AV_BASE          = "https://www.alphavantage.co/query"
_MIN_INTERVAL     = 12.0   # seconds between requests (5 req/min free tier)
_CACHE_TTL        = 4 * 3600  # 4 hours — reuse data between scans
_HTTP_TIMEOUT     = 20     # seconds per request

# ── In-process cache ──────────────────────────────────────────────────────
_cache: dict[str, dict] = {}   # symbol → {"daily": df, "hourly": df, "ts": float}
_cache_lock = threading.Lock()

# ── Rate-limiter state ────────────────────────────────────────────────────
_last_req_ts: float = 0.0
_rl_lock = threading.Lock()


# ── Rate limiter ──────────────────────────────────────────────────────────

def _rate_limit() -> None:
    """Block until at least _MIN_INTERVAL seconds have passed since last request."""
    global _last_req_ts
    with _rl_lock:
        elapsed = time.time() - _last_req_ts
        if elapsed < _MIN_INTERVAL:
            wait = _MIN_INTERVAL - elapsed
            print(f"  [AV] rate limit — sleeping {wait:.1f}s", flush=True)
            time.sleep(wait)
        _last_req_ts = time.time()


# ── HTTP helper ───────────────────────────────────────────────────────────

def _av_get(params: dict) -> dict | None:
    """Make one Alpha Vantage request with rate limiting and error handling."""
    if not ALPHA_VANTAGE_KEY:
        print("  [AV] ALPHA_VANTAGE_KEY not set — cannot fetch data", flush=True)
        return None

    _rate_limit()
    url_params = {**params, "apikey": ALPHA_VANTAGE_KEY}
    print(f"  [AV] GET function={params.get('function')} symbol={params.get('symbol', '?')}", flush=True)

    try:
        r = _http.get(_AV_BASE, params=url_params, timeout=_HTTP_TIMEOUT)
        r.raise_for_status()
        data = r.json()
    except Exception as e:
        print(f"  [AV] request error: {e}", flush=True)
        return None

    # AV signals rate-limit exceeded with a "Note" key
    if "Note" in data:
        print(f"  [AV] rate-limit note: {data['Note'][:100]}", flush=True)
        print(f"  [AV] sleeping 60s then continuing", flush=True)
        time.sleep(60)
        return None

    # AV signals API key problems or quota exhaustion with "Information"
    if "Information" in data:
        print(f"  [AV] API info: {data['Information'][:120]}", flush=True)
        return None

    return data


# ── Response parsers ──────────────────────────────────────────────────────

def _parse_daily(data: dict | None, symbol: str) -> pd.DataFrame | None:
    if not data:
        return None
    ts = data.get("Time Series (Daily)")
    if not ts:
        print(f"  [AV] {symbol} daily: unexpected keys {list(data.keys())[:4]}", flush=True)
        return None
    rows = []
    for date_str, vals in ts.items():
        try:
            rows.append({
                "datetime": pd.Timestamp(date_str),
                "open":     float(vals["1. open"]),
                "high":     float(vals["2. high"]),
                "low":      float(vals["3. low"]),
                "close":    float(vals["4. close"]),
                "volume":   float(vals["5. volume"]),
            })
        except Exception:
            continue
    if not rows:
        return None
    df = pd.DataFrame(rows).sort_values("datetime").reset_index(drop=True)
    # Keep most recent 60 trading days (enough for MACD-26 + 20-day vol + 20-day RS)
    return df.tail(60).reset_index(drop=True)


def _parse_intraday(data: dict | None, symbol: str) -> pd.DataFrame | None:
    if not data:
        return None
    ts = data.get("Time Series (60min)")
    if not ts:
        print(f"  [AV] {symbol} intraday: unexpected keys {list(data.keys())[:4]}", flush=True)
        return None
    rows = []
    for dt_str, vals in ts.items():
        try:
            rows.append({
                "datetime": pd.Timestamp(dt_str),
                "open":     float(vals["1. open"]),
                "high":     float(vals["2. high"]),
                "low":      float(vals["3. low"]),
                "close":    float(vals["4. close"]),
                "volume":   float(vals["5. volume"]),
            })
        except Exception:
            continue
    if not rows:
        return None
    df = pd.DataFrame(rows).sort_values("datetime").reset_index(drop=True)
    # Keep last 100 hourly bars (~2.5 weeks of trading hours — enough for MACD-26)
    return df.tail(100).reset_index(drop=True)


# ── Per-symbol fetch ──────────────────────────────────────────────────────

def _fetch_symbol(symbol: str) -> tuple[pd.DataFrame | None, pd.DataFrame | None]:
    """Fetch daily + intraday for one symbol. Makes 2 API calls (rate limited)."""
    daily_data = _av_get({
        "function":   "TIME_SERIES_DAILY",
        "symbol":     symbol,
        "outputsize": "compact",
    })
    df_d = _parse_daily(daily_data, symbol)

    hourly_data = _av_get({
        "function":   "TIME_SERIES_INTRADAY",
        "symbol":     symbol,
        "interval":   "60min",
        "outputsize": "compact",
    })
    df_h = _parse_intraday(hourly_data, symbol)

    d_rows = len(df_d) if df_d is not None else 0
    h_rows = len(df_h) if df_h is not None else 0
    print(f"  [AV] {symbol} fetched — daily={d_rows} rows  hourly={h_rows} rows", flush=True)
    return df_d, df_h


# ── Batch prefetch ────────────────────────────────────────────────────────

def batch_scan_populate(symbols: list[str]) -> None:
    """
    Fetch every stale symbol sequentially respecting the 5 req/min rate limit.

    First call (cold cache): fetches all 60+SPY → ~24 min.
    Subsequent calls within 4 hours: all cached → instant.
    """
    all_syms = list(dict.fromkeys(symbols + ["SPY"]))
    now = time.time()

    with _cache_lock:
        stale = [s for s in all_syms
                 if s not in _cache or now - _cache[s].get("ts", 0) >= _CACHE_TTL]

    if not stale:
        print(f"  [AV] all {len(all_syms)} symbols fresh in cache — skipping fetch", flush=True)
        return

    est_min = len(stale) * 2 * _MIN_INTERVAL / 60
    print(
        f"  [AV] fetching {len(stale)}/{len(all_syms)} stale symbols"
        f" (est {est_min:.0f} min at 5 req/min)…",
        flush=True,
    )

    for i, sym in enumerate(stale, 1):
        print(f"  [AV] [{i}/{len(stale)}] fetching {sym}…", flush=True)
        df_d, df_h = _fetch_symbol(sym)
        with _cache_lock:
            _cache[sym] = {"daily": df_d, "hourly": df_h, "ts": time.time()}

    with _cache_lock:
        populated = sum(1 for s in stale if _cache.get(s, {}).get("daily") is not None)
    print(f"  [AV] batch done — {populated}/{len(stale)} symbols populated", flush=True)


# ── Public OHLC getter ────────────────────────────────────────────────────

def get_ohlc(symbol: str) -> tuple[pd.DataFrame | None, pd.DataFrame | None]:
    now = time.time()
    with _cache_lock:
        cached = _cache.get(symbol)
    if cached and now - cached["ts"] < _CACHE_TTL:
        return cached["daily"], cached["hourly"]
    # Single-symbol fallback (batch_scan_populate normally pre-fills this)
    df_d, df_h = _fetch_symbol(symbol)
    with _cache_lock:
        _cache[symbol] = {"daily": df_d, "hourly": df_h, "ts": time.time()}
    return df_d, df_h


def get_price(symbol: str) -> float | None:
    """Return latest close from cached daily data — no extra API call."""
    with _cache_lock:
        cached = _cache.get(symbol)
    if cached and cached["daily"] is not None and len(cached["daily"]):
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
    # Force-fetch if not cached (batch_scan_populate should have done this already)
    df_d, df_h = _fetch_symbol("SPY")
    with _cache_lock:
        _cache["SPY"] = {"daily": df_d, "hourly": df_h, "ts": time.time()}
    return df_d


def get_spy_regime() -> dict:
    df = _get_spy_daily()
    if df is None or len(df) < 50:
        return {"regime": "NEUTRAL", "spy_price": 0.0, "spy_50sma": 0.0}
    sma50  = float(df["close"].rolling(50).mean().iloc[-1])
    price  = float(df["close"].iloc[-1])
    if   price > sma50 * 1.005: regime = "BULLISH"
    elif price < sma50 * 0.995: regime = "BEARISH"
    else:                        regime = "NEUTRAL"
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


# ── Earnings (disabled — conserves API quota) ─────────────────────────────

def get_earnings_flag(symbol: str) -> bool:
    # Disabled: each check would cost 1 API request (60 requests = half the daily budget).
    # Re-enable by implementing AV EARNINGS_CALENDAR endpoint on a paid plan.
    return False


def refresh_earnings_cache(symbols: list[str]) -> None:
    print("  [AV] earnings refresh skipped (disabled to conserve API quota)", flush=True)
