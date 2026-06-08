"""
Multi-timeframe scanner.

Every SCAN_INTERVAL seconds, fetches 1D + 1H data for all symbols and
computes an opportunity score for each.

Scoring (0–100):
  40 pts  trend alignment  — 1D MACD bullish
  30 pts  signal strength  — fresh 1H MACD bullish cross (+30), or holding (+15)
  30 pts  volume vs avg    — capped at 2× average volume → 30 pts

Only BUY signals receive a non-zero score. Results are sorted descending;
the paper trader uses the top MAX_POSITIONS entries as trade candidates.
"""

import time
import threading

import pandas as pd

import config
import market_data
import strategies


class Scanner:
    def __init__(self):
        self._results: list[dict] = []
        self._results_lock = threading.Lock()
        self._last_scan_ts: float = 0.0
        self._running = False

    # ------------------------------------------------------------------ #
    #  Scoring                                                             #
    # ------------------------------------------------------------------ #

    def _score_symbol(self, symbol: str) -> dict:
        df_d, df_h = market_data.get_ohlc(symbol)

        base = {
            "symbol":        symbol,
            "price":         0.0,
            "change_pct":    0.0,
            "signal":        0,
            "signal_label":  "NEUTRAL",
            "score":         0.0,
            "trend_bullish": False,
            "entry_cross":   0,
            "vol_ratio":     1.0,
            "no_data":       True,
        }

        if df_d is None or len(df_d) < 30:
            return base

        cross_d, bullish_d = strategies.macd_state(df_d)
        cross_h, bullish_h = (0, False)
        if df_h is not None and len(df_h) >= 26:
            cross_h, bullish_h = strategies.macd_state(df_h)

        # Multi-timeframe signal
        if bullish_d and cross_h == 1:
            signal = 1
        elif cross_h == -1 or not bullish_d:
            signal = -1
        else:
            signal = 0

        # Volume ratio (using daily data)
        avg_vol = df_d["volume"].rolling(20).mean().iloc[-1]
        cur_vol = float(df_d["volume"].iloc[-1])
        vol_ratio = (cur_vol / avg_vol) if avg_vol > 0 else 1.0

        # Price / change
        price = float(df_d["close"].iloc[-1])
        prev  = float(df_d["close"].iloc[-2]) if len(df_d) > 1 else price
        change_pct = (price - prev) / prev * 100 if prev else 0.0

        # Score (only for BUY signals)
        score = 0.0
        if signal == 1:
            trend_pts  = 40.0 if bullish_d else 0.0
            signal_pts = 30.0 if cross_h == 1 else 15.0
            vol_pts    = min((vol_ratio - 1.0) / 1.0, 1.0) * 30.0 if vol_ratio > 1 else 0.0
            score      = trend_pts + signal_pts + vol_pts

        label = {1: "BUY", -1: "SELL", 0: "NEUTRAL"}[signal]

        return {
            "symbol":        symbol,
            "price":         round(price, 2),
            "change_pct":    round(change_pct, 2),
            "signal":        signal,
            "signal_label":  label,
            "score":         round(score, 1),
            "trend_bullish": bullish_d,
            "entry_cross":   cross_h,
            "vol_ratio":     round(vol_ratio, 2),
            "no_data":       False,
        }

    # ------------------------------------------------------------------ #
    #  Full scan                                                           #
    # ------------------------------------------------------------------ #

    def scan_all(self) -> list[dict]:
        results = []
        for sym in config.SYMBOLS:
            r = self._score_symbol(sym)
            results.append(r)
            time.sleep(0.15)   # avoid hammering yfinance

        results.sort(key=lambda x: x["score"], reverse=True)
        return results

    # ------------------------------------------------------------------ #
    #  Thread                                                              #
    # ------------------------------------------------------------------ #

    def run_loop(self, trader):
        """Blocking loop — call from a daemon thread."""
        self._running = True
        while self._running:
            try:
                print(f"  [SCANNER] Scanning {len(config.SYMBOLS)} symbols…")
                results = self.scan_all()
                with self._results_lock:
                    self._results       = results
                    self._last_scan_ts  = time.time()
                trader.update_scan(results, self._last_scan_ts)
                buys = [r["symbol"] for r in results if r["signal"] == 1]
                print(f"  [SCANNER] Done — BUY signals: {buys[:5]} …")
            except Exception as e:
                print(f"  [SCANNER] Error: {e}")
            time.sleep(config.SCAN_INTERVAL)

    def get_results(self) -> list[dict]:
        with self._results_lock:
            return list(self._results)

    @property
    def last_scan_ts(self) -> float:
        return self._last_scan_ts
