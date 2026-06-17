"""
smt_divergence.py — SMT (Smart Money Technique) divergence on BTC vs ETH.

SMT divergence compares two correlated assets at their swings. When they
DISAGREE, smart-money activity is implied:

  Bullish SMT : one asset takes out its prior swing LOW (lower low) while the
                other HOLDS (higher / equal low) → accumulation, bias up.
  Bearish SMT : one asset takes out its prior swing HIGH (higher high) while the
                other FAILS (lower / equal high) → distribution, bias down.

Pure, no-lookahead detectors (only CONFIRMED swing pivots, reusing the exact
pivot logic that drives MSS in tjr_strategy). The detector takes the two candle
series and returns the current SMT state; sizing is ADDITIVE — SMT never gates a
buy, it only scales it (confluence_mult).

`update()` is the live orchestrator (detect + persist to tjr_smt).
`build_smt_timeline()` is the backtest helper: a causal state timeline so a
replay can look up the SMT state at any timestamp using only candles ≤ that time.
"""

from datetime import datetime, timezone

import config
from quant import tjr_strategy as T   # reuse find_pivot_highs / find_pivot_lows (MSS swings)

UTC = timezone.utc

NEUTRAL = {"type": "none", "btc_swing": None, "eth_swing": None, "timestamp": None}


# ── Pure detector (no DB, no-lookahead) ───────────────────────────────────

def detect_smt(btc_candles: list[dict], eth_candles: list[dict],
               swing_bars: int = 1) -> dict:
    """Current SMT state from two correlated candle series.

    Uses only CONFIRMED swing pivots (a pivot needs `swing_bars` closed candles
    on each side), so there is no lookahead. Compares each asset's latest two
    confirmed swing lows (for bullish SMT) and highs (for bearish SMT):

      bullish : exactly one asset made a LOWER low than its prior swing low
                (the other held) — a divergence at the lows.
      bearish : exactly one asset made a HIGHER high than its prior swing high
                (the other failed) — a divergence at the highs.

    When both a low- and a high-divergence are present, the one anchored at the
    MORE RECENT confirmed swing wins (it reflects the current state). Returns
    {"type": bullish|bearish|none, "btc_swing", "eth_swing", "timestamp"}.
    """
    if len(btc_candles) < 3 or len(eth_candles) < 3:
        return dict(NEUTRAL)

    bl = T.find_pivot_lows(btc_candles, swing_bars, swing_bars)
    el = T.find_pivot_lows(eth_candles, swing_bars, swing_bars)
    bh = T.find_pivot_highs(btc_candles, swing_bars, swing_bars)
    eh = T.find_pivot_highs(eth_candles, swing_bars, swing_bars)

    low_sig = None
    if len(bl) >= 2 and len(el) >= 2:
        b_now, b_prev = btc_candles[bl[-1]]["low"], btc_candles[bl[-2]]["low"]
        e_now, e_prev = eth_candles[el[-1]]["low"], eth_candles[el[-2]]["low"]
        # exactly one asset took out its prior swing low → divergence
        if (b_now < b_prev) != (e_now < e_prev):
            low_sig = {"type": "bullish", "btc_swing": b_now, "eth_swing": e_now,
                       "timestamp": max(btc_candles[bl[-1]]["timestamp"],
                                        eth_candles[el[-1]]["timestamp"])}

    high_sig = None
    if len(bh) >= 2 and len(eh) >= 2:
        b_now, b_prev = btc_candles[bh[-1]]["high"], btc_candles[bh[-2]]["high"]
        e_now, e_prev = eth_candles[eh[-1]]["high"], eth_candles[eh[-2]]["high"]
        # exactly one asset took out its prior swing high → divergence
        if (b_now > b_prev) != (e_now > e_prev):
            high_sig = {"type": "bearish", "btc_swing": b_now, "eth_swing": e_now,
                        "timestamp": max(btc_candles[bh[-1]]["timestamp"],
                                         eth_candles[eh[-1]]["timestamp"])}

    if low_sig and high_sig:
        return low_sig if low_sig["timestamp"] >= high_sig["timestamp"] else high_sig
    return low_sig or high_sig or dict(NEUTRAL)


# ── Sizing effect (additive, never a gate) ────────────────────────────────

def confluence_mult(smt_type: str | None, buy_direction: str,
                    enabled: bool | None = None) -> float:
    """Extra multiplier for a buy given the SMT state and the buy's direction
    ('bull' / 'bear'). SMT agreeing with the buy → SMT_CONFLUENCE_MULT (boost);
    disagreeing → SMT_DISAGREE_MULT (reduce, NOT block); neutral / disabled → 1.0.
    `enabled` overrides config.SMT_ENABLED (used by the backtester's per-run flag).
    """
    if enabled is None:
        enabled = config.SMT_ENABLED
    if not enabled or smt_type in (None, "none"):
        return 1.0
    bias = "bull" if smt_type == "bullish" else "bear"
    if bias == buy_direction:
        return config.SMT_CONFLUENCE_MULT
    return config.SMT_DISAGREE_MULT


# ── Live orchestrator (detect + persist) ──────────────────────────────────

def update(btc_candles: list[dict], eth_candles: list[dict],
           now: datetime | None = None, persist: bool = True,
           swing_bars: int | None = None) -> dict:
    """Compute the current BTC/ETH SMT state from recent 5m candles and persist
    it to tjr_smt. Mirrors tjr_strategy: structure uses only CLOSED candles, so
    the in-progress (last) candle is dropped before detection. Returns the
    detect_smt dict."""
    bars = config.SMT_SWING_BARS if swing_bars is None else swing_bars
    btc = btc_candles[:-1] if len(btc_candles) > 1 else btc_candles
    eth = eth_candles[:-1] if len(eth_candles) > 1 else eth_candles
    sig = detect_smt(btc, eth, bars)
    if persist:
        from quant import crypto_db   # lazy: keeps the pure detectors DB-free
        ts = (now or datetime.now(UTC)).isoformat()
        crypto_db.log_smt(ts, sig["type"], sig["btc_swing"], sig["eth_swing"])
    return sig


# ── Backtest helper: causal SMT state timeline ────────────────────────────

def build_smt_timeline(btc_candles: list[dict], eth_candles: list[dict],
                       swing_bars: int = 1):
    """Causal SMT timeline for a backtest replay. A confirmed pivot at index i
    only becomes known once candle i+swing_bars has closed, so the state is
    (re)evaluated at each such confirmation timestamp using ONLY candles ≤ that
    time — identical, by construction, to the live detect_smt on the same prefix
    (no lookahead). Returns (tslist, statelist) for a bisect lookup via smt_at()."""
    conf_ts = set()
    for cs in (btc_candles, eth_candles):
        piv = (T.find_pivot_lows(cs, swing_bars, swing_bars)
               + T.find_pivot_highs(cs, swing_bars, swing_bars))
        for idx in piv:
            conf_ts.add(cs[idx + swing_bars]["timestamp"])
    tslist, statelist = [], []
    for ts in sorted(conf_ts):
        b = [c for c in btc_candles if c["timestamp"] <= ts]
        e = [c for c in eth_candles if c["timestamp"] <= ts]
        tslist.append(ts)
        statelist.append(detect_smt(b, e, swing_bars)["type"])
    return tslist, statelist


def smt_at(timeline, ts) -> str:
    """SMT state in effect at time `ts` from a build_smt_timeline() result."""
    from bisect import bisect_right
    if not timeline:
        return "none"
    tslist, statelist = timeline
    i = bisect_right(tslist, ts) - 1
    return statelist[i] if i >= 0 else "none"
