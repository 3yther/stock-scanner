# VALIDATION_REPORT.md — TJR look-ahead bias check (Freqtrade)

**Verdict: PASS — no look-ahead bias detected** in the ported TJR entry logic.
98 of 100 entry signals were re-tested on progressively hidden data; 0 entry
signals, 0 exit signals, and 0 indicators changed when future candles were
removed.

This is an **independent** check: the live bot's signals were re-implemented in
Freqtrade (`user_data/strategies/TjrValidation.py`) and run through
`freqtrade lookahead-analysis`, which re-runs the strategy on truncated data and
flags anything whose value depends on candles it shouldn't be able to see.

- Date run: 2026-06-17
- Freqtrade 2026.5.1, CCXT 4.5.59, Python 3.12.13 (isolated venv)
- Source ported: `stock-scanner/quant/tjr_strategy.py` @ commit `7be34bb`
- Data: Binance `BTC/USDT`, `ETH/USDT`, 5m, `20260101–20260616` (48,286 bars/pair)
- The live bot was **not modified** in this task.

---

## 1. TJR rules ported & line-level mapping

Entry signal = the conditions under which the live DCA bot makes a **TJR buy**:
a **bullish MSS** (`dca_bot.py:177–183`) or a **bullish FVG fill**
(`dca_bot.py:196–202`). Bearish MSS only skips the next interval buy in the live
bot (`dca_bot.py:189–193`), so it is intentionally not an entry.

| # | TJR rule | `tjr_strategy.py` lines | Ported in `TjrValidation.py` |
|---|---|---|---|
| 1 | Session windows (Asia 20:00–00:00; trading 03:00–13:00 UTC) | 24–27, 48–53 | module constants; `in_session` |
| 2 | ATR(14), SMA-of-TR | 72–80 | `_atr()` |
| 3 | Asia range (prev-day 20:00 → 00:00 high/low) | 56–61, 83–89 | `_asia_range()` |
| 4 | Liquidity sweep of Asia high/low, once per direction, in session | 193–215 | loop "Step 2" |
| 5 | Swing pivots (left=right=1, confirmed one bar later) | 92–101 | loop "swing pivot", `p = q-2` |
| 6 | MSS: first post-sweep close beyond most-recent swing | 114–138, 217–226 | loop "Step 3" (bullish branch) |
| 7 | FVG: 3-candle gap > 0.5×ATR(14) | 141–163, 228–234 | loop "Step 4" |
| 8 | FVG fill when price re-enters the zone | 236–241 | loop "Step 4b" |

Each ported block carries an inline comment citing the exact source lines.
Approximations are documented in `PORTING_NOTES.md` (intra-candle price point,
1-candle execution delay, synthetic exits, sizing/regime/SMT out of scope).

---

## 2. Full `lookahead-analysis` output

Run A — default sample (20 signals):

```
freqtrade lookahead-analysis --strategy TjrValidation \
  --pairs BTC/USDT ETH/USDT --timeframe 5m --timerange 20260101-20260616

INFO - Found targeted trade amount = 20 signals.
INFO - TjrValidation: no bias detected

 filename          strategy       has_bias  total_signals  biased_entry_signals  biased_exit_signals  biased_indicators
 TjrValidation.py  TjrValidation  No        20             0                     0
```

Run B — expanded sample (`--targeted-trade-amount 200 --minimum-trade-amount 50`),
i.e. essentially **every** available signal:

```
INFO - TjrValidation: no bias detected
INFO - Checking look ahead bias ... took 194 seconds.

 filename          strategy       has_bias  total_signals  biased_entry_signals  biased_exit_signals  biased_indicators
 TjrValidation.py  TjrValidation  No        98             0                     0
```

CSV export (`user_data/lookahead_tjr.csv`):

```
filename,strategy,has_bias,total_signals,biased_entry_signals,biased_exit_signals,biased_indicators
TjrValidation.py,TjrValidation,False,98,0,0,
```

### Backtest sanity check (it produces trades)

```
freqtrade backtesting --strategy TjrValidation --pairs BTC/USDT ETH/USDT \
  --timeframe 5m --timerange 20260101-20260616

Strategy   Trades  Win%   Tot Profit %
TjrValidation  100   50.0   -1.06%

ENTER TAG STATS
  tjr_mss   9 entries
  tjr_fvg  91 entries
```

Both entry paths fired and were exercised by the bias check (P&L is irrelevant
to this validation — the synthetic ROI/stoploss exits drive returns, not TJR).

---

## 3. Signals flagged as biased

**None.** No entry, exit, or indicator was flagged. No live-bot fix is required
on the basis of this check.

---

## 4. Honest caveats — what this does and does NOT prove

**What it supports:** The TJR entry logic is **causally expressible** — every
ported rule (ATR, Asia range, sweep, confirmed pivots, MSS, FVG detect/fill) can
be computed using only candles `≤ T`, and when implemented that way the bias tool
finds nothing that secretly reads the future. The pivot rule (the usual culprit,
since a swing needs the *next* bar to confirm) is handled correctly: the port
only consumes a pivot once its right-neighbour bar has closed (`p = q-2`),
mirroring `detect_mss`'s use of `find_pivot_highs(candles[:i])`.

**What it does NOT prove / cannot check:**
1. **The verdict is conditional on the port being faithful.** A pass means *"the
   logic as ported has no look-ahead."* I deliberately wrote the port causally to
   mirror the source; the tool would have caught an *accidental* future-read in
   the port (e.g. centered pivots, `shift(-1)` in a decision), but it cannot
   prove semantic identity with the live bot. The line-level mapping in §1 and
   `PORTING_NOTES.md` are the audit trail for that fidelity.
2. **Approximated rules.** The intra-candle price point (sweeps / FVG fills) and
   the 1-candle execution delay are model differences, not the live behaviour
   bar-for-bar. They don't introduce look-ahead, but they mean the *trade list*
   here is not identical to the live ledger.
3. **Out of scope.** Position **sizing** — regime scaling (`quant/regime.py`) and
   SMT confluence (`quant/smt_divergence.py`) — was not ported; sizing can't
   create entry-time look-ahead but was not exercised here. Exits are synthetic.
4. **Not tested:** the live data pipeline (`quant/crypto_data.py`), the DB/state
   reconstruction in `dca_bot`, alerting, or order execution. This check is purely
   about whether the **signal logic** peeks at future candles.
5. **`lookahead-analysis` scope.** The tool checks entry/exit *signals* (and
   indicators that affect them) by hiding future data around each signal. It is
   not a general proof of correctness, and it samples signals (here, 98/100).
6. **Single window / venue.** One ~5.5-month window of Binance USDT candles.
   Re-running on other windows would add confidence (the logic is causal, so the
   result should be stable, but it wasn't exhaustively swept).

### Related but separate: recursive-formula bias
`lookahead-analysis` does not catch *recursive* indicator drift (warm-up
sensitivity). The ported indicators (SMA-based ATR, rolling max/min) are
non-recursive, so this is low-risk, but a `freqtrade recursive-analysis` run was
not performed and could be added if desired.

---

## 5. Live-bot findings

**No look-ahead finding to escalate.** Nothing flagged maps back to a bug in
`quant/tjr_strategy.py`. The audited rules are causal as written.

One **observation** (not a bias bug), for awareness when reading live results:
the live bot acts on the in-progress candle's `live_price` for sweeps and FVG
fills (`tjr_strategy.py:205–215, 238`). That is legitimate in live trading (the
price is *now*), but it means a naive *backtest* that used the **closed** candle's
high/low to fill at that same candle's close could overstate fills. The live code
is fine; the note is a guard-rail for anyone building a backtest of it. No change
to `quant/tjr_strategy.py` is warranted from this validation.
