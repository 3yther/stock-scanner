"""
MACD-based signal utilities + shared strategy core.

This module is the single source of truth for the trading strategy. Both the
live bot (scanner.py + paper_trader.py) and the backtester (backtest.py) call
these exact functions, so the simulated strategy can never drift from what we
trade live. Every function here is PURE: it takes data/state in and returns a
decision out, with no I/O, no clock, and no global mutation (except mutating a
position dict that is passed in, mirroring the live trader).
"""

from datetime import date, timedelta

import pandas as pd

import config


def macd_state(df: pd.DataFrame) -> tuple[int, bool]:
    """
    Compute MACD (12/26/9) on a OHLC DataFrame.

    Returns:
        crossover  : +1 = fresh bullish cross, -1 = fresh bearish cross, 0 = no cross
        is_bullish : True if MACD line is currently above signal line
    """
    if df is None or len(df) < 26:
        return 0, False

    d = df.copy()
    d["ema12"] = d["close"].ewm(span=12, adjust=False).mean()
    d["ema26"] = d["close"].ewm(span=26, adjust=False).mean()
    d["macd"]  = d["ema12"] - d["ema26"]
    d["msig"]  = d["macd"].ewm(span=9, adjust=False).mean()

    mc, sc   = float(d["macd"].iloc[-1]), float(d["msig"].iloc[-1])
    mp, sp   = float(d["macd"].iloc[-2]), float(d["msig"].iloc[-2])

    bullish = mc > sc
    if mc > sc and mp <= sp:
        cross = 1
    elif mc < sc and mp >= sp:
        cross = -1
    else:
        cross = 0

    return cross, bullish


def macd_histogram(df: pd.DataFrame) -> float:
    """Latest MACD histogram value (macd - signal), normalised by price."""
    if df is None or len(df) < 26:
        return 0.0
    d = df.copy()
    d["ema12"] = d["close"].ewm(span=12, adjust=False).mean()
    d["ema26"] = d["close"].ewm(span=26, adjust=False).mean()
    d["macd"]  = d["ema12"] - d["ema26"]
    d["msig"]  = d["macd"].ewm(span=9, adjust=False).mean()
    hist  = float(d["macd"].iloc[-1]) - float(d["msig"].iloc[-1])
    price = float(d["close"].iloc[-1])
    return abs(hist / price) * 100 if price else 0.0   # as % of price


# ── Market regime + relative strength (pure) ──────────────────────────────

def spy_regime(df_spy: pd.DataFrame | None) -> dict:
    """SPY vs its 50-day SMA → BULLISH / NEUTRAL / BEARISH.

    Given only the SPY daily candles available at the decision moment, so it is
    safe to call on a truncated (point-in-time) slice during a backtest.
    """
    if df_spy is None or len(df_spy) < 50:
        return {"regime": "NEUTRAL", "spy_price": 0.0, "spy_50sma": 0.0}
    sma50 = float(df_spy["close"].rolling(50).mean().iloc[-1])
    price = float(df_spy["close"].iloc[-1])
    if   price > sma50 * 1.005: regime = "BULLISH"
    elif price < sma50 * 0.995: regime = "BEARISH"
    else:                        regime = "NEUTRAL"
    return {"regime": regime, "spy_price": round(price, 2), "spy_50sma": round(sma50, 2)}


def relative_strength(df_sym: pd.DataFrame | None, df_spy: pd.DataFrame | None) -> float:
    """20-day return of a symbol minus SPY's, in percent. Causal/point-in-time."""
    if df_spy is None or df_sym is None:
        return 0.0
    if len(df_spy) < 21 or len(df_sym) < 21:
        return 0.0
    spy_ret = (df_spy["close"].iloc[-1] / df_spy["close"].iloc[-21] - 1) * 100
    sym_ret = (df_sym["close"].iloc[-1] / df_sym["close"].iloc[-21] - 1) * 100
    return round(float(sym_ret - spy_ret), 2)


# ── Per-symbol scoring (pure) ─────────────────────────────────────────────
#
# This is the heart of the entry signal. scanner._score_symbol fetches the data
# and calls this; the backtester slices historical data point-in-time and calls
# the SAME function. df_d/df_h must contain only candles up to the decision
# moment — never future bars — or the result is lookahead-biased.

def base_result(symbol: str) -> dict:
    """Neutral, no-data result for a symbol (insufficient history)."""
    return {
        "symbol":          symbol,
        "sector":          config.SECTOR_MAP.get(symbol, "Other"),
        "price":           0.0,
        "change_pct":      0.0,
        "signal":          0,
        "signal_label":    "NEUTRAL",
        "score":           0.0,
        "trend_bullish":   False,
        "entry_cross":     0,
        "vol_ratio":       1.0,
        "rs":              0.0,
        "earnings_soon":   False,
        "proximity_label": "",
        "no_data":         True,
    }


def score_symbol(
    symbol: str,
    df_d: pd.DataFrame | None,
    df_h: pd.DataFrame | None,
    rs: float = 0.0,
    earnings_soon: bool = False,
) -> dict:
    """Score one symbol 0–100 and derive its BUY/SELL/NEUTRAL signal.

    40 trend (1D MACD bullish) + 25 entry (fresh 1H bullish cross) +
    20 volume (tiered) + 15 relative strength. Identical to the live scanner.

    In DAILY-ONLY backtest mode the caller passes df_h = df_d so both the trend
    filter and the entry cross are evaluated on daily candles.
    """
    if df_d is None or len(df_d) < 26:
        return base_result(symbol)

    cross_d, bullish_d = macd_state(df_d)
    cross_h, bullish_h = 0, False
    if df_h is not None and len(df_h) >= 26:
        cross_h, bullish_h = macd_state(df_h)

    avg_vol    = df_d["volume"].rolling(20).mean().iloc[-1]
    cur_vol    = float(df_d["volume"].iloc[-1])
    vol_ratio  = (cur_vol / avg_vol) if avg_vol > 0 else 1.0
    price      = float(df_d["close"].iloc[-1])
    prev       = float(df_d["close"].iloc[-2]) if len(df_d) > 1 else price
    change_pct = (price - prev) / prev * 100 if prev else 0.0

    # Trend (40 pts)
    trend_pts = 40.0 if bullish_d else 0.0

    # Entry signal (25 pts)
    if cross_h == 1 and bullish_h:
        signal_pts = 25.0
    elif bullish_h:
        signal_pts = 12.0
    else:
        signal_pts = 0.0

    # Volume (20 pts, tiered)
    if   vol_ratio >= 2.0: vol_pts = 20.0
    elif vol_ratio >= 1.5: vol_pts = 15.0
    elif vol_ratio >= 1.2: vol_pts = 10.0
    elif vol_ratio >= 1.0: vol_pts = 5.0
    else:                  vol_pts = 0.0

    # Relative strength vs SPY (15 pts)
    if   rs > 3.0: rs_pts = 15.0
    elif rs > 1.0: rs_pts = 10.0
    elif rs > 0.0: rs_pts = 5.0
    else:          rs_pts = 0.0

    score = trend_pts + signal_pts + vol_pts + rs_pts

    if bullish_d and cross_h == 1 and vol_ratio >= config.MIN_VOL_RATIO and not earnings_soon:
        signal = 1
    elif cross_h == -1 or not bullish_d:
        signal = -1
    else:
        signal = 0

    if earnings_soon and signal == 1:
        signal = 0

    label = {1: "BUY", -1: "SELL", 0: "NEUTRAL"}[signal]

    proximity_label = ""
    if signal != 1:
        if   score >= 65: proximity_label = "CLOSE"
        elif score >= 50: proximity_label = "WATCHING"

    return {
        "symbol":          symbol,
        "sector":          config.SECTOR_MAP.get(symbol, "Other"),
        "price":           round(price, 2),
        "change_pct":      round(change_pct, 2),
        "signal":          signal,
        "signal_label":    label,
        "score":           round(score, 1),
        "trend_bullish":   bullish_d,
        "entry_cross":     cross_h,
        "vol_ratio":       round(vol_ratio, 2),
        "rs":              rs,
        "earnings_soon":   earnings_soon,
        "proximity_label": proximity_label,
        "no_data":         False,
    }


# ── Trade-management rules (pure) ─────────────────────────────────────────

def min_score_for_regime(regime: str) -> float:
    """Minimum score required to open a position in the given regime."""
    return config.MIN_SCORE_BEAR if regime == "BEARISH" else config.MIN_SCORE_BULL


def position_size_usd(balance: float, regime: str) -> float:
    """Dollar size for a new position — halved in a bear regime."""
    size_pct = config.TRADE_SIZE_PCT
    if regime == "BEARISH":
        size_pct *= config.BEAR_POSITION_SCALE
    return balance * size_pct


def sector_blocked(symbol: str, held_symbols) -> bool:
    """True if opening `symbol` would violate one-position-per-sector (ETFs exempt)."""
    sym_sector = config.SECTOR_MAP.get(symbol, "Other")
    if sym_sector == "ETF":
        return False
    held_sectors = {config.SECTOR_MAP.get(s, "Other") for s in held_symbols}
    return sym_sector in held_sectors


def count_trading_days(start: date, end: date) -> int:
    """Count Mon–Fri days strictly after `start` up to and including `end`."""
    days, cur = 0, start
    while cur < end:
        cur += timedelta(days=1)
        if cur.weekday() < 5:
            days += 1
    return days


def manage_position(pos: dict, price: float) -> str | None:
    """Update a position's trailing state for `price`; return a price-based exit
    action or None.

    Mutates pos['highest_price'] and pos['breakeven_active'] in place, exactly
    like the live trader's _check_exits. Returns 'TRAILING STOP',
    'BREAK-EVEN STOP', or 'TAKE PROFIT' when a stop/target is hit, else None.
    Time-based exits and signal-driven (SELL) exits are decided by the caller
    because they need calendar / scan context.
    """
    if price > pos["highest_price"]:
        pos["highest_price"] = price

    upnl_pct = (price - pos["entry_price"]) / pos["entry_price"] if pos["entry_price"] else 0.0
    if upnl_pct >= config.BREAKEVEN_TRIGGER and not pos["breakeven_active"]:
        pos["breakeven_active"] = True

    trail_sl = pos["highest_price"] * (1 - config.STOP_LOSS_PCT)
    if pos["breakeven_active"]:
        trail_sl = max(trail_sl, pos["entry_price"])

    tp_price = pos["entry_price"] * (1 + config.TAKE_PROFIT_PCT)

    if price <= trail_sl:
        if pos["breakeven_active"] and price <= pos["entry_price"]:
            return "BREAK-EVEN STOP"
        return "TRAILING STOP"
    if price >= tp_price:
        return "TAKE PROFIT"
    return None


def atr(df: pd.DataFrame | None, period: int = 14) -> float | None:
    """Average True Range over `period` completed bars (causal / no lookahead).

    True Range = max(high-low, |high-prev_close|, |low-prev_close|). Uses only
    bars up to and including the last row of `df`, so calling it on a
    point-in-time slice gives the ATR as it stood at that bar's close. Returns
    a price-unit value, or None if there isn't enough history.
    """
    if df is None or len(df) < period + 1:
        return None
    high, low, close = df["high"], df["low"], df["close"]
    prev_close = close.shift(1)
    tr = pd.concat([(high - low).abs(),
                    (high - prev_close).abs(),
                    (low - prev_close).abs()], axis=1).max(axis=1)
    val = tr.rolling(period).mean().iloc[-1]
    return float(val) if pd.notna(val) else None


def manage_position_atr(pos: dict, price: float, atr_value: float,
                        atr_mult: float = 3.0) -> str | None:
    """ATR-based 'let winners run' trailing stop — NO fixed take-profit.

    Trails `atr_mult` × ATR below the highest price reached since entry; keeps
    the same break-even behaviour (once up BREAKEVEN_TRIGGER, the stop can't drop
    below entry). Mutates pos in place like manage_position. Returns an exit
    action or None. The caller still applies the (extended) max-hold limit.
    """
    if price > pos["highest_price"]:
        pos["highest_price"] = price

    upnl_pct = (price - pos["entry_price"]) / pos["entry_price"] if pos["entry_price"] else 0.0
    if upnl_pct >= config.BREAKEVEN_TRIGGER and not pos["breakeven_active"]:
        pos["breakeven_active"] = True

    trail_sl = pos["highest_price"] - atr_mult * atr_value
    if pos["breakeven_active"]:
        trail_sl = max(trail_sl, pos["entry_price"])

    if price <= trail_sl:
        if pos["breakeven_active"] and price <= pos["entry_price"]:
            return "BREAK-EVEN STOP"
        return "ATR TRAILING STOP"
    return None


# ── vol_conviction sizing (shared by the backtester and the live bot) ─────
# Canonical parameters for the "T4" config. Both code paths call
# vol_conviction_size() so the live bot sizes positions exactly as the backtest
# that validated this config did — otherwise the validated result wouldn't
# transfer to live.
RISK_PER_TRADE   = 0.01    # risk 1% of equity per position
ATR_STOP_MULT    = 2.0     # sizing assumes a 2×ATR stop distance
MAX_POSITION_PCT = 0.20    # cap any single position at 20% of equity
ATR_TRAIL_MULT   = 3.0     # trail_only exit: trail 3×ATR below the high-water mark
MAX_HOLD_TRAIL   = 15      # trail_only exit: extended max-hold (trading days)
ATR_PERIOD       = 14


def conviction_mult(rank: int) -> float:
    """Position multiplier by signal rank: 1st 1.5×, 2nd 1.25×, 3rd+ 1.0×."""
    return {1: 1.5, 2: 1.25}.get(rank, 1.0)


def vol_conviction_size(equity: float, cash: float, entry_price: float,
                        atr_value: float | None, rank: int) -> float:
    """Dollar position size under volatility-scaled conviction sizing.

    Risk RISK_PER_TRADE of equity over an ATR_STOP_MULT×ATR stop, scaled by the
    conviction multiplier for `rank`, then capped at MAX_POSITION_PCT of equity
    AND at available cash (so total exposure can never exceed 100%). Returns 0.0
    if ATR is unusable — the caller should fall back to flat sizing.
    """
    if not atr_value or atr_value <= 0 or entry_price <= 0:
        return 0.0
    stop_dist = ATR_STOP_MULT * atr_value
    risk_usd  = RISK_PER_TRADE * equity
    base_size = risk_usd * entry_price / stop_dist     # so a stop move loses risk_usd
    final     = base_size * conviction_mult(rank)
    return min(final, MAX_POSITION_PCT * equity, cash)


# ── Shared trend helper ───────────────────────────────────────────────────

def trend_up_sma(df: pd.DataFrame | None, period: int = 50) -> bool:
    """True if the latest close is above its `period`-day SMA (causal). The same
    'above SMA50' trend concept the regime filter uses, applied per symbol."""
    if df is None or len(df) < period:
        return False
    sma = float(df["close"].rolling(period).mean().iloc[-1])
    return float(df["close"].iloc[-1]) > sma > 0


def make_result(symbol: str, *, signal: int = 0, score: float = 0.0,
                price: float = 0.0, change_pct: float = 0.0,
                earnings_soon: bool = False, no_data: bool = False,
                **extra) -> dict:
    """Build a result dict in the standard shape every strategy returns, so the
    backtester / scanner can consume any strategy's output the same way."""
    d = {
        "symbol":          symbol,
        "sector":          config.SECTOR_MAP.get(symbol, "Other"),
        "price":           round(price, 2),
        "change_pct":      round(change_pct, 2),
        "signal":          signal,
        "signal_label":    {1: "BUY", -1: "SELL", 0: "NEUTRAL"}[signal],
        "score":           round(score, 1),
        "trend_bullish":   False,
        "entry_cross":     0,
        "vol_ratio":       1.0,
        "rs":              0.0,
        "earnings_soon":   earnings_soon,
        "proximity_label": "",
        "no_data":         no_data,
    }
    d.update(extra)
    return d


# ══════════════════════════════════════════════════════════════════════════
#  STRATEGY INTERFACE + REGISTRY
#
#  Each strategy turns a point-in-time view of one symbol into a signal dict.
#  Entry admission is overridable, but ALL strategies share the same risk core
#  (position_size_usd / sector_blocked / manage_position / max-hold / max-
#  positions) — they only differ in HOW they decide to enter. The backtester
#  fills at the next bar's open for every strategy, so the no-lookahead
#  guarantee holds regardless of which one is selected.
# ══════════════════════════════════════════════════════════════════════════

class Strategy:
    """Base interface. A strategy is stateless: given candles up to *now* it
    returns a BUY/HOLD/SELL signal + a 0–100 score."""
    name        = "base"
    label       = "Base"
    description = ""

    # How many prior daily bars this strategy needs before it can emit its first
    # valid signal (longest lookback it touches). The backtester prefetches at
    # least this much history BEFORE the requested start so day-one indicators
    # are real — never lookahead, since warmup bars are all in the past relative
    # to the first tradeable day.
    warmup_bars = 50

    def generate_signal(self, symbol: str, df_d: pd.DataFrame | None,
                        df_h: pd.DataFrame | None, df_spy: pd.DataFrame | None,
                        rs: float = 0.0, earnings_soon: bool = False) -> dict:
        raise NotImplementedError

    def entry_admits(self, result: dict, regime: str) -> bool:
        """Whether a scored symbol may open a position now. Default: a BUY
        signal, not near earnings, clearing the regime-aware score gate."""
        return (result.get("signal") == 1
                and not result.get("earnings_soon")
                and result.get("score", 0.0) >= min_score_for_regime(regime))


class MacdMtfStrategy(Strategy):
    """The live strategy: 1D MACD trend + 1H MACD entry cross, scored on volume
    and relative strength. Thin wrapper over score_symbol — behaviour unchanged."""
    name        = "macd_mtf"
    label       = "MACD Multi-Timeframe"
    description = ("1D MACD trend filter + 1H MACD entry cross, scored on volume "
                   "and relative strength vs SPY. This is the live strategy.")
    # MACD(12/26/9) needs ~26 bars; 20-bar volume avg + 21-bar RS → ~35 to settle.
    warmup_bars = 35

    def generate_signal(self, symbol, df_d, df_h, df_spy, rs=0.0, earnings_soon=False):
        return score_symbol(symbol, df_d, df_h, rs, earnings_soon)


class LiquiditySweepStrategy(Strategy):
    """Mechanical liquidity-sweep reversal on daily candles.

    Long when today's LOW dips below the lowest low of the previous N days (the
    sweep) but today's CLOSE finishes back above that prior low (the sweep
    failed → reversal), with a bullish close for confirmation, and only while
    the symbol's daily trend is up (close > SMA50). All risk/exit rules are the
    shared core — no new risk logic here.
    """
    name        = "liquidity_sweep"
    label       = "Liquidity Sweep Reversal"
    description = ("Long when today sweeps below the prior {N}-day low then "
                   "closes back above it (failed breakdown), with a bullish "
                   "close, while price is above its SMA50.")

    def __init__(self, lookback: int = 10, require_bullish_close: bool = True):
        self.lookback = lookback
        self.require_bullish_close = require_bullish_close
        self.description = self.description.replace("{N}", str(lookback))
        # N-bar sweep window + the 50-bar SMA trend filter.
        self.warmup_bars = max(lookback + 1, 50)

    def generate_signal(self, symbol, df_d, df_h, df_spy, rs=0.0, earnings_soon=False):
        n = self.lookback
        # Need N prior bars for the sweep level and 50 for the SMA trend filter.
        if df_d is None or len(df_d) < max(n + 1, 50):
            return make_result(symbol, no_data=True)

        lows        = df_d["low"]
        prior_low   = float(lows.iloc[-(n + 1):-1].min())   # previous N lows, EXCLUDING today
        today_low   = float(lows.iloc[-1])
        today_close = float(df_d["close"].iloc[-1])
        today_open  = float(df_d["open"].iloc[-1])

        trend_up      = trend_up_sma(df_d, 50)
        swept         = today_low < prior_low                # dipped below the level
        reclaimed     = today_close > prior_low              # but closed back above it
        bullish_close = today_close > today_open
        confirmed     = bullish_close or not self.require_bullish_close

        if swept and reclaimed and trend_up and confirmed and not earnings_soon:
            recov_pct = (today_close - prior_low) / prior_low * 100 if prior_low else 0.0
            score = min(100.0, 85.0 + recov_pct)             # valid sweeps score 85–100
            return make_result(symbol, signal=1, score=score, price=today_close,
                               earnings_soon=earnings_soon, trend_bullish=trend_up,
                               prior_low=round(prior_low, 2), swept=True)

        return make_result(symbol, signal=0, score=0.0, price=today_close,
                           earnings_soon=earnings_soon, trend_bullish=trend_up)


class MomentumStrategy(Strategy):
    """Classic long-term momentum on daily candles.

    Long when price is above its 200-day SMA (long-term uptrend) AND its 6-month
    (126-day) return is positive; ranks candidates by momentum strength so the
    strongest get the limited position slots. Needs a LONG warmup (200 bars) —
    the case that exposed the engine's old fixed-warmup bug. Risk/exit rules are
    the shared core; only the entry differs.
    """
    name        = "momentum"
    label       = "Long-Term Momentum"
    description = ("Long when price is above its 200-day SMA and 6-month "
                   "(126-day) momentum is positive; ranked by momentum strength.")
    warmup_bars = 200

    def __init__(self, sma_period: int = 200, mom_lookback: int = 126):
        self.sma_period   = sma_period
        self.mom_lookback = mom_lookback
        # Binding lookback: the SMA period or the momentum window, whichever is
        # longer (+1 bar for the return's base).
        self.warmup_bars  = max(sma_period, mom_lookback + 1)

    def generate_signal(self, symbol, df_d, df_h, df_spy, rs=0.0, earnings_soon=False):
        need = max(self.sma_period, self.mom_lookback + 1)
        if df_d is None or len(df_d) < need:
            return make_result(symbol, no_data=True)

        close = df_d["close"]
        price = float(close.iloc[-1])
        sma   = float(close.rolling(self.sma_period).mean().iloc[-1])
        mom   = (price / float(close.iloc[-(self.mom_lookback + 1)]) - 1) * 100
        trend_up = price > sma > 0

        if trend_up and mom > 0 and not earnings_soon:
            score = min(100.0, 50.0 + mom)   # stronger momentum ranks higher
            return make_result(symbol, signal=1, score=score, price=price,
                               earnings_soon=earnings_soon, trend_bullish=trend_up,
                               momentum_pct=round(mom, 2), sma200=round(sma, 2))
        return make_result(symbol, signal=0, score=0.0, price=price,
                           earnings_soon=earnings_soon, trend_bullish=trend_up)

    def entry_admits(self, result, regime):
        # Momentum self-selects by trend + positive momentum; rank by score
        # (strength) for the limited slots rather than the MACD score gate.
        return result.get("signal") == 1 and not result.get("earnings_soon")


# Registry — add new strategies here and they appear in the backtest selector.
STRATEGIES: dict[str, Strategy] = {
    MacdMtfStrategy().name:        MacdMtfStrategy(),
    LiquiditySweepStrategy().name: LiquiditySweepStrategy(),
    MomentumStrategy().name:       MomentumStrategy(),
}
DEFAULT_STRATEGY = "macd_mtf"


def get_strategy(name: str | None) -> Strategy:
    """Look up a strategy by name, falling back to the default."""
    return STRATEGIES.get(name or "", STRATEGIES[DEFAULT_STRATEGY])


def strategy_list() -> list[dict]:
    """[{name, label, description}] for the UI selector."""
    return [{"name": s.name, "label": s.label, "description": s.description}
            for s in STRATEGIES.values()]
