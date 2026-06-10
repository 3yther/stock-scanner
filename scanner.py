"""
Multi-timeframe scanner — v2.

Always-on scoring (0–100) for all 60 symbols every scan:
  40 pts  trend         — 1D MACD bullish
  25 pts  entry signal  — fresh 1H bullish cross (25), holding (12), else 0
  20 pts  volume        — tiered: ≥2× avg (20), ≥1.5× (15), ≥1.2× (10), ≥1× (5), <1× (0)
  15 pts  rel. strength — 20-day return vs SPY: >+3% (15), >+1% (10), >0% (5), ≤0% (0)

BUY signal requires: 1D bullish AND fresh 1H bullish cross AND vol ≥ MIN_VOL_RATIO.
Earnings-flagged stocks are suppressed to NEUTRAL even if signal qualifies.

Batch prefetch: two yfinance API calls cover all 60 symbols + SPY before scoring.
"""

import threading
import time

import config
import market_data
import strategies


class Scanner:
    def __init__(self):
        self._results: list[dict] = []
        self._regime:  dict       = {"regime": "NEUTRAL", "spy_price": 0.0, "spy_50sma": 0.0}
        self._lock            = threading.Lock()
        self._last_scan_ts: float = 0.0
        self._running = False

    # ------------------------------------------------------------------ #
    #  Scoring                                                             #
    # ------------------------------------------------------------------ #

    def _score_symbol(self, symbol: str, spy_regime: dict) -> dict:
        df_d, df_h = market_data.get_ohlc(symbol)

        base = {
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

        if df_d is None or len(df_d) < 30:
            return base

        cross_d, bullish_d = strategies.macd_state(df_d)
        cross_h, bullish_h = 0, False
        if df_h is not None and len(df_h) >= 26:
            cross_h, bullish_h = strategies.macd_state(df_h)

        # Volume ratio vs 20-day average
        avg_vol   = df_d["volume"].rolling(20).mean().iloc[-1]
        cur_vol   = float(df_d["volume"].iloc[-1])
        vol_ratio = (cur_vol / avg_vol) if avg_vol > 0 else 1.0

        # Price / 1-day change
        price     = float(df_d["close"].iloc[-1])
        prev      = float(df_d["close"].iloc[-2]) if len(df_d) > 1 else price
        change_pct = (price - prev) / prev * 100 if prev else 0.0

        # ── Always-on scoring ─────────────────────────────────────────
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
        if vol_ratio >= 2.0:
            vol_pts = 20.0
        elif vol_ratio >= 1.5:
            vol_pts = 15.0
        elif vol_ratio >= 1.2:
            vol_pts = 10.0
        elif vol_ratio >= 1.0:
            vol_pts = 5.0
        else:
            vol_pts = 0.0

        # Relative strength vs SPY (15 pts)
        rs = market_data.get_relative_strength(symbol)
        if rs > 3.0:
            rs_pts = 15.0
        elif rs > 1.0:
            rs_pts = 10.0
        elif rs > 0.0:
            rs_pts = 5.0
        else:
            rs_pts = 0.0

        score = trend_pts + signal_pts + vol_pts + rs_pts

        # ── Signal determination ──────────────────────────────────────
        # BUY requires: bullish trend + fresh 1H cross + sufficient volume
        earnings_soon = market_data.get_earnings_flag(symbol)

        if bullish_d and cross_h == 1 and vol_ratio >= config.MIN_VOL_RATIO and not earnings_soon:
            signal = 1
        elif cross_h == -1 or not bullish_d:
            signal = -1
        else:
            signal = 0

        # Earnings suppresses to NEUTRAL (but score stays for proximity)
        if earnings_soon and signal == 1:
            signal = 0

        label = {1: "BUY", -1: "SELL", 0: "NEUTRAL"}[signal]

        # Proximity: how close a non-BUY stock is to triggering
        proximity_label = ""
        if signal != 1:
            if score >= 65:
                proximity_label = "CLOSE"
            elif score >= 50:
                proximity_label = "WATCHING"

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

    # ------------------------------------------------------------------ #
    #  Full scan                                                           #
    # ------------------------------------------------------------------ #

    def scan_all(self) -> tuple[list[dict], dict]:
        # Batch-prefetch daily + hourly for all 60 + SPY in 2 API calls
        market_data.batch_scan_populate(config.SYMBOLS)
        spy_regime = market_data.get_spy_regime()

        results = []
        for sym in config.SYMBOLS:
            results.append(self._score_symbol(sym, spy_regime))

        results.sort(key=lambda x: x["score"], reverse=True)
        return results, spy_regime

    # ------------------------------------------------------------------ #
    #  Thread                                                              #
    # ------------------------------------------------------------------ #

    def run_loop(self, trader):
        """Blocking loop — call from a daemon thread."""
        self._running = True

        # Kick off earnings refresh in the background (slow, ~6s for 60 symbols)
        def _earnings_loop():
            market_data.refresh_earnings_cache(config.SYMBOLS)
            while self._running:
                time.sleep(14400)  # refresh every 4 hours
                market_data.refresh_earnings_cache(config.SYMBOLS)

        threading.Thread(target=_earnings_loop, daemon=True, name="earnings").start()

        while self._running:
            try:
                print(f"  [SCANNER] Scanning {len(config.SYMBOLS)} symbols…")
                results, spy_regime = self.scan_all()
                ts = time.time()
                with self._lock:
                    self._results      = results
                    self._regime       = spy_regime
                    self._last_scan_ts = ts
                trader.update_scan(results, ts, spy_regime)
                buys = [r["symbol"] for r in results if r["signal"] == 1]
                print(
                    f"  [SCANNER] Done — regime={spy_regime['regime']}"
                    f"  BUY signals: {buys[:5] or 'none'}"
                )
            except Exception as e:
                print(f"  [SCANNER] Error: {e}")
            time.sleep(config.SCAN_INTERVAL)

    def get_results(self) -> list[dict]:
        with self._lock:
            return list(self._results)

    def get_regime(self) -> dict:
        with self._lock:
            return dict(self._regime)

    @property
    def last_scan_ts(self) -> float:
        return self._last_scan_ts
