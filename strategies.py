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
