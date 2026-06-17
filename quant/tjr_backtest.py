"""
tjr_backtest.py — read-only TJR backtester / tuning lab for BTC/ETH.

Single run  : one parameter set → standard-vs-TJR DCA over the range, plus a
              TRAIN→VALIDATION split when a split date is given.
Sweep run   : vary ONE parameter across its range; one row per value, sorted,
              best Sharpe highlighted (anti-overfit caption included).

Tunable params: symbols, DCA interval, FVG ATR threshold, MSS/FVG multipliers,
base buy USD, regime filter (Phase A). Uses the SAME causal TJR detectors as the
live strategy (no-lookahead). NEVER writes the live tjr_buys ledger — all in
memory. Runs in a background thread, polled via get_status().
"""

import threading
import time
from bisect import bisect_right as _bisect_right
from datetime import datetime, timezone, timedelta

import config
from quant import crypto_data, tjr_strategy as T, regime as RG, smt_divergence as SMT

UTC = timezone.utc
INITIAL  = 1000.0
ALL_SYMBOLS = ["BTC", "ETH"]

# Sweep ranges (UI picks which single param to vary).
SWEEP_RANGES = {
    "interval_hours": [1, 2, 4, 6, 8, 12],
    "fvg_atr":        [0.3, 0.4, 0.5, 0.6, 0.7, 0.8, 0.9, 1.0],
    "mss_mult":       [1.5, 2.0, 2.5, 3.0],
    "fvg_mult":       [1.0, 1.5, 2.0, 2.5],
    "base_usd":       [5, 10, 20, 50],
}

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


# ── Param defaults / validation ────────────────────────────────────────────

def _clean_params(p: dict) -> dict:
    syms = p.get("symbols") or ["BTC", "ETH"]
    syms = [s for s in ALL_SYMBOLS if s in syms] or ["BTC", "ETH"]
    def num(k, d, lo, hi):
        try:
            return max(lo, min(float(p.get(k, d)), hi))
        except (TypeError, ValueError):
            return d
    return {
        "mode":          "sweep" if p.get("mode") == "sweep" else "single",
        "start":         p.get("start"), "end": p.get("end"),
        "split_date":    (p.get("split_date") or None),
        "symbols":       syms,
        "interval_hours": int(num("interval_hours", 6, 1, 24)),
        "fvg_atr":       num("fvg_atr", 0.5, 0.1, 2.0),
        "mss_mult":      num("mss_mult", 2.5, 1.0, 5.0),
        "fvg_mult":      num("fvg_mult", 1.5, 1.0, 5.0),
        "base_usd":      num("base_usd", 10.0, 1.0, 1000.0),
        "regime_filter": bool(p.get("regime_filter", True)),
        "smt_enabled":   bool(p.get("smt_enabled", True)),
        "sweep_param":   p.get("sweep_param") if p.get("sweep_param") in SWEEP_RANGES else "fvg_atr",
    }


def start(params: dict) -> bool:
    if is_running():
        return False
    _set(status="fetching", progress=0.0, message="Starting…", result=None,
         error=None, started_at=time.time(), finished_at=None)
    threading.Thread(target=_run, args=(_clean_params(params),), daemon=True,
                     name="tjr-backtest").start()
    return True


# ── Regime timeline (causal 1h) ────────────────────────────────────────────

def _build_regime_lookup(symbol, sim_start_ts, end_ts):
    fetch = int(sim_start_ts - 14 * 86400)        # SMA200 on 1h needs ~9 days warmup
    c = crypto_data.get_candles_range(symbol, "1h", fetch, end_ts)
    need = 200 + 20 + 1
    tslist, reglist = [], []
    for j in range(need, len(c) + 1):
        tslist.append(c[j - 1]["timestamp"])
        reglist.append(RG.classify(c[:j])["regime"])
    return tslist, reglist


def _reg_at(reg_lookup, sym, ts):
    if not reg_lookup or sym not in reg_lookup:
        return "UNKNOWN"
    tslist, reglist = reg_lookup[sym]
    i = _bisect_right(tslist, ts) - 1
    return reglist[i] if i >= 0 else "UNKNOWN"


# ── Causal signal detection ────────────────────────────────────────────────

def _detect_events(candles, start_ts, end_ts, fvg_atr=0.5):
    events, counts = {}, {"mss_bull": 0, "mss_bear": 0, "fvg_fill": 0}
    sessions, active = {}, []
    n = len(candles)
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

        if i >= 2:
            a = (sum(trs[i - 13:i + 1]) / 14) if i >= 14 else None
            if a:
                c0, c2 = candles[i], candles[i - 2]
                if c0["low"] > c2["high"] and (c0["low"] - c2["high"]) > fvg_atr * a:
                    active.append({"top": c0["low"], "bottom": c2["high"], "dir": "bull", "filled": False})
                elif c0["high"] < c2["low"] and (c2["low"] - c0["high"]) > fvg_atr * a:
                    active.append({"top": c2["low"], "bottom": c0["high"], "dir": "bear", "filled": False})
                if len(active) > 40:
                    del active[:-40]
        for f in active:
            if not f["filled"] and f["bottom"] <= price <= f["top"]:
                f["filled"] = True; counts["fvg_fill"] += 1
                ev.append("fvg_fill_" + f["dir"])

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


# ── Simulation (both DCA variants) ─────────────────────────────────────────

def _new_pf(symbols):
    return {"cash": INITIAL, "qty": {s: 0.0 for s in symbols}, "invested": 0.0,
            "lots": [], "buys": [], "skip": {s: False for s in symbols},
            "next_iv": None, "eq": [], "last_day": None}


def _buy(pf, sym, usd, price, btype, mult, ts, regime_=None, smt_state="none", smt_mult=1.0):
    usd = min(usd, pf["cash"])
    if usd < 0.01 or price <= 0:
        return
    q = usd / price
    pf["cash"] -= usd; pf["qty"][sym] += q; pf["invested"] += usd
    pf["lots"].append({"sym": sym, "price": price, "qty": q})
    pf["buys"].append({"timestamp": T._iso(ts), "symbol": sym, "type": btype,
                       "usd_amount": round(usd, 2), "price": round(price, 2),
                       "multiplier": round(mult, 3), "regime": regime_,
                       "smt_state": smt_state, "smt_applied_mult": round(smt_mult, 3)})


def _eff(regime, mss_mult, fvg_mult, reg_filter):
    """Effective multipliers given the regime (Phase A policy applied on top of
    the tunable base multipliers)."""
    if not reg_filter or regime in ("TRENDING_UP", "UNKNOWN"):
        return {"mss": mss_mult, "fvg": fvg_mult, "interval": 1.0, "mss_on": True, "fvg_on": True}
    if regime == "CHOPPY":
        return {"mss": 0.0, "fvg": min(fvg_mult, 1.25), "interval": 1.0, "mss_on": False, "fvg_on": True}
    if regime == "TRENDING_DOWN":
        return {"mss": 0.0, "fvg": 0.0, "interval": 0.5, "mss_on": False, "fvg_on": False}
    return {"mss": mss_mult, "fvg": fvg_mult, "interval": 1.0, "mss_on": True, "fvg_on": True}


def _simulate(prices, ev_by_sym, start_ts, end_ts, symbols, p, reg_lookup, smt_lookup=None):
    iv = p["interval_hours"] * 3600
    base, mssm, fvgm, rf = p["base_usd"], p["mss_mult"], p["fvg_mult"], p["regime_filter"]
    smt_on = p.get("smt_enabled", True)
    timeline = sorted({ts for s in symbols for ts in prices[s] if start_ts <= ts <= end_ts})
    if not timeline:
        return None
    std, tjr = _new_pf(symbols), _new_pf(symbols)
    std["next_iv"] = tjr["next_iv"] = timeline[0]
    last = {s: 0.0 for s in symbols}

    def eff(sym, ts):
        return _eff(_reg_at(reg_lookup, sym, ts) if rf else "UNKNOWN", mssm, fvgm, rf)

    def smt_for(ts):
        """SMT state + confluence multiplier (for bullish buys) at time `ts`."""
        state = SMT.smt_at(smt_lookup, ts) if (smt_lookup and smt_on) else "none"
        return state, SMT.confluence_mult(state, "bull", enabled=smt_on)

    for ts in timeline:
        for s in symbols:
            if ts in prices[s]:
                last[s] = prices[s][ts]
        for s in symbols:
            for et in ev_by_sym[s].get(ts, []):
                pol = eff(s, ts); reg = _reg_at(reg_lookup, s, ts) if rf else None
                pp = prices[s].get(ts, last[s])
                if et == "mss_bull" and pol["mss_on"] and pol["mss"] > 0:
                    smt_state, sm = smt_for(ts)
                    _buy(tjr, s, base * pol["mss"] * sm, pp, "tjr_mss", pol["mss"], ts, reg, smt_state, sm)
                elif et == "mss_bear":
                    tjr["skip"][s] = True
                elif et == "fvg_fill_bull" and pol["fvg_on"] and pol["fvg"] > 0:
                    smt_state, sm = smt_for(ts)
                    _buy(tjr, s, base * pol["fvg"] * sm, pp, "tjr_fvg", pol["fvg"], ts, reg, smt_state, sm)
        while std["next_iv"] is not None and ts >= std["next_iv"]:
            for s in symbols:
                _buy(std, s, base, last[s], "dca_interval", 1.0, std["next_iv"])
            std["next_iv"] += iv
        while tjr["next_iv"] is not None and ts >= tjr["next_iv"]:
            for s in symbols:
                if tjr["skip"][s]:
                    tjr["skip"][s] = False
                else:
                    pol = eff(s, tjr["next_iv"]); reg = _reg_at(reg_lookup, s, tjr["next_iv"]) if rf else None
                    if pol["interval"] > 0:
                        _buy(tjr, s, base * pol["interval"], last[s], "dca_interval", pol["interval"], tjr["next_iv"], reg)
            tjr["next_iv"] += iv
        day = T._dt(ts).date().isoformat()
        for pf in (std, tjr):
            if pf["last_day"] != day:
                val = sum(pf["qty"][s] * last[s] for s in symbols)
                pf["eq"].append({"date": day, "equity": round(pf["cash"] + val, 2)})
                pf["last_day"] = day
    for pf in (std, tjr):
        val = sum(pf["qty"][s] * last[s] for s in symbols)
        if pf["eq"]:
            pf["eq"][-1]["equity"] = round(pf["cash"] + val, 2)
    return std, tjr, last


def _metrics(pf, last, symbols):
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
        "final_equity": round(final, 2), "total_return_pct": round((final / INITIAL - 1) * 100, 2),
        "invested": round(pf["invested"], 2),
        "holdings_value": round(sum(pf["qty"][s] * last[s] for s in symbols), 2),
        "num_buys": nb, "win_rate": round(wins / nb * 100, 1) if nb else 0.0,
        "profit_factor": (round(gw / gl, 2) if gl > 0 else (None if gw > 0 else 0.0)),
        "max_drawdown_pct": round(max_dd, 2), "sharpe": round(sharpe, 2),
        "equity_curve": eq, "drawdown_curve": dd, "buys": pf["buys"][-100:],
    }


def _backtest_window(candles_by_sym, reg_lookup, symbols, start_ts, end_ts, p, smt_lookup=None):
    prices, ev_by_sym, counts = {}, {}, {"mss_bull": 0, "mss_bear": 0, "fvg_fill": 0}
    for s in symbols:
        ev, cnt = _detect_events(candles_by_sym[s], start_ts, end_ts, p["fvg_atr"])
        ev_by_sym[s] = ev
        prices[s] = {c["timestamp"]: c["close"] for c in candles_by_sym[s]}
        for k in counts:
            counts[k] += cnt[k]
    sim = _simulate(prices, ev_by_sym, start_ts, end_ts, symbols, p, reg_lookup, smt_lookup)
    if sim is None:
        return None
    std, tjr, last = sim
    return {"signals": counts, "standard": _metrics(std, last, symbols), "tjr": _metrics(tjr, last, symbols)}


# ── Runner ─────────────────────────────────────────────────────────────────

def _run(p):
    try:
        symbols = p["symbols"]
        start_dt = datetime.strptime(p["start"], "%Y-%m-%d").replace(tzinfo=UTC)
        end_dt   = datetime.strptime(p["end"], "%Y-%m-%d").replace(tzinfo=UTC) + timedelta(days=1)
        start_ts, end_ts = int(start_dt.timestamp()), int(end_dt.timestamp())
        fetch_ts = int((start_dt - timedelta(days=2)).timestamp())

        candles_by_sym = {}
        for k, sym in enumerate(symbols):
            _set(status="fetching", progress=0.05 + 0.25 * k / len(symbols), message=f"Fetching {sym} 5m history…")
            cs = crypto_data.get_candles_range(sym, "5m", fetch_ts, end_ts)
            if not cs:
                _set(status="error", error=f"No {sym} candles for that range (5m history depth is limited — keep it recent).",
                     finished_at=time.time())
                return
            candles_by_sym[sym] = cs

        reg_lookup = {}
        if p["regime_filter"]:
            for k, sym in enumerate(symbols):
                _set(status="running", progress=0.32 + 0.12 * k / len(symbols), message=f"Building {sym} 1h regime…")
                reg_lookup[sym] = _build_regime_lookup(sym, start_ts, end_ts)

        # SMT divergence needs the BTC/ETH pair in lockstep — build a causal
        # state timeline once (no-lookahead) and reuse across all windows.
        smt_lookup = None
        if p["smt_enabled"] and "BTC" in candles_by_sym and "ETH" in candles_by_sym:
            _set(status="running", progress=0.46, message="Building BTC/ETH SMT timeline…")
            smt_lookup = SMT.build_smt_timeline(candles_by_sym["BTC"], candles_by_sym["ETH"],
                                                config.SMT_SWING_BARS)

        if p["mode"] == "sweep":
            sp = p["sweep_param"]; values = SWEEP_RANGES[sp]
            rows = []
            for i, v in enumerate(values):
                _set(status="running", progress=0.5 + 0.48 * i / len(values),
                     message=f"Sweep {sp}={v} ({i+1}/{len(values)})…")
                pv = {**p, sp: v}
                w = _backtest_window(candles_by_sym, reg_lookup, symbols, start_ts, end_ts, pv, smt_lookup)
                if not w:
                    continue
                m = w["tjr"]
                rows.append({"value": v, "total_return_pct": m["total_return_pct"], "sharpe": m["sharpe"],
                             "max_drawdown_pct": m["max_drawdown_pct"], "num_buys": m["num_buys"],
                             "win_rate": m["win_rate"]})
            best = max(rows, key=lambda r: r["sharpe"], default=None)
            result = {"mode": "sweep", "sweep_param": sp, "params": p, "rows": rows,
                      "best_sharpe_value": best["value"] if best else None,
                      "caption": "More variants tested = higher chance of a false winner. "
                                 "Confirm the top row on the validation split before trusting it."}
            _set(status="done", progress=1.0, result=result, finished_at=time.time(),
                 message=f"Sweep done — {len(rows)} variants of {sp}; best Sharpe @ {result['best_sharpe_value']}")
            return

        # single
        _set(status="running", progress=0.55, message="Simulating standard vs TJR DCA…")
        full = _backtest_window(candles_by_sym, reg_lookup, symbols, start_ts, end_ts, p, smt_lookup)
        if not full:
            _set(status="error", error="No candles inside the selected range.", finished_at=time.time())
            return
        result = {"mode": "single", "params": p, "regime_filter": p["regime_filter"],
                  "label": "TJR BACKTEST — read-only (never writes the live ledger)",
                  "signals": full["signals"], "standard": full["standard"], "tjr": full["tjr"]}

        if p["split_date"]:
            split_dt = datetime.strptime(p["split_date"][:10], "%Y-%m-%d").replace(tzinfo=UTC)
            split_ts = int(split_dt.timestamp())
            if start_ts < split_ts < end_ts:
                _set(status="running", progress=0.8, message="Train / validation split…")
                tr = _backtest_window(candles_by_sym, reg_lookup, symbols, start_ts, split_ts, p, smt_lookup)
                va = _backtest_window(candles_by_sym, reg_lookup, symbols, split_ts, end_ts, p, smt_lookup)
                result["split_date"] = p["split_date"][:10]
                result["train"] = tr
                result["validation"] = va

        _set(status="done", progress=1.0, result=result, finished_at=time.time(),
             message=(f"Done — standard {full['standard']['total_return_pct']:+.1f}% "
                      f"vs TJR {full['tjr']['total_return_pct']:+.1f}%"))
    except Exception as exc:  # noqa: BLE001
        import traceback; traceback.print_exc()
        _set(status="error", error=f"{type(exc).__name__}: {exc}", finished_at=time.time())
