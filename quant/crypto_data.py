"""
crypto_data.py — exchange-agnostic crypto OHLC fetcher for the NoisyEdge bot.

Public interface:
    get_candles(symbol, interval, limit) -> list[dict] ascending by time,
    each {timestamp(int, UTC seconds), open, high, low, close, volume}.

The rest of NoisyEdge never needs to know which exchange the data came from.

Default source is Coinbase Exchange's public API (api.exchange.coinbase.com),
which is NOT geo-fenced from cloud servers. Binance returns "Service unavailable
from a restricted location" for US/Railway IPs, so it's only safe to select when
you've confirmed the deploy IP isn't blocked (hit /api/crypto/datatest). Flip
SOURCE (env CRYPTO_SOURCE) to 'binance' or 'kraken' and the normalisation hides
the differences.

Disk cache mirrors the Polygon cache: a short TTL keeps the latest candle fresh
while a 24/7 loop polls, and requests are throttled + retried on rate limits.
"""

import json
import os
import threading
import time
from datetime import datetime, timezone, timedelta

import requests as _http

# ── Config ────────────────────────────────────────────────────────────────
SOURCE         = os.getenv("CRYPTO_SOURCE", "coinbase")   # coinbase | binance | kraken
_CACHE_FILE    = "/tmp/crypto_cache.json"
_CACHE_TTL     = 30.0          # seconds — keep the live candle fresh, avoid hammering
_HTTP_TIMEOUT  = 12
_MIN_REQ_GAP   = 0.25          # seconds between HTTP calls (politeness)
_RETRY_WAIT    = 2.0           # seconds to back off on 429/5xx
_MAX_RETRIES   = 3

# interval label → seconds
_INTERVAL_SECONDS = {"1m": 60, "5m": 300, "15m": 900, "1h": 3600,
                     "4h": 14400, "1d": 86400}

# symbol → per-source product code
_PRODUCTS = {
    "coinbase": {"BTC": "BTC-USD",  "ETH": "ETH-USD"},
    "binance":  {"BTC": "BTCUSDT",  "ETH": "ETHUSDT"},
    "kraken":   {"BTC": "XBTUSD",   "ETH": "ETHUSD"},
}
# Kraken returns its own canonical pair keys
_KRAKEN_RESULT_KEY = {"XBTUSD": "XXBTZUSD", "ETHUSD": "XETHZUSD"}

# ── State ─────────────────────────────────────────────────────────────────
_lock        = threading.Lock()      # one HTTP call at a time
_last_call   = 0.0
_cache: dict[str, dict] = {}          # key -> {"ts": float, "data": list}
_cache_loaded = False


# ── HTTP helper (throttled + retried) ─────────────────────────────────────

def _get(url: str, params: dict):
    """One rate-limited GET. Returns parsed JSON or raises."""
    global _last_call
    with _lock:
        for attempt in range(1, _MAX_RETRIES + 1):
            gap = time.time() - _last_call
            if gap < _MIN_REQ_GAP:
                time.sleep(_MIN_REQ_GAP - gap)
            r = _http.get(url, params=params, timeout=_HTTP_TIMEOUT,
                          headers={"User-Agent": "noisyedge/1.0"})
            _last_call = time.time()
            if r.status_code == 429 or r.status_code >= 500:
                if attempt < _MAX_RETRIES:
                    time.sleep(_RETRY_WAIT * attempt)
                    continue
            r.raise_for_status()
            return r.json()
    raise RuntimeError("unreachable")


# ── Disk cache ─────────────────────────────────────────────────────────────

def _cache_load():
    global _cache_loaded
    if _cache_loaded:
        return
    _cache_loaded = True
    if os.path.exists(_CACHE_FILE):
        try:
            with open(_CACHE_FILE) as f:
                _cache.update(json.load(f))
            print(f"  [CRYPTO] loaded {len(_cache)} cached series from {_CACHE_FILE}", flush=True)
        except Exception as exc:
            print(f"  [CRYPTO] cache load failed: {exc}", flush=True)


def _cache_save():
    try:
        tmp = _CACHE_FILE + ".tmp"
        with open(tmp, "w") as f:
            json.dump(_cache, f)
        os.replace(tmp, _CACHE_FILE)
    except Exception as exc:
        print(f"  [CRYPTO] cache save failed: {exc}", flush=True)


# ── Source-specific fetchers → normalised ascending list of dict ──────────

def _norm(ts, o, h, l, c, v):
    return {"timestamp": int(ts), "open": float(o), "high": float(h),
            "low": float(l), "close": float(c), "volume": float(v)}


def _coinbase(product: str, interval: str, limit: int):
    # Coinbase returns up to 300 rows [time, low, high, open, close, volume]
    # (descending). Page backwards with start/end for larger limits.
    gran = _INTERVAL_SECONDS[interval]
    raw = []
    end = datetime.now(timezone.utc)
    pages = 0
    while len(raw) < limit and pages < 25:
        pages += 1
        chunk = min(300, limit - len(raw))
        start = end - timedelta(seconds=chunk * gran)
        rows = _get(f"https://api.exchange.coinbase.com/products/{product}/candles",
                    {"granularity": gran, "start": start.isoformat(), "end": end.isoformat()})
        if not rows:
            break
        raw.extend(rows)
        end = start
        if len(rows) < chunk:
            break
    return [_norm(r[0], r[3], r[2], r[1], r[4], r[5]) for r in raw]


def _binance(product: str, interval: str, limit: int):
    rows = _get("https://api.binance.com/api/v3/klines",
                {"symbol": product, "interval": interval, "limit": min(limit, 1000)})
    # [openTime(ms), open, high, low, close, volume, ...]
    return [_norm(r[0] // 1000, r[1], r[2], r[3], r[4], r[5]) for r in rows]


def _kraken(product: str, interval: str, limit: int):
    minutes = _INTERVAL_SECONDS[interval] // 60
    data = _get("https://api.kraken.com/0/public/OHLC",
                {"pair": product, "interval": minutes})
    if data.get("error"):
        raise RuntimeError(f"kraken: {data['error']}")
    key = _KRAKEN_RESULT_KEY.get(product, product)
    rows = data.get("result", {}).get(key, [])
    # [time, open, high, low, close, vwap, volume, count]
    return [_norm(r[0], r[1], r[2], r[3], r[4], r[6]) for r in rows]


_FETCHERS = {"coinbase": _coinbase, "binance": _binance, "kraken": _kraken}


# ── Public interface ──────────────────────────────────────────────────────

def get_candles(symbol: str, interval: str = "5m", limit: int = 300) -> list[dict]:
    """Return up to `limit` normalised OHLC candles ascending by time.

    Source-agnostic. Cached on disk for _CACHE_TTL seconds; on a fetch error the
    last good (stale) data is returned so the bot degrades gracefully.
    """
    _cache_load()
    symbol = symbol.upper()
    if interval not in _INTERVAL_SECONDS:
        raise ValueError(f"unsupported interval {interval!r}")
    product = _PRODUCTS.get(SOURCE, {}).get(symbol)
    if not product:
        print(f"  [CRYPTO] no product for {symbol} on {SOURCE}", flush=True)
        return []

    key = f"{SOURCE}:{symbol}:{interval}:{limit}"
    now = time.time()
    entry = _cache.get(key)
    if entry and now - entry.get("ts", 0) < _CACHE_TTL:
        return entry["data"]

    try:
        data = _FETCHERS[SOURCE](product, interval, limit)
    except Exception as exc:
        print(f"  [CRYPTO] {symbol} {interval} fetch failed ({exc}); "
              f"serving {'stale cache' if entry else 'empty'}", flush=True)
        return entry["data"] if entry else []

    # de-dupe by timestamp, sort ascending, keep newest `limit`
    by_ts = {c["timestamp"]: c for c in data}
    data = sorted(by_ts.values(), key=lambda c: c["timestamp"])[-limit:]
    _cache[key] = {"ts": now, "data": data}
    _cache_save()
    return data


def source() -> str:
    """Which exchange is currently in use."""
    return SOURCE


def get_candles_range(symbol: str, interval: str, start_ts: int, end_ts: int) -> list[dict]:
    """Historical candles in [start_ts, end_ts] (epoch seconds), ascending. For
    the backtester. Cached on disk with a long TTL since historical bars are
    immutable. Pages backward within the window."""
    _cache_load()
    symbol = symbol.upper()
    if interval not in _INTERVAL_SECONDS:
        raise ValueError(f"unsupported interval {interval!r}")
    product = _PRODUCTS.get(SOURCE, {}).get(symbol)
    if not product:
        return []
    gran = _INTERVAL_SECONDS[interval]
    key  = f"{SOURCE}:{symbol}:{interval}:range:{int(start_ts)}:{int(end_ts)}"
    now  = time.time()
    entry = _cache.get(key)
    if entry and now - entry.get("ts", 0) < 86400:     # 1-day cache for history
        return entry["data"]

    raw = []
    end = float(end_ts)
    pages = 0
    while end > start_ts and pages < 200:
        pages += 1
        start = max(start_ts, end - 300 * gran)
        try:
            if SOURCE == "coinbase":
                rows = _get(f"https://api.exchange.coinbase.com/products/{product}/candles",
                            {"granularity": gran,
                             "start": datetime.fromtimestamp(start, timezone.utc).isoformat(),
                             "end":   datetime.fromtimestamp(end, timezone.utc).isoformat()})
                raw.extend(_norm(r[0], r[3], r[2], r[1], r[4], r[5]) for r in rows)
                got = len(rows)
            else:   # binance/kraken: fall back to recent fetch, filtered below
                rows = _FETCHERS[SOURCE](product, interval, 1000)
                raw.extend(rows); got = 0
        except Exception as exc:
            print(f"  [CRYPTO] range fetch {symbol} failed: {exc}", flush=True)
            break
        end = start
        if SOURCE != "coinbase" or got < 300:
            break

    by_ts = {c["timestamp"]: c for c in raw if start_ts <= c["timestamp"] <= end_ts}
    data = sorted(by_ts.values(), key=lambda c: c["timestamp"])
    _cache[key] = {"ts": now, "data": data}
    _cache_save()
    return data
