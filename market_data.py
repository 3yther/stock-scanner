"""
market_data.py — Polygon.io backend.

Endpoints used:
  Daily  : /v2/aggs/ticker/{symbol}/range/1/day/{from}/{to}   (last 90 days)
  Hourly : /v2/aggs/ticker/{symbol}/range/1/hour/{from}/{to}  (last 5 days)
  Live   : /v2/last/trade/{symbol}

No rate limiting required on Polygon free tier.
Cache TTL = 4 hours.

Set POLYGON_API_KEY in your .env file or Railway environment variables.

Note: get_spy_regime uses a 50-day SMA. 30 calendar days ≈ 22 trading days,
which is insufficient; daily window is set to 90 days (~63 trading days) so
all indicators (MACD-26, 50-day SMA, 21-day relative-strength) have enough bars.
"""

import os
import threading
import time
from datetime import datetime, timedelta

import pandas as pd
import requests as _http

# ── Config ────────────────────────────────────────────────────────────────
POLYGON_API_KEY = os.getenv("POLYGON_API_KEY", "")
_POLY_BASE      = "https://api.polygon.io"
_DAILY_DAYS     = 90   # calendar days back for daily bars  (~63 trading days)
_HOURLY_DAYS    = 5    # calendar days back for hourly bars
_CACHE_TTL      = 4 * 3600   # 4 hours
_HTTP_TIMEOUT   = 20         # seconds per request

# ── In-process cache ──────────────────────────────────────────────────────
_cache: dict[str, dict] = {}
_cache_lock = threading.Lock()


# ── Date helpers ──────────────────────────────────────────────────────────

def _today() -> str:
    return datetime.utcnow().strftime("%Y-%m-%d")


def _days_ago(n: int) -> str:
    return (datetime.utcnow() - timedelta(days=n)).strftime("%Y-%m-%d")


# ── HTTP helper ───────────────────────────────────────────────────────────

def _poly_get(path: str) -> dict | None:
    """GET one Polygon endpoint; returns parsed JSON or None on any error."""
    if not POLYGON_API_KEY:
        print("  [Poly] POLYGON_API_KEY not set — cannot fetch data", flush=True)
        return None

    sep = "&" if "?" in path else "?"
    url = f"{path}{sep}apiKey={POLYGON_API_KEY}"
    print(f"  [Poly] GET {path}", flush=True)

    try:
        r = _http.get(url, timeout=_HTTP_TIMEOUT)
        r.raise_for_status()
        return r.json()
    except Exception as e:
        print(f"  [Poly] request error: {e}", flush=True)
        return None


# ── Response parser ───────────────────────────────────────────────────────

def _parse_aggs(data: dict | None, symbol: str, label: str) -> pd.DataFrame | None:
    """Convert a Polygon aggregates response into a sorted OHLCV DataFrame."""
    if not data:
        return None
    results = data.get("results")
    if not results:
        status = data.get("status", "?")
        print(f"  [Poly] {symbol} {label}: no results (status={status})", flush=True)
        return None
    rows = []
    for bar in results:
        try:
            rows.append({
                "datetime": pd.Timestamp(bar["t"], unit="ms"),
                "open":     float(bar["o"]),
                "high":     float(bar["h"]),
                "low":      float(bar["l"]),
                "close":    float(bar["c"]),
                "volume":   float(bar["v"]),
            })
        except Exception:
            continue
    if not rows:
        return None
    return pd.DataFrame(rows).sort_values("datetime").reset_index(drop=True)


# ── Per-symbol fetch ──────────────────────────────────────────────────────

def _fetch_symbol(symbol: str) -> tuple[pd.DataFrame | None, pd.DataFrame | None]:
    """Fetch daily + hourly bars for one symbol. Makes 2 API calls."""
    today   = _today()

    daily_url  = (
        f"{_POLY_BASE}/v2/aggs/ticker/{symbol}/range/1/day"
        f"/{_days_ago(_DAILY_DAYS)}/{today}"
    )
    df_d = _parse_aggs(_poly_get(daily_url), symbol, "daily")

    hourly_url = (
        f"{_POLY_BASE}/v2/aggs/ticker/{symbol}/range/1/hour"
        f"/{_days_ago(_HOURLY_DAYS)}/{today}"
    )
    df_h = _parse_aggs(_poly_get(hourly_url), symbol, "hourly")

    d_rows = len(df_d) if df_d is not None else 0
    h_rows = len(df_h) if df_h is not None else 0
    print(f"  [Poly] {symbol} fetched — daily={d_rows} rows  hourly={h_rows} rows", flush=True)
    return df_d, df_h


# ── Batch prefetch ────────────────────────────────────────────────────────

def batch_scan_populate(symbols: list[str]) -> None:
    """
    Fetch every stale symbol sequentially. No rate limiting needed on Polygon.

    First call (cold cache): fetches all symbols + SPY.
    Subsequent calls within 4 hours: all cached → instant.
    """
    all_syms = list(dict.fromkeys(symbols + ["SPY"]))
    now = time.time()

    with _cache_lock:
        stale = [s for s in all_syms
                 if s not in _cache or now - _cache[s].get("ts", 0) >= _CACHE_TTL]

    if not stale:
        print(f"  [Poly] all {len(all_syms)} symbols fresh in cache — skipping fetch", flush=True)
        return

    print(f"  [Poly] fetching {len(stale)}/{len(all_syms)} stale symbols…", flush=True)

    for i, sym in enumerate(stale, 1):
        print(f"  [Poly] [{i}/{len(stale)}] fetching {sym}…", flush=True)
        df_d, df_h = _fetch_symbol(sym)
        with _cache_lock:
            _cache[sym] = {"daily": df_d, "hourly": df_h, "ts": time.time()}

    with _cache_lock:
        populated = sum(1 for s in stale if _cache.get(s, {}).get("daily") is not None)
    print(f"  [Poly] batch done — {populated}/{len(stale)} symbols populated", flush=True)


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
    """Return live last-trade price from Polygon, falling back to cached daily close."""
    url  = f"{_POLY_BASE}/v2/last/trade/{symbol}"
    data = _poly_get(url)
    if data and "results" in data:
        try:
            return float(data["results"]["p"])
        except Exception:
            pass
    # Fallback: last close from cache
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
    df_d, df_h = _fetch_symbol("SPY")
    with _cache_lock:
        _cache["SPY"] = {"daily": df_d, "hourly": df_h, "ts": time.time()}
    return df_d


def get_spy_regime() -> dict:
    df = _get_spy_daily()
    if df is None or len(df) < 50:
        return {"regime": "NEUTRAL", "spy_price": 0.0, "spy_50sma": 0.0}
    sma50 = float(df["close"].rolling(50).mean().iloc[-1])
    price = float(df["close"].iloc[-1])
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


# ── Earnings (stub) ───────────────────────────────────────────────────────

def get_earnings_flag(symbol: str) -> bool:
    return False


def refresh_earnings_cache(symbols: list[str]) -> None:
    print("  [Poly] earnings refresh skipped (not implemented)", flush=True)
