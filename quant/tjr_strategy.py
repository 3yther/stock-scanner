"""
tjr_strategy.py — TJR / ICT Smart Money Concepts for BTC & ETH (UTC, 24/7).

Pipeline (no-lookahead: structure uses only CLOSED candles):
  Step 1  Asia range  : 20:00–00:00 UTC high/low, stored per trading date.
  Step 2  Liquidity sweep : during 03:00–13:00 UTC, price beyond the Asia
          high/low (once per direction per session).
  Step 3  Market structure shift (MSS): after a sweep, a closed 5m candle that
          closes beyond the most recent confirmed swing (one MSS per session).
  Step 4  Fair value gap (FVG): 3-candle imbalance bigger than 0.5×ATR(14);
          marked filled when price trades back into the zone. Last 20 unfilled
          kept per symbol.

`process(symbol, candles, live_price, now)` runs the whole pipeline and persists
to crypto_db. The pure detectors below are unit-testable on their own.
"""

from datetime import datetime, timezone, timedelta, time as dtime

from quant import crypto_db

UTC = timezone.utc

# Session boundaries (UTC hours)
ASIA_START_H    = 20    # 20:00
TRADING_START_H = 3     # 03:00
TRADING_END_H   = 13    # 13:00

SYMBOLS = ["BTC", "ETH"]


# ── Time / session helpers ────────────────────────────────────────────────

def _dt(ts: int) -> datetime:
    return datetime.fromtimestamp(ts, UTC)


def _iso(ts: int) -> str:
    return _dt(ts).isoformat()


def trading_date_for(dt: datetime):
    """The trading-session date a moment belongs to (sessions are in the
    morning, so it's simply the calendar date)."""
    return dt.date()


def in_trading_session(dt: datetime) -> bool:
    return TRADING_START_H <= dt.hour < TRADING_END_H


def in_asia_session(dt: datetime) -> bool:
    return dt.hour >= ASIA_START_H   # 20:00–00:00


def _asia_window(td) -> tuple[float, float]:
    """Epoch [start, end) of the Asia session feeding trading date `td`:
    previous day 20:00 UTC → `td` 00:00 UTC."""
    start = datetime.combine(td - timedelta(days=1), dtime(ASIA_START_H), tzinfo=UTC)
    end   = datetime.combine(td, dtime(0), tzinfo=UTC)
    return start.timestamp(), end.timestamp()


def _trading_window(td) -> tuple[float, float]:
    start = datetime.combine(td, dtime(TRADING_START_H), tzinfo=UTC)
    end   = datetime.combine(td, dtime(TRADING_END_H), tzinfo=UTC)
    return start.timestamp(), end.timestamp()


# ── Pure indicators / detectors ───────────────────────────────────────────

def atr(candles: list[dict], period: int = 14):
    """ATR over `period` completed candles (causal). None if too few bars."""
    if len(candles) < period + 1:
        return None
    trs = []
    for i in range(1, len(candles)):
        h, l, pc = candles[i]["high"], candles[i]["low"], candles[i - 1]["close"]
        trs.append(max(h - l, abs(h - pc), abs(l - pc)))
    return sum(trs[-period:]) / period


def compute_asia_range(candles: list[dict], td):
    """(high, low) of the Asia window for trading date `td`, or None."""
    s, e = _asia_window(td)
    win = [c for c in candles if s <= c["timestamp"] < e]
    if not win:
        return None
    return max(c["high"] for c in win), min(c["low"] for c in win)


def find_pivot_highs(candles: list[dict], left: int = 1, right: int = 1):
    """Indices of confirmed swing highs (bar strictly higher than `left` bars
    before and `right` bars after — all closed, so no lookahead)."""
    out = []
    for i in range(left, len(candles) - right):
        hi = candles[i]["high"]
        if all(hi > candles[i - j]["high"] for j in range(1, left + 1)) and \
           all(hi > candles[i + j]["high"] for j in range(1, right + 1)):
            out.append(i)
    return out


def find_pivot_lows(candles: list[dict], left: int = 1, right: int = 1):
    out = []
    for i in range(left, len(candles) - right):
        lo = candles[i]["low"]
        if all(lo < candles[i - j]["low"] for j in range(1, left + 1)) and \
           all(lo < candles[i + j]["low"] for j in range(1, right + 1)):
            out.append(i)
    return out


def detect_mss(candles: list[dict], swept: str):
    """First market structure shift after a sweep. `candles` are CLOSED,
    ascending. Scans for the FIRST candle that closes beyond the most recent
    swing confirmed before it (one MSS per session). swept='low' → bullish MSS
    (close above the recent swing high); swept='high' → bearish MSS."""
    if len(candles) < 4:
        return None
    for i in range(2, len(candles)):
        if swept == "low":
            highs = find_pivot_highs(candles[:i])   # confirmed strictly before bar i
            if not highs:
                continue
            level = candles[highs[-1]]["high"]
            if candles[i]["close"] > level:
                return {"direction": "bull", "price": candles[i]["close"],
                        "swing_level": level, "timestamp": candles[i]["timestamp"]}
        elif swept == "high":
            lows = find_pivot_lows(candles[:i])
            if not lows:
                continue
            level = candles[lows[-1]]["low"]
            if candles[i]["close"] < level:
                return {"direction": "bear", "price": candles[i]["close"],
                        "swing_level": level, "timestamp": candles[i]["timestamp"]}
    return None


def find_fvgs(candles: list[dict], atr_period: int = 14, atr_mult: float = 0.5):
    """All fair-value gaps in `candles` (ascending). A 3-candle window with the
    NEWEST candle as index [0]: bullish if c[0].low > c[2].high, bearish if
    c[0].high < c[2].low. Valid only if the gap > atr_mult×ATR(14) measured up to
    that window (causal). Returns dicts {timestamp(of forming candle), direction,
    top, bottom, gap}."""
    out = []
    for i in range(2, len(candles)):
        a = atr(candles[: i + 1], atr_period)
        if a is None:
            continue
        c0, c2 = candles[i], candles[i - 2]        # c0 newest, c2 oldest of triplet
        if c0["low"] > c2["high"]:
            gap = c0["low"] - c2["high"]
            if gap > atr_mult * a:
                out.append({"timestamp": c0["timestamp"], "direction": "bull",
                            "top": c0["low"], "bottom": c2["high"], "gap": gap})
        elif c0["high"] < c2["low"]:
            gap = c2["low"] - c0["high"]
            if gap > atr_mult * a:
                out.append({"timestamp": c0["timestamp"], "direction": "bear",
                            "top": c2["low"], "bottom": c0["high"], "gap": gap})
    return out


# ── Orchestrator: detect + persist ────────────────────────────────────────

def process(symbol: str, candles: list[dict], live_price: float | None = None,
            now: datetime | None = None) -> dict:
    """Run the full TJR pipeline for one symbol on its recent 5m candles and
    persist to crypto_db. Returns a summary of what fired this cycle."""
    now = now or datetime.now(UTC)
    td = trading_date_for(now)
    td_str = td.isoformat()
    result = {"symbol": symbol, "trading_date": td_str, "in_session": in_trading_session(now),
              "price": None, "asia": None, "sweep": None, "mss": None,
              "new_fvgs": [], "filled_fvgs": []}
    if not candles:
        return result

    price = float(live_price) if live_price is not None else float(candles[-1]["close"])
    result["price"] = price
    # Structure uses only closed candles — drop the in-progress (last) candle.
    closed = candles[:-1] if len(candles) > 1 else candles

    # Step 1 — Asia range for this trading date.
    ar = compute_asia_range(candles, td)
    if ar:
        crypto_db.save_asia_range(symbol, td_str, round(ar[0], 8), round(ar[1], 8))
    asia = crypto_db.get_asia_range(symbol, td_str)
    result["asia"] = asia

    # Step 2 — Liquidity sweep (trading session only, once per direction). The
    # live price OR any closed session candle breaching the level counts (robust
    # to a polling loop that didn't tick on the exact breaching minute).
    swept = set(s["direction"] for s in crypto_db.get_sweeps_on(symbol, td_str))
    if asia and in_trading_session(now):
        s_ts, e_ts = _trading_window(td)
        sess = [c for c in closed if s_ts <= c["timestamp"] < e_ts and c["timestamp"] <= now.timestamp()]
        if "high" not in swept:
            brk = next((c for c in sess if c["high"] > asia["asia_high"]), None)
            if brk:
                crypto_db.log_sweep(symbol, _iso(brk["timestamp"]), "high", brk["high"], asia["asia_high"])
                swept.add("high"); result["sweep"] = "high"
            elif price > asia["asia_high"]:
                crypto_db.log_sweep(symbol, now.isoformat(), "high", price, asia["asia_high"])
                swept.add("high"); result["sweep"] = "high"
        if "low" not in swept:
            brk = next((c for c in sess if c["low"] < asia["asia_low"]), None)
            if brk:
                crypto_db.log_sweep(symbol, _iso(brk["timestamp"]), "low", brk["low"], asia["asia_low"])
                swept.add("low"); result["sweep"] = "low"
            elif price < asia["asia_low"]:
                crypto_db.log_sweep(symbol, now.isoformat(), "low", price, asia["asia_low"])
                swept.add("low"); result["sweep"] = "low"

    # Step 3 — MSS (after a sweep, once per session).
    if swept and not crypto_db.get_mss_on(symbol, td_str):
        s, e = _trading_window(td)
        sess = [c for c in closed if s <= c["timestamp"] < e]
        swept_dir = "low" if "low" in swept else "high"
        mss = detect_mss(sess, swept_dir)
        if mss:
            crypto_db.log_mss(symbol, _iso(mss["timestamp"]), mss["direction"],
                              round(mss["price"], 8), round(mss["swing_level"], 8))
            result["mss"] = {**mss, "timestamp": _iso(mss["timestamp"])}

    # Step 4 — FVGs over recent closed candles (dedup by forming timestamp).
    for f in find_fvgs(closed[-60:]):
        iso = _iso(f["timestamp"])
        if not crypto_db.fvg_exists(symbol, iso):
            crypto_db.log_fvg(symbol, iso, f["direction"],
                              round(f["top"], 8), round(f["bottom"], 8))
            result["new_fvgs"].append({**f, "timestamp": iso})

    # Step 4b — mark fills when price trades back into an unfilled zone.
    for fvg in crypto_db.get_active_fvgs(symbol, 20):
        if fvg["bottom_price"] <= price <= fvg["top_price"]:
            crypto_db.mark_fvg_filled(fvg["id"], now.isoformat())
            result["filled_fvgs"].append(fvg["id"])

    return result
