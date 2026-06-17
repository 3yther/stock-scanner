"""
test_smt_divergence.py — SMT (BTC/ETH) divergence tests.

Pure-logic tests (no network, no live ledger writes). Runnable two ways:
    python -m quant.test_smt_divergence      # standalone (prints PASS/FAIL)
    pytest quant/test_smt_divergence.py      # if pytest is installed

Covers: bullish SMT firing, confluence/conflict sizing, backtest↔live parity,
and the no-lookahead guarantee (an unconfirmed swing is never used).
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import config
from quant import smt_divergence as S

STEP = 300  # 5-minute candles
T0   = 1_700_000_000


def mk(lows, highs, t0=T0, step=STEP):
    """Build a candle series from parallel low/high lists (close = midpoint)."""
    assert len(lows) == len(highs)
    out = []
    for i, (lo, hi) in enumerate(zip(lows, highs)):
        c = (hi + lo) / 2.0
        out.append({"timestamp": t0 + i * step, "open": c, "high": float(hi),
                    "low": float(lo), "close": c, "volume": 1.0})
    return out


# Highs strictly increasing → no confirmed swing HIGH, so these fixtures isolate
# the LOW-side (bullish) divergence with no competing bearish signal.
_HIGHS = [20, 21, 22, 23, 24, 25]

# BTC carves a LOWER low (8 → 7); ETH carves a HIGHER low (7 → 8, it "holds").
_BTC_LOWS = [10, 8, 10, 9, 7, 9]
_ETH_LOWS = [10, 7, 10, 9, 8, 9]


def _bullish_pair():
    return mk(_BTC_LOWS, _HIGHS), mk(_ETH_LOWS, _HIGHS)


# ── Test 1: bullish SMT fires (BTC lower low, ETH holds) ──────────────────

def test_bullish_smt_fires():
    btc, eth = _bullish_pair()
    sig = S.detect_smt(btc, eth, swing_bars=1)
    assert sig["type"] == "bullish", sig
    # the involved swings: BTC's new lower low (7) vs ETH's held higher low (8)
    assert sig["btc_swing"] == 7 and sig["eth_swing"] == 8, sig


def test_bearish_smt_fires():
    # Mirror image on the highs: BTC carves a HIGHER high (21 → 24); ETH FAILS
    # (24 → 22, a lower high). Lows strictly DECREASING so there is no confirmed
    # swing low to compete.
    lows = [15, 14, 13, 12, 11, 10]
    btc = mk(lows, [20, 21, 20, 22, 24, 22])
    eth = mk(lows, [20, 24, 20, 22, 21, 22])
    sig = S.detect_smt(btc, eth, swing_bars=1)
    assert sig["type"] == "bearish", sig


# ── Test 2: confluence (1.3×) vs conflict (0.7×) sizing ───────────────────

def test_confluence_and_conflict_multipliers():
    # Agreeing SMT boosts, disagreeing reduces (never blocks), neutral/off = 1.0.
    assert S.confluence_mult("bullish", "bull", enabled=True) == config.SMT_CONFLUENCE_MULT
    assert S.confluence_mult("bearish", "bull", enabled=True) == config.SMT_DISAGREE_MULT
    assert S.confluence_mult("none",    "bull", enabled=True) == 1.0
    assert S.confluence_mult("bullish", "bull", enabled=False) == 1.0
    # Defaults from config: confluence boosts, conflict reduces.
    assert config.SMT_CONFLUENCE_MULT == 1.3
    assert config.SMT_DISAGREE_MULT == 0.7


def test_fvg_buy_sizing_bump_and_cut():
    # A bullish FVG buy: base 10 × fvg 1.5. SMT bullish → ×1.3; SMT bearish → ×0.7.
    base, fvg_mult = 10.0, 1.5
    confluent = base * fvg_mult * S.confluence_mult("bullish", "bull", enabled=True)
    conflict  = base * fvg_mult * S.confluence_mult("bearish", "bull", enabled=True)
    assert round(confluent, 4) == round(10.0 * 1.5 * 1.3, 4)   # 19.5
    assert round(conflict,  4) == round(10.0 * 1.5 * 0.7, 4)   # 10.5
    assert confluent > base * fvg_mult > conflict


# ── Test 3: backtest SMT timeline matches live (pure) logic ───────────────

def test_backtest_matches_live():
    btc, eth = _bullish_pair()
    timeline = S.build_smt_timeline(btc, eth, swing_bars=1)
    # By construction each timeline state is detect_smt over candles ≤ its ts.
    tslist, statelist = timeline
    for ts, state in zip(tslist, statelist):
        b = [c for c in btc if c["timestamp"] <= ts]
        e = [c for c in eth if c["timestamp"] <= ts]
        assert S.smt_at(timeline, ts) == state
        assert S.detect_smt(b, e, 1)["type"] == state
    # Final state equals the live detector over the full series (no candle after
    # the last confirmation can change an already-confirmed pivot).
    assert statelist[-1] == "bullish"
    assert S.detect_smt(btc, eth, 1)["type"] == "bullish"


# ── Test 4: no-lookahead — SMT at T uses only candles ≤ T ──────────────────

def test_no_lookahead():
    btc, eth = _bullish_pair()
    # Drop the final candle (idx 5): BTC's lower low at idx 4 now has no RIGHT
    # neighbour, so it is NOT yet a confirmed swing → only one swing low each →
    # no divergence. Adding the candle back confirms it → bullish.
    assert S.detect_smt(btc[:5], eth[:5], 1)["type"] == "none"
    assert S.detect_smt(btc, eth, 1)["type"] == "bullish"

    # Same guarantee through the timeline: the bullish state only appears at the
    # confirming candle's timestamp (idx 5), never at idx 4.
    timeline = S.build_smt_timeline(btc, eth, 1)
    assert S.smt_at(timeline, btc[4]["timestamp"]) == "none"
    assert S.smt_at(timeline, btc[5]["timestamp"]) == "bullish"


# ── Standalone runner (no pytest dependency) ──────────────────────────────

def _main():
    tests = [v for k, v in sorted(globals().items())
             if k.startswith("test_") and callable(v)]
    failed = 0
    for t in tests:
        try:
            t()
            print(f"  PASS  {t.__name__}")
        except AssertionError as exc:
            failed += 1
            print(f"  FAIL  {t.__name__}: {exc}")
        except Exception as exc:  # noqa: BLE001
            failed += 1
            print(f"  ERROR {t.__name__}: {type(exc).__name__}: {exc}")
    print(f"\n{len(tests) - failed}/{len(tests)} passed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(_main())
