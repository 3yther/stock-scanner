"""
TjrValidation.py — Freqtrade IStrategy port of the live TJR strategy, built
SOLELY to run `freqtrade lookahead-analysis` as an independent bias check.

This is NOT the live bot. It mirrors the signal logic in the main app's
quant/tjr_strategy.py so the lookahead tool can re-run it on progressively
revealed data and flag any signal that secretly depends on future candles.

Source of truth: stock-scanner/quant/tjr_strategy.py. Every ported rule cites the
exact function / line range it mirrors (line numbers as of the audited revision;
see VALIDATION_REPORT.md for the commit). Fidelity > convenience: because the
live bot runs a candle-by-candle loop (dca_bot._cycle → tjr_strategy.process),
the path-dependent parts (session sweep state, "first MSS per session", the
active-FVG list and its fills) are ported as an explicit CAUSAL loop over the
dataframe rather than vectorized — this removes any ambiguity about whether the
port itself introduced look-ahead. Cleanly-causal scalars (ATR, the Asia range,
which only ever reads the *prior* night) are precomputed with pandas.

Entry = the conditions under which the live DCA bot makes a TJR buy:
  * Bullish MSS               (dca_bot.py:180 → _buy "tjr_mss")
  * A bullish FVG zone fills  (dca_bot.py:174-177 → _buy "tjr_fvg")
Bearish MSS does NOT buy in the live bot (it skips the next interval,
dca_bot.py:189-190), so it is intentionally not an entry here.

Exits are SYNTHETIC (minimal_roi / stoploss). The live crypto bot is buy-only
DCA accumulation and never sells; exits exist here only so `backtesting` can
close trades for the sanity check. They are not part of the TJR logic under test.
"""

from datetime import datetime

import numpy as np
import pandas as pd
from pandas import DataFrame

from freqtrade.strategy import IStrategy


# Session boundaries — mirror tjr_strategy.py:24-27
ASIA_START_H = 20      # tjr_strategy.py:25  ASIA_START_H
TRADING_START_H = 3    # tjr_strategy.py:26  TRADING_START_H
TRADING_END_H = 13     # tjr_strategy.py:27  TRADING_END_H

ATR_PERIOD = 14        # tjr_strategy.py:72  atr(period=14) / find_fvgs ATR(14)
FVG_ATR_MULT = 0.5     # tjr_strategy.py:141 find_fvgs(atr_mult=0.5)
ACTIVE_FVG_LIMIT = 20  # tjr_strategy.py:237 get_active_fvgs(symbol, 20)


class TjrValidation(IStrategy):
    INTERFACE_VERSION = 3
    timeframe = "5m"
    can_short = False

    # Synthetic exits only (see module docstring). Not TJR rules.
    minimal_roi = {"0": 0.04}
    stoploss = -0.06
    use_exit_signal = False
    process_only_new_candles = True

    # Warmup: a full prior day (288×5m) covers the Asia range + ATR(14) + pivots.
    startup_candle_count = 300

    # ── Cleanly-causal precomputations ────────────────────────────────────

    @staticmethod
    def _atr(df: DataFrame, period: int = ATR_PERIOD) -> pd.Series:
        """Mirror tjr_strategy.py:72-80 atr(): TR = max(h-l, |h-pc|, |l-pc|),
        ATR = mean of the last `period` TRs (simple moving average, NOT Wilder).
        Causal: TR uses the PREVIOUS close (shift(1)); the rolling mean is a
        trailing window, so atr[i] depends only on rows ≤ i."""
        pc = df["close"].shift(1)
        tr = pd.concat([
            df["high"] - df["low"],
            (df["high"] - pc).abs(),
            (df["low"] - pc).abs(),
        ], axis=1).max(axis=1)
        return tr.rolling(period).mean()

    @staticmethod
    def _asia_range(df: DataFrame):
        """Mirror tjr_strategy.py:56-61 _asia_window + :83-89 compute_asia_range.
        The Asia session feeding trading-date D is the PREVIOUS day 20:00 → D
        00:00 UTC (hours 20–23 of D-1). asia_high/asia_low for D = max-high /
        min-low over that window. This only ever reads the night BEFORE D's
        03:00–13:00 trading session, so it is causal for every trading bar."""
        cal_date = df["date"].dt.normalize()
        hour = df["date"].dt.hour
        # candles in hours 20–23 belong to the NEXT calendar day's trading date
        is_asia = hour >= ASIA_START_H                      # tjr_strategy.py:53 in_asia_session
        asia_for = cal_date.where(is_asia) + pd.Timedelta(days=1)
        src = df.loc[is_asia]
        if src.empty:
            return (pd.Series(np.nan, index=df.index),
                    pd.Series(np.nan, index=df.index))
        keys = asia_for.loc[is_asia]
        highs = src["high"].groupby(keys).max()
        lows = src["low"].groupby(keys).min()
        ah = cal_date.map(highs)
        al = cal_date.map(lows)
        return ah, al

    # ── Causal loop mirroring tjr_strategy.process() ──────────────────────

    def populate_indicators(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        df = dataframe
        df["atr14"] = self._atr(df)
        df["asia_high"], df["asia_low"] = self._asia_range(df)

        n = len(df)
        high = df["high"].to_numpy(dtype=float)
        low = df["low"].to_numpy(dtype=float)
        close = df["close"].to_numpy(dtype=float)
        atr = df["atr14"].to_numpy(dtype=float)
        asia_high = df["asia_high"].to_numpy(dtype=float)
        asia_low = df["asia_low"].to_numpy(dtype=float)
        hour = df["date"].dt.hour.to_numpy()
        day_id = df["date"].dt.normalize().astype("int64").to_numpy()  # session key

        mss_entry = np.zeros(n, dtype=bool)   # bullish MSS buy  (tjr_mss)
        fvg_entry = np.zeros(n, dtype=bool)   # bullish FVG fill  (tjr_fvg)
        swept_low_col = np.zeros(n, dtype=bool)
        swept_high_col = np.zeros(n, dtype=bool)

        # Per-session state — reset each trading date (mirrors crypto_db rows
        # being scoped per trading date: get_sweeps_on / get_mss_on, used in
        # tjr_strategy.py:196,218).
        cur_session = None
        swept_low = swept_high = False
        mss_done = False
        sess_high = []          # session-local highs, in order (for pivots)
        sess_swing_highs = []   # CONFIRMED session swing-high levels (most recent last)

        # Active (unfilled) FVG zones — mirrors the persisted tjr_fvg list that
        # get_active_fvgs(...,20) reads (tjr_strategy.py:237). Both directions are
        # kept (live keeps both); only a BULLISH fill triggers a buy.
        active_fvgs = []        # list of {"top","bottom","dir"}

        for i in range(n):
            in_session = TRADING_START_H <= hour[i] < TRADING_END_H  # tjr_strategy.py:49 in_trading_session

            # New trading date → reset session structure state.
            if in_session and day_id[i] != cur_session:
                cur_session = day_id[i]
                swept_low = swept_high = False
                mss_done = False
                sess_high = []
                sess_swing_highs = []

            # ---- Step 4: FVG formation (tjr_strategy.py:141-163, :228-234) ----
            # 3-candle window with c0 = NEWEST closed candle (index i), c2 = i-2.
            # Bullish: low[i] > high[i-2]; gap must exceed 0.5×ATR(14) measured up
            # to i (causal). Bearish is the mirror; kept for the active list only.
            if i >= 2 and not np.isnan(atr[i]) and atr[i] > 0:
                if low[i] > high[i - 2]:
                    gap = low[i] - high[i - 2]
                    if gap > FVG_ATR_MULT * atr[i]:
                        active_fvgs.append({"top": low[i], "bottom": high[i - 2], "dir": "bull"})
                elif high[i] < low[i - 2]:
                    gap = low[i - 2] - high[i]
                    if gap > FVG_ATR_MULT * atr[i]:
                        active_fvgs.append({"top": low[i - 2], "bottom": high[i], "dir": "bear"})
                if len(active_fvgs) > ACTIVE_FVG_LIMIT:      # keep most-recent 20 (tjr_strategy.py:237)
                    del active_fvgs[:-ACTIVE_FVG_LIMIT]

            # ---- Step 4b: FVG fill (tjr_strategy.py:236-241) ----
            # Live marks a fill when the current price re-enters an unfilled zone
            # (bottom ≤ price ≤ top). We use the closed candle's close as that
            # price point (see PORTING_NOTES.md on intra-candle polling). A
            # BULLISH fill is the buy trigger (dca_bot.py:174-177).
            still_active = []
            for f in active_fvgs:
                if f["bottom"] <= close[i] <= f["top"]:
                    if f["dir"] == "bull":
                        fvg_entry[i] = True
                    # filled → drop from active (don't carry forward)
                else:
                    still_active.append(f)
            active_fvgs = still_active

            if in_session:
                # ---- Step 2: liquidity sweep (tjr_strategy.py:193-215) ----
                # Once per direction, during the trading session. A session
                # candle breaching the Asia high/low counts (the candle's
                # high/low captures what the live bot also checks via live_price).
                if not np.isnan(asia_high[i]) and high[i] > asia_high[i]:
                    swept_high = True
                if not np.isnan(asia_low[i]) and low[i] < asia_low[i]:
                    swept_low = True

                # session-local pivot confirmation, then MSS check
                sess_high.append(high[i])
                q = len(sess_high) - 1            # current session position
                # ---- swing pivot (tjr_strategy.py:92-101 find_pivot_highs, left=right=1) ----
                # A pivot at session position p is only CONFIRMED once its right
                # neighbour (p+1) has closed. When evaluating bar i (position q),
                # detect_mss uses sess[:q] (tjr_strategy.py:123), so the newest
                # usable pivot is p = q-2 (its neighbours q-3 and q-1 are < q).
                p = q - 2
                if p >= 1 and sess_high[p] > sess_high[p - 1] and sess_high[p] > sess_high[p + 1]:
                    sess_swing_highs.append(sess_high[p])

                # ---- Step 3: MSS (tjr_strategy.py:114-138 detect_mss, :217-226) ----
                # After a LOW sweep (swept_dir = "low" → bullish, tjr_strategy.py:221),
                # the FIRST closed candle that closes ABOVE the most recent
                # confirmed session swing high is a bullish MSS → buy. One per
                # session (mss_done). Bullish only: bearish MSS does not buy.
                if swept_low and not mss_done and sess_swing_highs:
                    if close[i] > sess_swing_highs[-1]:
                        mss_entry[i] = True
                        mss_done = True

            swept_low_col[i] = swept_low
            swept_high_col[i] = swept_high

        df["tjr_mss_entry"] = mss_entry
        df["tjr_fvg_entry"] = fvg_entry
        df["swept_low"] = swept_low_col
        df["swept_high"] = swept_high_col
        return df

    def populate_entry_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        # Entry = live bot's TJR buy triggers: bullish MSS OR bullish FVG fill.
        mss = dataframe["tjr_mss_entry"]
        fvg = dataframe["tjr_fvg_entry"]
        dataframe.loc[mss | fvg, "enter_long"] = 1
        dataframe.loc[mss, "enter_tag"] = "tjr_mss"
        # FVG tag only where MSS didn't already tag (don't overwrite an MSS bar)
        dataframe.loc[fvg & ~mss, "enter_tag"] = "tjr_fvg"
        return dataframe

    def populate_exit_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        # Synthetic: exits handled by minimal_roi / stoploss only. Live bot never
        # sells (buy-only DCA), so there is no TJR exit rule to port.
        dataframe["exit_long"] = 0
        return dataframe
