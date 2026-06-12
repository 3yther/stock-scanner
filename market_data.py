"""
market_data.py — Polygon.io backend with rate limiting.

Polygon free tier: 5 API calls / minute.
Strategy:
  - One global lock serialises every Polygon HTTP call.
  - 12-second sleep after every successful call  → max 5/min.
  - On 429: back off 60 s and retry up to 3 times.

Cache TTLs (separate per data type):
  Daily  bars : 4 hours   — daily OHLCV only changes after market close
  Hourly bars : 30 minutes — intraday momentum needs fresher data

Cold-start cost: 25 symbols × 2 calls × ~12 s = ~10 minutes.
Hot path:  cache hit → no HTTP call → no lock contention → instant.
"""

import collections
import os
import threading
import time
from datetime import datetime, timedelta

import pandas as pd
import requests as _http

# ── Config ────────────────────────────────────────────────────────────────
POLYGON_API_KEY  = os.getenv("POLYGON_API_KEY", "")
_POLY_BASE       = "https://api.polygon.io"
_DAILY_DAYS      = 90       # calendar days back for daily bars (~63 trading days)
_HOURLY_DAYS     = 5        # calendar days back for hourly bars
_DAILY_TTL       = 4 * 3600     # 4 hours
_HOURLY_TTL      = 30 * 60     # 30 minutes
_HTTP_TIMEOUT    = 20           # seconds per HTTP call
_INTER_REQ_SLEEP = 12.0         # seconds between requests (keeps us at 5/min)
_RETRY_WAIT      = 60.0         # seconds to wait after a 429
_MAX_RETRIES     = 3
_RATE_LIMIT      = 5            # max calls/minute on free tier
_RATE_WINDOW     = 60.0         # rolling window for budget display

# ── Per-symbol cache ──────────────────────────────────────────────────────
# Structure per symbol:
#   {"daily": df|None, "daily_ts": float, "hourly": df|None, "hourly_ts": float}
_cache: dict[str, dict] = {}
_cache_lock = threading.Lock()

# ── Rate-limiter state ────────────────────────────────────────────────────
_rl_lock         = threading.Lock()                    # one HTTP call at a time
_call_times: collections.deque = collections.deque()   # timestamps of recent calls
_last_call_time: float = 0.0
_cache_hits:    int    = 0
_total_requests: int   = 0


# ── Date helpers ──────────────────────────────────────────────────────────

def _today() -> str:
    return datetime.utcnow().strftime("%Y-%m-%d")


def _days_ago(n: int) -> str:
    return (datetime.utcnow() - timedelta(days=n)).strftime("%Y-%m-%d")


# ── Core HTTP helper — rate-limited, retried ──────────────────────────────

def _poly_get(path: str) -> dict | None:
    """
    GET one Polygon endpoint.
    Acquires _rl_lock so only one request runs at a time.
    Sleeps _INTER_REQ_SLEEP seconds after every successful call.
    On HTTP 429: sleeps _RETRY_WAIT seconds then retries (up to _MAX_RETRIES).
    """
    global _last_call_time, _total_requests

    if not POLYGON_API_KEY:
        print("  [Poly] POLYGON_API_KEY not set — cannot fetch", flush=True)
        return None

    sep = "&" if "?" in path else "?"
    url = f"{path}{sep}apiKey={POLYGON_API_KEY}"

    with _rl_lock:
        # Show rolling budget before the call
        now = time.time()
        while _call_times and now - _call_times[0] > _RATE_WINDOW:
            _call_times.popleft()
        print(
            f"  [Poly] rate limit budget: {len(_call_times)}/{_RATE_LIMIT} used this minute",
            flush=True,
        )

        for attempt in range(1, _MAX_RETRIES + 1):
            print(f"  [Poly] GET {path[:90]}", flush=True)
            try:
                r = _http.get(url, timeout=_HTTP_TIMEOUT)
            except Exception as exc:
                print(f"  [Poly] network error: {exc}", flush=True)
                return None

            # Record every call (even 429s) for budget tracking
            now = time.time()
            _call_times.append(now)
            _last_call_time = now
            _total_requests += 1

            if r.status_code == 429:
                if attempt < _MAX_RETRIES:
                    print(
                        f"  [Poly] 429 received — backing off {int(_RETRY_WAIT)}s,"
                        f" retry {attempt}/{_MAX_RETRIES}",
                        flush=True,
                    )
                    time.sleep(_RETRY_WAIT)
                    continue
                print(
                    f"  [Poly] 429 — max retries ({_MAX_RETRIES}) exhausted, giving up",
                    flush=True,
                )
                return None

            if not r.ok:
                print(f"  [Poly] HTTP {r.status_code} on {path[:70]}", flush=True)
                # Still sleep so we don't hammer on errors
                time.sleep(_INTER_REQ_SLEEP)
                return None

            data = r.json()
            # Inter-request sleep while still holding the lock — next call waits
            time.sleep(_INTER_REQ_SLEEP)
            return data

    return None


# ── Response parser ───────────────────────────────────────────────────────

def _parse_aggs(data: dict | None, symbol: str, label: str) -> pd.DataFrame | None:
    """Convert a Polygon aggregates response into a sorted OHLCV DataFrame."""
    if not data:
        return None
    results = data.get("results")
    if not results:
        print(
            f"  [Poly] {symbol} {label}: no results (status={data.get('status','?')})",
            flush=True,
        )
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


# ── Per-type fetchers (each costs 1 API call + 12 s sleep) ───────────────

def _fetch_daily(symbol: str) -> pd.DataFrame | None:
    url = (
        f"{_POLY_BASE}/v2/aggs/ticker/{symbol}/range/1/day"
        f"/{_days_ago(_DAILY_DAYS)}/{_today()}"
    )
    df = _parse_aggs(_poly_get(url), symbol, "daily")
    rows = len(df) if df is not None else 0
    print(f"  [Poly] {symbol} daily — {rows} rows", flush=True)
    return df


def _fetch_hourly(symbol: str) -> pd.DataFrame | None:
    url = (
        f"{_POLY_BASE}/v2/aggs/ticker/{symbol}/range/1/hour"
        f"/{_days_ago(_HOURLY_DAYS)}/{_today()}"
    )
    df = _parse_aggs(_poly_get(url), symbol, "hourly")
    rows = len(df) if df is not None else 0
    print(f"  [Poly] {symbol} hourly — {rows} rows", flush=True)
    return df


# ── Batch prefetch ────────────────────────────────────────────────────────

def batch_scan_populate(symbols: list[str]) -> None:
    """
    Fetch every stale symbol sequentially, obeying the rate limit.

    Daily  data: stale after 4 hours  → 1 call/symbol
    Hourly data: stale after 30 minutes → 1 call/symbol

    Fetches daily then hourly per symbol so each symbol is fully populated
    before the scanner processes it.  Re-checks freshness inside the loop so
    concurrent code paths (e.g. _get_spy_daily) don't trigger double-fetches.
    """
    all_syms = list(dict.fromkeys(symbols + ["SPY"]))
    now = time.time()

    with _cache_lock:
        daily_stale  = {s for s in all_syms
                        if s not in _cache or now - _cache[s].get("daily_ts",  0) >= _DAILY_TTL}
        hourly_stale = {s for s in all_syms
                        if s not in _cache or now - _cache[s].get("hourly_ts", 0) >= _HOURLY_TTL}

    total_calls = len(daily_stale) + len(hourly_stale)
    if total_calls == 0:
        print(f"  [Poly] all {len(all_syms)} symbols fresh in cache — skipping fetch", flush=True)
        return

    est_min = total_calls * (_INTER_REQ_SLEEP + 1) / 60
    print(
        f"  [Poly] batch: {len(daily_stale)} daily + {len(hourly_stale)} hourly stale"
        f" = {total_calls} API calls  (est. {est_min:.1f} min)",
        flush=True,
    )

    done = 0
    for sym in all_syms:
        needs_daily  = sym in daily_stale
        needs_hourly = sym in hourly_stale
        if not needs_daily and not needs_hourly:
            continue

        # Re-check: another code path may have already fetched this symbol
        now = time.time()
        with _cache_lock:
            entry = _cache.get(sym, {})
            if needs_daily  and now - entry.get("daily_ts",  0) < _DAILY_TTL:
                needs_daily  = False
            if needs_hourly and now - entry.get("hourly_ts", 0) < _HOURLY_TTL:
                needs_hourly = False

        if needs_daily:
            done += 1
            print(f"  [Poly] [{done}/{total_calls}] {sym} daily …", flush=True)
            df_d = _fetch_daily(sym)
            with _cache_lock:
                e = _cache.setdefault(sym, {})
                e["daily"]    = df_d
                e["daily_ts"] = time.time()

        if needs_hourly:
            done += 1
            print(f"  [Poly] [{done}/{total_calls}] {sym} hourly …", flush=True)
            df_h = _fetch_hourly(sym)
            with _cache_lock:
                e = _cache.setdefault(sym, {})
                e["hourly"]    = df_h
                e["hourly_ts"] = time.time()

    with _cache_lock:
        populated = sum(1 for s in daily_stale if _cache.get(s, {}).get("daily") is not None)
    print(f"  [Poly] batch done — {populated}/{len(daily_stale)} daily populated", flush=True)


# ── Public OHLC getter ────────────────────────────────────────────────────

def get_ohlc(symbol: str) -> tuple[pd.DataFrame | None, pd.DataFrame | None]:
    global _cache_hits
    now = time.time()

    with _cache_lock:
        entry        = _cache.get(symbol, {})
        daily_fresh  = now - entry.get("daily_ts",  0) < _DAILY_TTL
        hourly_fresh = now - entry.get("hourly_ts", 0) < _HOURLY_TTL

    if daily_fresh and hourly_fresh:
        _cache_hits += 1
        return entry.get("daily"), entry.get("hourly")

    # Fetch only what is stale
    if not daily_fresh:
        df_d = _fetch_daily(symbol)
        with _cache_lock:
            e = _cache.setdefault(symbol, {})
            e["daily"]    = df_d
            e["daily_ts"] = time.time()

    if not hourly_fresh:
        df_h = _fetch_hourly(symbol)
        with _cache_lock:
            e = _cache.setdefault(symbol, {})
            e["hourly"]    = df_h
            e["hourly_ts"] = time.time()

    with _cache_lock:
        entry = _cache.get(symbol, {})
    return entry.get("daily"), entry.get("hourly")


def get_price(symbol: str) -> float | None:
    """Return live last-trade price, falling back to cached daily close."""
    url  = f"{_POLY_BASE}/v2/last/trade/{symbol}"
    data = _poly_get(url)
    if data and "results" in data:
        try:
            return float(data["results"]["p"])
        except Exception:
            pass
    with _cache_lock:
        df = _cache.get(symbol, {}).get("daily")
    if df is not None and len(df):
        return float(df["close"].iloc[-1])
    return None


def invalidate(symbol: str) -> None:
    with _cache_lock:
        _cache.pop(symbol, None)


# ── SPY regime ────────────────────────────────────────────────────────────

def _get_spy_daily() -> pd.DataFrame | None:
    now = time.time()
    with _cache_lock:
        entry = _cache.get("SPY", {})
        if entry.get("daily") is not None and now - entry.get("daily_ts", 0) < _DAILY_TTL:
            return entry["daily"]
    df_d = _fetch_daily("SPY")
    with _cache_lock:
        e = _cache.setdefault("SPY", {})
        e["daily"]    = df_d
        e["daily_ts"] = time.time()
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
        df_sym = _cache.get(symbol, {}).get("daily")
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


# ── Status snapshot (for /api/poly_status) ───────────────────────────────

def poly_status() -> dict:
    """Return a snapshot of rate-limiter and cache state."""
    now = time.time()
    calls_recent = sum(1 for t in _call_times if now - t <= _RATE_WINDOW)
    with _cache_lock:
        cache_size = len(_cache)
        daily_pop  = sum(1 for v in _cache.values() if v.get("daily")  is not None)
        hourly_pop = sum(1 for v in _cache.values() if v.get("hourly") is not None)
    return {
        "last_call_time":        _last_call_time if _last_call_time else None,
        "calls_in_last_minute":  calls_recent,
        "rate_limit_per_minute": _RATE_LIMIT,
        "total_requests":        _total_requests,
        "cache_hits":            _cache_hits,
        "cache_size":            cache_size,
        "daily_populated":       daily_pop,
        "hourly_populated":      hourly_pop,
        "inter_request_sleep_s": _INTER_REQ_SLEEP,
        "daily_ttl_hours":       _DAILY_TTL / 3600,
        "hourly_ttl_minutes":    _HOURLY_TTL / 60,
    }
