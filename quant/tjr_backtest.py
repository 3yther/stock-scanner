"""
tjr_backtest.py — read-only TJR backtester for BTC/ETH.

Mirrors the stock backtester's structure: a single run at a time in a background
thread, polled via get_status(). Replays historical 5m candles with the SAME
pure TJR detectors the live strategy uses (no-lookahead — only closed candles),
counts how often each signal would have fired, and simulates TWO DCA variants on
the identical signal stream:

  standard : interval-only DCA ($BASE every INTERVAL_HOURS into each symbol)
  tjr      : interval DCA + 2.5× on bullish MSS + 1.5× on bullish FVG fill,
             skipping the next interval after a bearish MSS

NEVER writes the live tjr_buys ledger — everything is in-memory.
"""

import threading
import time
from bisect import bisect_right as _bisect_right
from datetime import datetime, timezone, timedelta

from quant import crypto_data, tjr_strategy as T, regime as RG

UTC = timezone.utc

BASE_DCA_USD   = 10.0
INTERVAL_HOURS = 6
INITIAL        = 1000.0
SYMBOLS        = ["BTC", "ETH"]

_state = {"status": "idle", "progress": 0.0, "message": "", "result": None,
          "error": None, "started_at": None, "finished_at": None}
_lock = threading.Lock()


def _set(**kw):
    with _lock:
        _state.update(kw)


def get_status() -> dict:
    with _lock:
        return dict(_state)


def is_running() -> bool:
    with _lock:
        return _state["status"] in ("fetching", "running")


def start(start_date: str, end_date: str, regime_filter: bool = True) -> bool:
    if is_running():
        return False
    _set(status="fetching", progress=0.0, message="Starting…", result=None,
         error=None, started_at=time.time(), finished_at=None)
    threading.Thread(target=_run, args=(start_date, end_date, regime_filter), daemon=True,
                     name="tjr-backtest").start()
    return True


def _build_regime_lookup(symbol, sim_start_ts, end_ts):
    """Causal regime at each completed 1h bar (TRENDING_UP/DOWN/CHOPPY/UNKNOWN),
    for the backtest's regime filter. Returns (sorted ts list, regime list)."""
    fetch = int(sim_start_ts - 14 * 86400)        # SMA200 on 1h needs ~9 days of warmup
    c = crypto_data.get_candles_range(symbol, "1h", fetch, end_ts)
    need = 200 + 20 + 1
    tslist, reglist = [], []
    for j in range(need, len(c) + 1):
        tslist.append(c[j - 1]["timestamp"])
        reglist.append(RG.classify(c[:j])["regime"])
    return tslist, reglist


# ── Per-symbol signal detection (causal walk) ─────────────────────────────

def _detect_events(candles, start_ts, end_ts):
    """Walk a symbol's candles; return {ts: [event,…]} within [start,end] and
    aggregate signal counts. Events: mss_bull/mss_bear/fvg_fill_bull/
    fvg_fill_bear. Sweep+MSS are session-bound; FVGs form/fill 24/7."""
    events, counts = {}, {"mss_bull": 0, "mss_bear": 0, "fvg_fill": 0}
    sessions, active = {}, []
    n = len(candles)
    # Precompute true ranges → rolling ATR(14) in O(n) (same value T.atr returns).
    trs = [0.0] * n
    for i in range(1, n):
        h, l, pc = candles[i]["high"], candles[i]["low"], candles[i - 1]["close"]
        trs[i] = max(h - l, abs(h - pc), abs(l - pc))
    for i in range(n):
        c = candles[i]; ts = c["timestamp"]; dt = T._dt(ts); td = T.trading_date_for(dt)
        if td not in sessions:
            sessions[td] = {"asia": T.compute_asia_range(candles[:i + 1], td),
                            "swept": set(), "mss_done": False}
        S = sessions[td]; price = c["close"]; ev = []

        # FVG formation (24/7, causal ATR(14) over the trailing window)
        if i >= 2:
            a = (sum(trs[i - 13:i + 1]) / 14) if i >= 14 else None
            if a:
                c0, c2 = candles[i], candles[i - 2]
                if c0["low"] > c2["high"] and (c0["low"] - c2["high"]) > 0.5 * a:
                    active.append({"top": c0["low"], "bottom": c2["high"], "dir": "bull", "filled": False})
                elif c0["high"] < c2["low"] and (c2["low"] - c0["high"]) > 0.5 * a:
                    active.append({"top": c2["low"], "bottom": c0["high"], "dir": "bear", "filled": False})
                if len(active) > 40:
                    del active[:-40]
        # FVG fill (24/7)
        for f in active:
            if not f["filled"] and f["bottom"] <= price <= f["top"]:
                f["filled"] = True; counts["fvg_fill"] += 1
                ev.append("fvg_fill_" + f["dir"])

        # Sweep + MSS (session only)
        if S["asia"] and T.in_trading_session(dt):
            ah, al = S["asia"]
            if c["high"] > ah: S["swept"].add("high")
            if c["low"]  < al: S["swept"].add("low")
            if S["swept"] and not S["mss_done"]:
                ws, we = T._trading_window(td)
                prefix = [x for x in candles[:i + 1] if ws <= x["timestamp"] < we]
                mss = T.detect_mss(prefix, "low" if "low" in S["swept"] else "high")
                if mss:
                    S["mss_done"] = True
                    counts["mss_" + mss["direction"]] += 1
                    ev.append("mss_" + mss["direction"])

        if ev and start_ts <= ts <= end_ts:
            events[ts] = ev
    return events, counts


# ── DCA simulation over a merged timeline (both variants) ─────────────────

def _new_pf():
    return {"cash": INITIAL, "qty": {s: 0.0 for s in SYMBOLS}, "invested": 0.0,
            "lots": [], "buys": [], "skip": {s: False for s in SYMBOLS},
            "next_iv": None, "eq": [], "last_day": None}


def _buy(pf, sym, usd, price, btype, mult, ts, regime_=None):
    usd = min(usd, pf["cash"])
    if usd < 0.01 or price <= 0:
        return
    q = usd / price
    pf["cash"] -= usd; pf["qty"][sym] += q; pf["invested"] += usd
    pf["lots"].append({"sym": sym, "price": price, "qty": q})
    pf["buys"].append({"timestamp": T._iso(ts), "symbol": sym, "type": btype,
                       "usd_amount": round(usd, 2), "price": round(price, 2),
                       "multiplier": mult, "regime": regime_})


def _reg_at(reg_lookup, sym, ts):
    if not reg_lookup or sym not in reg_lookup:
        return "UNKNOWN"
    tslist, reglist = reg_lookup[sym]
    i = _bisect_right(tslist, ts) - 1
    return reglist[i] if i >= 0 else "UNKNOWN"


def _simulate(prices, ev_by_sym, start_ts, end_ts, reg_filter=False, reg_lookup=None):
    iv = INTERVAL_HOURS * 3600
    timeline = sorted({ts for s in SYMBOLS for ts in prices[s] if start_ts <= ts <= end_ts})
    if not timeline:
        return None
    std, tjr = _new_pf(), _new_pf()
    std["next_iv"] = tjr["next_iv"] = timeline[0]
    last = {s: 0.0 for s in SYMBOLS}

    def policy(sym, ts):
        return RG.sizing_policy(_reg_at(reg_lookup, sym, ts) if reg_filter else "UNKNOWN")

    for ts in timeline:
        for s in SYMBOLS:
            if ts in prices[s]:
                last[s] = prices[s][ts]
        # TJR-only event reactions (regime-gated when the filter is on)
        for s in SYMBOLS:
            for et in ev_by_sym[s].get(ts, []):
                p = prices[s].get(ts, last[s]); pol = policy(s, ts); reg = _reg_at(reg_lookup, s, ts) if reg_filter else None
                if et == "mss_bull" and pol["mss_enabled"] and pol["mss"] > 0:
                    _buy(tjr, s, BASE_DCA_USD * pol["mss"], p, "tjr_mss", pol["mss"], ts, reg)
                elif et == "mss_bear":
                    tjr["skip"][s] = True
                elif et == "fvg_fill_bull" and pol["fvg_enabled"] and pol["fvg"] > 0:
                    _buy(tjr, s, BASE_DCA_USD * pol["fvg"], p, "tjr_fvg", pol["fvg"], ts, reg)
        # standard interval DCA (regime never applies)
        while std["next_iv"] is not None and ts >= std["next_iv"]:
            for s in SYMBOLS:
                _buy(std, s, BASE_DCA_USD, last[s], "dca_interval", 1.0, std["next_iv"])
            std["next_iv"] += iv
        # tjr interval DCA (regime scales/pauses; bearish-MSS skip honoured)
        while tjr["next_iv"] is not None and ts >= tjr["next_iv"]:
            for s in SYMBOLS:
                if tjr["skip"][s]:
                    tjr["skip"][s] = False
                else:
                    pol = policy(s, tjr["next_iv"]); reg = _reg_at(reg_lookup, s, tjr["next_iv"]) if reg_filter else None
                    if pol["interval"] > 0:
                        _buy(tjr, s, BASE_DCA_USD * pol["interval"], last[s], "dca_interval", pol["interval"], tjr["next_iv"], reg)
            tjr["next_iv"] += iv
        # daily equity snapshot
        day = T._dt(ts).date().isoformat()
        for pf in (std, tjr):
            if pf["last_day"] != day:
                val = sum(pf["qty"][s] * last[s] for s in SYMBOLS)
                pf["eq"].append({"date": day, "equity": round(pf["cash"] + val, 2)})
                pf["last_day"] = day
    # finalise last point
    for pf in (std, tjr):
        val = sum(pf["qty"][s] * last[s] for s in SYMBOLS)
        if pf["eq"]:
            pf["eq"][-1]["equity"] = round(pf["cash"] + val, 2)
    return std, tjr, last


def _metrics(pf, last):
    eq = pf["eq"]
    final = eq[-1]["equity"] if eq else INITIAL
    wins = losses = gw = gl = 0
    for lot in pf["lots"]:
        pnl = (last[lot["sym"]] - lot["price"]) * lot["qty"]
        if pnl > 0: wins += 1; gw += pnl
        else:       losses += 1; gl += abs(pnl)
    nb = len(pf["lots"])
    peak, max_dd, dd = INITIAL, 0.0, []
    for pt in eq:
        peak = max(peak, pt["equity"]); d = (pt["equity"] / peak - 1) * 100
        max_dd = min(max_dd, d); dd.append({"date": pt["date"], "drawdown": round(d, 2)})
    rets = [eq[i]["equity"] / eq[i - 1]["equity"] - 1 for i in range(1, len(eq)) if eq[i - 1]["equity"] > 0]
    sharpe = 0.0
    if len(rets) > 1:
        m = sum(rets) / len(rets); var = sum((r - m) ** 2 for r in rets) / (len(rets) - 1); sd = var ** 0.5
        if sd > 0:
            sharpe = m / sd * (365 ** 0.5)
    return {
        "final_equity":     round(final, 2),
        "total_return_pct": round((final / INITIAL - 1) * 100, 2),
        "invested":         round(pf["invested"], 2),
        "holdings_value":   round(sum(pf["qty"][s] * last[s] for s in SYMBOLS), 2),
        "num_buys":         nb,
        "win_rate":         round(wins / nb * 100, 1) if nb else 0.0,
        "profit_factor":    (round(gw / gl, 2) if gl > 0 else (None if gw > 0 else 0.0)),
        "max_drawdown_pct": round(max_dd, 2),
        "sharpe":           round(sharpe, 2),
        "equity_curve":     eq,
        "drawdown_curve":   dd,
        "buys":             pf["buys"][-100:],
    }


def _run(start_date, end_date, regime_filter=True):
    try:
        start_dt = datetime.strptime(start_date, "%Y-%m-%d").replace(tzinfo=UTC)
        end_dt   = datetime.strptime(end_date, "%Y-%m-%d").replace(tzinfo=UTC) + timedelta(days=1)
        start_ts, end_ts = int(start_dt.timestamp()), int(end_dt.timestamp())
        fetch_ts = int((start_dt - timedelta(days=2)).timestamp())   # asia + ATR warmup

        prices, ev_by_sym, counts = {}, {}, {"mss_bull": 0, "mss_bear": 0, "fvg_fill": 0}
        for k, sym in enumerate(SYMBOLS):
            _set(status="fetching", progress=0.1 + 0.3 * k / len(SYMBOLS),
                 message=f"Fetching {sym} history…")
            candles = crypto_data.get_candles_range(sym, "5m", fetch_ts, end_ts)
            if not candles:
                _set(status="error", error=f"No {sym} candles for that range (try a recent range; "
                     f"5m history depth is limited).", finished_at=time.time())
                return
            _set(status="running", progress=0.5 + 0.2 * k / len(SYMBOLS),
                 message=f"Detecting {sym} TJR signals over {len(candles)} candles…")
            ev, cnt = _detect_events(candles, start_ts, end_ts)
            ev_by_sym[sym] = ev
            prices[sym] = {c["timestamp"]: c["close"] for c in candles}
            for kk in counts:
                counts[kk] += cnt[kk]

        reg_lookup = {}
        if regime_filter:
            for k, sym in enumerate(SYMBOLS):
                _set(status="running", progress=0.75 + 0.05 * k / len(SYMBOLS),
                     message=f"Building {sym} 1h regime timeline…")
                reg_lookup[sym] = _build_regime_lookup(sym, start_ts, end_ts)

        _set(status="running", progress=0.85, message="Simulating standard vs TJR DCA…")
        sim = _simulate(prices, ev_by_sym, start_ts, end_ts, reg_filter=regime_filter, reg_lookup=reg_lookup)
        if sim is None:
            _set(status="error", error="No candles inside the selected range.", finished_at=time.time())
            return
        std, tjr, last = sim
        result = {
            "label":   "TJR BACKTEST — Simulated, read-only (never writes the live ledger)",
            "params":  {"start": start_date, "end": end_date, "symbols": SYMBOLS,
                        "base_dca_usd": BASE_DCA_USD, "interval_hours": INTERVAL_HOURS,
                        "initial_balance": INITIAL, "regime_filter": regime_filter},
            "regime_filter": regime_filter,
            "signals": counts,
            "standard": _metrics(std, last),
            "tjr":      _metrics(tjr, last),
        }
        _set(status="done", progress=1.0, result=result, finished_at=time.time(),
             message=(f"Done — signals: {counts['mss_bull']}▲MSS / {counts['mss_bear']}▼MSS / "
                      f"{counts['fvg_fill']} FVG fills | standard {result['standard']['total_return_pct']:+.1f}% "
                      f"vs TJR {result['tjr']['total_return_pct']:+.1f}%"))
    except Exception as exc:  # noqa: BLE001
        import traceback; traceback.print_exc()
        _set(status="error", error=f"{type(exc).__name__}: {exc}", finished_at=time.time())
