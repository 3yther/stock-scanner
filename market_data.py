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
import json
import os
import threading
import time
from datetime import datetime, timedelta

import pandas as pd
import requests as _http

import strategies

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
_CACHE_FILE      = "/tmp/polygon_cache.json"   # disk cache survives restarts

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
_cache_misses:  int    = 0
_total_requests: int   = 0
_calls_today:   int    = 0
_calls_today_date: str = ""   # UTC date the _calls_today counter belongs to


# ── Date helpers ──────────────────────────────────────────────────────────

def _today() -> str:
    return datetime.utcnow().strftime("%Y-%m-%d")


def _days_ago(n: int) -> str:
    return (datetime.utcnow() - timedelta(days=n)).strftime("%Y-%m-%d")


def _fmt_age(age_s: float) -> str:
    """Format an age in seconds as '2h 14m' or '18m'."""
    age_s = max(0, int(age_s))
    h, m = age_s // 3600, (age_s % 3600) // 60
    return f"{h}h {m}m" if h else f"{m}m"


# ── URL builders ──────────────────────────────────────────────────────────

def _daily_url(symbol: str) -> str:
    return (
        f"{_POLY_BASE}/v2/aggs/ticker/{symbol}/range/1/day"
        f"/{_days_ago(_DAILY_DAYS)}/{_today()}"
    )


def _hourly_url(symbol: str) -> str:
    return (
        f"{_POLY_BASE}/v2/aggs/ticker/{symbol}/range/1/hour"
        f"/{_days_ago(_HOURLY_DAYS)}/{_today()}"
    )


# ── Core HTTP helper — rate-limited, retried ──────────────────────────────

def _poly_get(path: str) -> dict | None:
    """
    GET one Polygon endpoint.
    Acquires _rl_lock so only one request runs at a time.
    Sleeps _INTER_REQ_SLEEP seconds after every successful call.
    On HTTP 429: sleeps _RETRY_WAIT seconds then retries (up to _MAX_RETRIES).
    """
    global _last_call_time, _total_requests, _calls_today, _calls_today_date

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

            # Daily call counter — reset when the UTC date rolls over
            today = _today()
            if today != _calls_today_date:
                _calls_today_date = today
                _calls_today = 0
            _calls_today += 1

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


# ── Disk persistence ──────────────────────────────────────────────────────
# Cache survives restarts so one full scan covers subsequent restarts without
# burning API calls. DataFrames serialise as column arrays with epoch-ms dates.

def _df_to_cols(df: pd.DataFrame | None) -> dict | None:
    if df is None or df.empty:
        return None
    # Force millisecond resolution BEFORE int conversion. pandas 3.0 keeps the
    # column's native resolution (often datetime64[ms]), so a bare astype("int64")
    # already yields ms — the old "// 1_000_000" (which assumed ns) then shrank it
    # ~1e6×, deserialising back to 1970. Normalising to [ms] first is correct for
    # ns/us/ms columns alike.
    dt_ms = pd.to_datetime(df["datetime"]).astype("datetime64[ms]").astype("int64")
    return {
        "datetime": dt_ms.tolist(),
        "open":     df["open"].astype(float).tolist(),
        "high":     df["high"].astype(float).tolist(),
        "low":      df["low"].astype(float).tolist(),
        "close":    df["close"].astype(float).tolist(),
        "volume":   df["volume"].astype(float).tolist(),
    }


def _cols_to_df(cols: dict | None) -> pd.DataFrame | None:
    if not cols or not cols.get("datetime"):
        return None
    df = pd.DataFrame(cols)
    df["datetime"] = pd.to_datetime(df["datetime"], unit="ms")
    # Reject legacy/corrupt entries (pre-fix ms-as-ns writes deserialise to 1970)
    # so the caller refetches instead of trusting garbage timestamps.
    if df.empty or df["datetime"].iloc[-1].year < 2000:
        return None
    return df.sort_values("datetime").reset_index(drop=True)


def _save_cache() -> None:
    """Atomically write the in-memory cache to _CACHE_FILE."""
    try:
        with _cache_lock:
            snapshot = {
                sym: {
                    "daily":     _df_to_cols(e.get("daily")),
                    "daily_ts":  e.get("daily_ts", 0),
                    "hourly":    _df_to_cols(e.get("hourly")),
                    "hourly_ts": e.get("hourly_ts", 0),
                }
                for sym, e in _cache.items()
            }
        tmp = _CACHE_FILE + ".tmp"
        with open(tmp, "w") as f:
            json.dump(snapshot, f)
        os.replace(tmp, _CACHE_FILE)
    except Exception as exc:
        print(f"  [CACHE] save failed: {exc}", flush=True)


def _load_cache() -> None:
    """Load the disk cache on startup and log fresh/stale counts."""
    if not os.path.exists(_CACHE_FILE):
        print(f"  [CACHE] no cache file at {_CACHE_FILE} — starting cold", flush=True)
        return
    try:
        with open(_CACHE_FILE) as f:
            snapshot = json.load(f)
    except Exception as exc:
        print(f"  [CACHE] load failed ({exc}) — starting cold", flush=True)
        return

    now = time.time()
    d_fresh = d_stale = h_fresh = h_stale = 0
    with _cache_lock:
        for sym, e in snapshot.items():
            df_d = _cols_to_df(e.get("daily"))
            df_h = _cols_to_df(e.get("hourly"))
            _cache[sym] = {
                "daily":     df_d,  "daily_ts":  e.get("daily_ts", 0),
                "hourly":    df_h,  "hourly_ts": e.get("hourly_ts", 0),
            }
            if df_d is not None:
                if now - e.get("daily_ts", 0)  < _DAILY_TTL:  d_fresh += 1
                else:                                         d_stale += 1
            if df_h is not None:
                if now - e.get("hourly_ts", 0) < _HOURLY_TTL: h_fresh += 1
                else:                                         h_stale += 1

    print(f"  [CACHE] loaded {len(snapshot)} symbols from {_CACHE_FILE}", flush=True)
    print(f"  [CACHE] {d_fresh} daily candles fresh, {d_stale} stale", flush=True)
    print(f"  [CACHE] {h_fresh} hourly candles fresh, {h_stale} stale", flush=True)


# ── Per-type fetchers (each costs 1 API call + 12 s sleep) ───────────────

def _fetch_daily(symbol: str) -> pd.DataFrame | None:
    df = _parse_aggs(_poly_get(_daily_url(symbol)), symbol, "daily")
    rows = len(df) if df is not None else 0
    print(f"  [Poly] {symbol} daily — {rows} rows", flush=True)
    return df


def _fetch_hourly(symbol: str) -> pd.DataFrame | None:
    df = _parse_aggs(_poly_get(_hourly_url(symbol)), symbol, "hourly")
    rows = len(df) if df is not None else 0
    print(f"  [Poly] {symbol} hourly — {rows} rows", flush=True)
    return df


# ── Batch prefetch ────────────────────────────────────────────────────────

def batch_scan_populate(symbols: list[str]) -> None:
    """
    Fetch every stale symbol sequentially, obeying the rate limit.

    Daily  data: stale after 4 hours   → 1 call/symbol
    Hourly data: stale after 30 minutes → 1 call/symbol

    Builds an explicit ordered list of (symbol, kind) fetch steps up front, then
    walks it one step at a time. Each step fetches exactly one thing, prints the
    actual URL next to its own step number, then advances — so the step number,
    the symbol/kind label and the URL on the wire can never drift apart.
    """
    all_syms = list(dict.fromkeys(symbols + ["SPY"]))
    now = time.time()

    # Build the exact work list. One entry == one API call.
    steps: list[tuple[str, str]] = []
    with _cache_lock:
        for s in all_syms:
            e = _cache.get(s, {})
            if e.get("daily") is None or now - e.get("daily_ts", 0) >= _DAILY_TTL:
                steps.append((s, "daily"))
            if e.get("hourly") is None or now - e.get("hourly_ts", 0) >= _HOURLY_TTL:
                steps.append((s, "hourly"))

    total = len(steps)
    if total == 0:
        print(f"  [Poly] all {len(all_syms)} symbols fresh in cache — skipping fetch", flush=True)
        return

    n_daily  = sum(1 for _, k in steps if k == "daily")
    n_hourly = total - n_daily
    est_min  = total * (_INTER_REQ_SLEEP + 1) / 60
    print(
        f"  [Poly] batch: {n_daily} daily + {n_hourly} hourly stale"
        f" = {total} API calls  (est. {est_min:.1f} min)",
        flush=True,
    )

    populated = 0
    for i, (sym, kind) in enumerate(steps, 1):
        ttl = _DAILY_TTL if kind == "daily" else _HOURLY_TTL

        # Re-check: another code path may have already fetched this exact item.
        now = time.time()
        with _cache_lock:
            e = _cache.get(sym, {})
            if e.get(kind) is not None and now - e.get(f"{kind}_ts", 0) < ttl:
                print(f"  [Poly] [{i}/{total}] {sym} {kind} — already fresh, skipping", flush=True)
                continue

        url = _daily_url(sym) if kind == "daily" else _hourly_url(sym)
        print(f"  [Poly] [{i}/{total}] {sym} {kind} → {url}", flush=True)

        df = _parse_aggs(_poly_get(url), sym, kind)
        rows = len(df) if df is not None else 0
        print(f"  [Poly] [{i}/{total}] {sym} {kind} — {rows} rows", flush=True)

        with _cache_lock:
            e = _cache.setdefault(sym, {})
            e[kind]            = df
            e[f"{kind}_ts"]    = time.time()
        if df is not None:
            populated += 1
        _save_cache()

    print(f"  [Poly] batch done — {populated}/{total} steps populated", flush=True)


# ── Public OHLC getter ────────────────────────────────────────────────────

def get_ohlc(symbol: str) -> tuple[pd.DataFrame | None, pd.DataFrame | None]:
    global _cache_hits, _cache_misses
    now = time.time()

    with _cache_lock:
        entry        = _cache.get(symbol, {})
        daily_ts     = entry.get("daily_ts",  0)
        hourly_ts    = entry.get("hourly_ts", 0)
        daily_fresh  = entry.get("daily")  is not None and now - daily_ts  < _DAILY_TTL
        hourly_fresh = entry.get("hourly") is not None and now - hourly_ts < _HOURLY_TTL

    dirty = False

    # Daily — cache hit is instant (no sleep); a miss costs one rate-limited call.
    if daily_fresh:
        _cache_hits += 1
        print(f"  [CACHE HIT] {symbol} daily — age {_fmt_age(now - daily_ts)}", flush=True)
    else:
        _cache_misses += 1
        df_d = _fetch_daily(symbol)
        with _cache_lock:
            e = _cache.setdefault(symbol, {})
            e["daily"]    = df_d
            e["daily_ts"] = time.time()
        dirty = True

    # Hourly — same pattern.
    if hourly_fresh:
        _cache_hits += 1
        print(f"  [CACHE HIT] {symbol} hourly — age {_fmt_age(now - hourly_ts)}", flush=True)
    else:
        _cache_misses += 1
        df_h = _fetch_hourly(symbol)
        with _cache_lock:
            e = _cache.setdefault(symbol, {})
            e["hourly"]    = df_h
            e["hourly_ts"] = time.time()
        dirty = True

    if dirty:
        _save_cache()

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
    _save_cache()
    return df_d


def get_spy_regime() -> dict:
    # Delegates to the shared strategy core so live and backtest agree exactly.
    return strategies.spy_regime(_get_spy_daily())


# ── Relative strength ─────────────────────────────────────────────────────

def get_relative_strength(symbol: str) -> float:
    df_spy = _get_spy_daily()
    with _cache_lock:
        df_sym = _cache.get(symbol, {}).get("daily")
    return strategies.relative_strength(df_sym, df_spy)


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
        "cache_misses":          _cache_misses,
        "cache_size":            cache_size,
        "daily_populated":       daily_pop,
        "hourly_populated":      hourly_pop,
        "inter_request_sleep_s": _INTER_REQ_SLEEP,
        "daily_ttl_hours":       _DAILY_TTL / 3600,
        "hourly_ttl_minutes":    _HOURLY_TTL / 60,
    }


# ── Cache status snapshot (for /api/cache_status) ─────────────────────────

def cache_status() -> dict:
    """Disk-cache snapshot: file path/size, per-symbol ages, call & hit stats."""
    now = time.time()
    try:
        size = os.path.getsize(_CACHE_FILE) if os.path.exists(_CACHE_FILE) else 0
    except OSError:
        size = 0

    symbols_cached: list[dict] = []
    with _cache_lock:
        for sym, e in sorted(_cache.items()):
            d_ts = e.get("daily_ts", 0)
            h_ts = e.get("hourly_ts", 0)
            symbols_cached.append({
                "symbol":              sym,
                "daily_age_minutes":   round((now - d_ts) / 60, 1) if e.get("daily")  is not None and d_ts else None,
                "hourly_age_minutes":  round((now - h_ts) / 60, 1) if e.get("hourly") is not None and h_ts else None,
            })

    checks   = _cache_hits + _cache_misses
    hit_rate = round(_cache_hits / checks * 100, 1) if checks else 0.0
    calls_today = _calls_today if _calls_today_date == _today() else 0

    return {
        "cache_file_path":  _CACHE_FILE,
        "cache_size_bytes": size,
        "symbols_cached":   symbols_cached,
        "api_calls_today":  calls_today,
        "cache_hit_rate":   hit_rate,
    }


# ── Backtest history (long-range fetch, separately cached on disk) ────────
# Kept in its own cache file so a multi-year backtest pull never disturbs the
# small, fast live-scan cache. Re-running a backtest with the same range/symbols
# is served entirely from disk — zero API calls.
_BACKTEST_CACHE_FILE = "/tmp/polygon_backtest_cache.json"
_bt_cache:  dict[str, dict] = {}      # "SYM:tf:from:to" → column-array dict
_bt_lock    = threading.Lock()
_bt_loaded  = False


def _bt_load() -> None:
    global _bt_loaded
    if _bt_loaded:
        return
    _bt_loaded = True
    if not os.path.exists(_BACKTEST_CACHE_FILE):
        return
    try:
        with open(_BACKTEST_CACHE_FILE) as f:
            data = json.load(f)
        with _bt_lock:
            _bt_cache.update(data)
        print(f"  [BACKTEST] loaded {len(data)} cached history series from {_BACKTEST_CACHE_FILE}", flush=True)
    except Exception as exc:
        print(f"  [BACKTEST] history cache load failed: {exc}", flush=True)


def _bt_save() -> None:
    try:
        with _bt_lock:
            snap = dict(_bt_cache)
        tmp = _BACKTEST_CACHE_FILE + ".tmp"
        with open(tmp, "w") as f:
            json.dump(snap, f)
        os.replace(tmp, _BACKTEST_CACHE_FILE)
    except Exception as exc:
        print(f"  [BACKTEST] history cache save failed: {exc}", flush=True)


def fetch_history(symbol: str, timeframe: str, start: str, end: str) -> pd.DataFrame | None:
    """Fetch full OHLCV history for a backtest, cached to disk.

    timeframe : 'day' or 'hour'.  start/end : 'YYYY-MM-DD' (inclusive).
    Uses the rate-limited _poly_get (12 s spacing, 429 retry) and follows
    Polygon's next_url pagination. Cached by symbol/timeframe/range so a re-run
    with the same parameters costs no API calls.
    """
    _bt_load()
    key = f"{symbol}:{timeframe}:{start}:{end}"
    with _bt_lock:
        cols = _bt_cache.get(key)
    if cols is not None:
        cached = _cols_to_df(cols)
        if cached is not None:
            print(f"  [BACKTEST] cache hit {symbol} {timeframe} {start}→{end}", flush=True)
            return cached
        # Legacy/corrupt entry (e.g. 1970 timestamps) — drop it and refetch.
        print(f"  [BACKTEST] stale/corrupt cache for {symbol} {timeframe} "
              f"{start}→{end} — refetching", flush=True)
        with _bt_lock:
            _bt_cache.pop(key, None)

    url = (
        f"{_POLY_BASE}/v2/aggs/ticker/{symbol}/range/1/{timeframe}"
        f"/{start}/{end}?adjusted=true&sort=asc&limit=50000"
    )
    pages: list[pd.DataFrame] = []
    page = 0
    while url:
        page += 1
        data = _poly_get(url)
        if not data:
            break
        df_page = _parse_aggs(data, symbol, timeframe)
        if df_page is not None:
            pages.append(df_page)
        url = data.get("next_url") or None   # _poly_get appends apiKey

    if not pages:
        print(f"  [BACKTEST] {symbol} {timeframe} {start}→{end}: 0 bars", flush=True)
        return None

    df = (
        pd.concat(pages)
        .drop_duplicates("datetime")
        .sort_values("datetime")
        .reset_index(drop=True)
    )
    with _bt_lock:
        _bt_cache[key] = _df_to_cols(df)
    _bt_save()
    print(f"  [BACKTEST] fetched {symbol} {timeframe} {start}→{end}: {len(df)} bars ({page} page(s))", flush=True)
    return df


# ── Load disk cache on import so a restart doesn't burn API calls ─────────
_load_cache()
