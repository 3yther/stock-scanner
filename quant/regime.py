"""
regime.py — per-symbol market regime detector for the Crypto Bot.

Classifies BTC/ETH (independently) on a higher timeframe (default 1h) into:
  TRENDING_UP    — price above a rising SMA(200) with a strong ADX(14)
  TRENDING_DOWN  — price below a falling SMA(200) with a strong ADX(14)
  CHOPPY         — weak ADX / ranging
  UNKNOWN        — not enough history yet (treated as full-weight by the policy)

Also reports trend slope, ADX, ATR(14) and ATR's percentile over its trailing
100-bar range. Pure functions (no I/O) plus a for_symbol() convenience fetcher.
sizing_policy() maps a regime to TJR DCA multipliers and is shared by the live
bot and the backtester so they agree.
"""

ADX_STRONG = 25.0      # ADX at/above this = a "strong" trend


# ── Indicators (pure) ──────────────────────────────────────────────────────

def sma(closes, period):
    if len(closes) < period:
        return None
    return sum(closes[-period:]) / period


def _true_ranges(candles):
    tr = []
    for i in range(1, len(candles)):
        h, l, pc = candles[i]["high"], candles[i]["low"], candles[i - 1]["close"]
        tr.append(max(h - l, abs(h - pc), abs(l - pc)))
    return tr


def atr_series(candles, period=14):
    """Rolling ATR(period) value at each bar (simple mean of true range)."""
    tr = _true_ranges(candles)
    if len(tr) < period:
        return []
    return [sum(tr[i - period + 1:i + 1]) / period for i in range(period - 1, len(tr))]


def atr_percentile(candles, period=14, lookback=100):
    """Current ATR as a percentile (0–100) of its trailing `lookback`-bar range."""
    s = atr_series(candles, period)
    if not s:
        return None
    window = s[-lookback:]
    cur = s[-1]
    return round(sum(1 for v in window if v <= cur) / len(window) * 100, 1)


def adx(candles, period=14):
    """Average Directional Index (Wilder). None if too little history."""
    n = len(candles)
    if n < 2 * period + 1:
        return None
    tr = [0.0] * n; pdm = [0.0] * n; ndm = [0.0] * n
    for i in range(1, n):
        h, l = candles[i]["high"], candles[i]["low"]
        ph, pl, pc = candles[i - 1]["high"], candles[i - 1]["low"], candles[i - 1]["close"]
        up, down = h - ph, pl - l
        pdm[i] = up if (up > down and up > 0) else 0.0
        ndm[i] = down if (down > up and down > 0) else 0.0
        tr[i] = max(h - l, abs(h - pc), abs(l - pc))

    def wilder(arr):
        sm = [0.0] * n
        sm[period] = sum(arr[1:period + 1])
        for i in range(period + 1, n):
            sm[i] = sm[i - 1] - sm[i - 1] / period + arr[i]
        return sm

    s_tr, s_pdm, s_ndm = wilder(tr), wilder(pdm), wilder(ndm)
    dx = [0.0] * n
    for i in range(period, n):
        if s_tr[i] == 0:
            continue
        pdi = 100 * s_pdm[i] / s_tr[i]
        ndi = 100 * s_ndm[i] / s_tr[i]
        denom = pdi + ndi
        dx[i] = 100 * abs(pdi - ndi) / denom if denom else 0.0

    if 2 * period >= n:
        return None
    adxv = sum(dx[period + 1:2 * period + 1]) / period
    for i in range(2 * period + 1, n):
        adxv = (adxv * (period - 1) + dx[i]) / period
    return adxv


# ── Classification ─────────────────────────────────────────────────────────

def classify(candles, sma_period=200, adx_period=14, slope_bars=20,
             adx_strong=ADX_STRONG, vol_lookback=100):
    closes = [c["close"] for c in candles]
    out = {"regime": "UNKNOWN", "ok": False, "price": closes[-1] if closes else None,
           "sma": None, "sma_slope": None, "sma_rising": None,
           "adx": None, "adx_strong": False, "atr_pctile": None}
    if len(candles) < sma_period + slope_bars + 1:
        return out

    s_now  = sma(closes, sma_period)
    s_prev = sma(closes[:-slope_bars], sma_period)
    price  = closes[-1]
    rising = s_now > s_prev
    slope  = (s_now - s_prev) / s_prev * 100 if s_prev else 0.0
    a      = adx(candles, adx_period)
    strong = a is not None and a >= adx_strong

    if price > s_now and rising and strong:
        regime = "TRENDING_UP"
    elif price < s_now and not rising and strong:
        regime = "TRENDING_DOWN"
    else:
        regime = "CHOPPY"

    out.update({"regime": regime, "ok": True, "sma": round(s_now, 4),
                "sma_slope": round(slope, 4), "sma_rising": rising,
                "adx": round(a, 2) if a is not None else None, "adx_strong": strong,
                "atr_pctile": atr_percentile(candles, adx_period, vol_lookback)})
    return out


# ── Regime → TJR sizing policy (shared by live bot + backtester) ───────────

def sizing_policy(regime: str) -> dict:
    """Multipliers/flags for a regime:
        interval  — scale on the base interval DCA (0 = pause)
        mss/fvg   — multiplier for an MSS / FVG-fill buy (0 = suppressed)
        *_enabled — whether that aggressive buy fires at all
    """
    table = {
        "TRENDING_UP":   {"interval": 1.0, "mss": 2.5,  "fvg": 1.5,  "mss_enabled": True,  "fvg_enabled": True},
        "CHOPPY":        {"interval": 1.0, "mss": 0.0,  "fvg": 1.25, "mss_enabled": False, "fvg_enabled": True},
        "TRENDING_DOWN": {"interval": 0.5, "mss": 0.0,  "fvg": 0.0,  "mss_enabled": False, "fvg_enabled": False},
    }
    # UNKNOWN / anything else → full weight (don't cripple the bot on thin data)
    return table.get(regime, {"interval": 1.0, "mss": 2.5, "fvg": 1.5,
                              "mss_enabled": True, "fvg_enabled": True})


def for_symbol(symbol: str, interval: str = "1h", limit: int = 260) -> dict:
    """Fetch candles and classify. Cheap (uses crypto_data's cache)."""
    from quant import crypto_data
    try:
        candles = crypto_data.get_candles(symbol, interval, limit)
    except Exception:
        candles = []
    res = classify(candles)
    res["symbol"] = symbol
    res["interval"] = interval
    return res
