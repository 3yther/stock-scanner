# PORTING_NOTES.md — TJR → Freqtrade `TjrValidation`

Honest record of how the live TJR logic was mapped onto Freqtrade's model, and
every place the two differ. The goal of the port is **bias-checking the logic**,
not reproducing the live bot's P&L.

Source of truth: `stock-scanner/quant/tjr_strategy.py`
(last commit `7be34bb` "phase 4: paper DCA bot with TJR signal integration",
243 lines). The live bot wraps it in `quant/dca_bot.py` (`_cycle` → `process`).

---

## Design decision: causal loop, not vectorization

The live signal generator is a **candle-by-candle loop**: `dca_bot._cycle()` runs
every ~60 s, calls `tjr_strategy.process(symbol, candles, …)`, which recomputes
structure from the recent candle window each time. Several rules are genuinely
**path-dependent** (sweep-state per session, "first MSS per session", the rolling
active-FVG list and its fills).

Rather than vectorize those (which risks *introducing* look-ahead that wasn't in
the original), `populate_indicators()` ports them as an **explicit forward loop**
over the dataframe that only ever reads rows `≤ i`. Cleanly-causal scalars (ATR,
the Asia range) are precomputed with pandas because they cannot leak the future
by construction. This makes the port auditable line-for-line against the source
and removes ambiguity about whether the *port* added bias.

---

## Rule-by-rule mapping & fidelity

| TJR rule | Live source (`tjr_strategy.py`) | Port (`TjrValidation.py`) | Fidelity |
|---|---|---|---|
| Session hours (Asia 20:00, trading 03:00–13:00 UTC) | L24–27, `in_trading_session` L48–49, `in_asia_session` L52–53 | module consts + `in_session` | exact |
| ATR(14) = SMA of TR | `atr()` L72–80 | `_atr()` | exact (both are a simple mean of the last 14 TRs, **not** Wilder) |
| Asia range (prev-day 20:00→00:00 hi/lo) | `_asia_window` L56–61, `compute_asia_range` L83–89 | `_asia_range()` | exact; reads only the night **before** the trading session |
| Liquidity sweep (once/direction in session) | Step 2, L193–215 | loop "Step 2" | see **Approx-1** (intra-candle price) |
| Swing pivots (left=right=1, confirmed) | `find_pivot_highs` L92–101 | loop "swing pivot" (`p = q-2`) | exact; pivot only used once its right neighbour has closed |
| MSS (post-sweep close beyond swing) | `detect_mss` L114–138, Step 3 L217–226 | loop "Step 3" | exact for the **bullish** path (the only one that buys) |
| FVG detect (3-candle gap > 0.5×ATR) | `find_fvgs` L141–163, Step 4 L228–234 | loop "Step 4" | exact |
| FVG fill | Step 4b L236–241 | loop "Step 4b" | see **Approx-1** |
| Entry = MSS buy ∪ FVG-fill buy | `dca_bot.py` L177–183 (mss), L196–202 (fvg) | `populate_entry_trend` | exact (bullish only) |
| Bearish MSS → skip, no buy | `dca_bot.py` L189–193 | *not an entry* | exact (intentionally omitted) |

---

## Unavoidable approximations / model differences

**Approx-1 — intra-candle price point for sweeps & FVG fills.**
The live bot polls `live_price` (the in-progress candle) every ~60 s, so it can
detect a sweep or an FVG fill *mid-candle* (`tjr_strategy.py:205–215, 238`).
Freqtrade works on closed candles. The port uses:
- **Sweep:** the closed candle's `high`/`low` breaching the Asia level (captures
  the same breach the live tick would, just confirmed at candle close).
- **FVG fill:** the closed candle's `close` inside the zone (mirrors the literal
  `bottom ≤ price ≤ top` point check). A `low ≤ top and high ≥ bottom` "any
  touch" variant would fire slightly more often; we chose the literal-code
  formulation. Neither variant looks ahead.

**Approx-2 — 1-candle execution delay.**
Freqtrade enters at the **next** candle's open after the signal candle. The live
bot buys ~immediately at `live_price` within the same cycle. This is a *timing*
difference only (the safe direction — it never reveals the future) and does not
affect the look-ahead verdict.

**Approx-3 — "in-progress candle" handling.**
Live structure uses `closed = candles[:-1]` (drops the forming candle,
`tjr_strategy.py:184`) while fills/sweeps use the forming candle's price. In the
backtest every row is a closed candle, so "newest closed candle" = row `i`. The
relative ordering of *form-then-fill* is preserved; only the wall-clock offset
differs (folded into Approx-2).

**Approx-4 — synthetic exits.**
The live crypto bot is **buy-only DCA** and never sells. To let `backtesting`
close trades, the port uses `minimal_roi = {"0": 0.04}` and `stoploss = -0.06`
with `use_exit_signal = False`. These are **not** TJR rules and are not the
subject of the validation. (Lookahead still reports 0 biased exit signals.)

**Approx-5 — sizing / regime / SMT not ported.**
The live bot scales buy *size* by regime (`dca_bot` + `quant/regime.py`) and by
SMT divergence (`quant/smt_divergence.py`). Sizing cannot create entry-time
look-ahead, so it is out of scope here; the port emits flat-size entries.

**Approx-6 — once-per-session MSS uses the low-sweep priority.**
Live picks `swept_dir = "low" if "low" in swept else "high"`
(`tjr_strategy.py:221`); only the bullish (low-swept) branch buys. The port
implements exactly that branch for entries.

---

## Data venue

Historical candles: **Binance** `BTC/USDT` & `ETH/USDT`, 5m, via
`freqtrade download-data` (CCXT). The live bot streams from its own
`quant/crypto_data.py` source; Coinbase via CCXT serves limited/`USD`-quoted
OHLCV history, so Binance `USDT` pairs were used as the deepest, most reliable 5m
source. **The venue of the candles is irrelevant to a look-ahead check** — the
tool tests whether the *logic* reads future bars, independent of which exchange
produced them. Window: `2026-01-01 → 2026-06-16` (~5.5 months, 48,286 bars/pair).
