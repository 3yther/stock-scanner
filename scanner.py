"""
Multi-timeframe scanner — v2.

Always-on scoring (0–100) for all 60 symbols every scan:
  40 pts  trend         — 1D MACD bullish
  25 pts  entry signal  — fresh 1H bullish cross (25), holding (12), else 0
  20 pts  volume        — tiered: ≥2× avg (20), ≥1.5× (15), ≥1.2× (10), ≥1× (5), <1× (0)
  15 pts  rel. strength — 20-day return vs SPY: >+3% (15), >+1% (10), >0% (5), ≤0% (0)
"""

import sys
import threading
import time
import traceback

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
        print(f"  [SCANNER] scoring {symbol}…", flush=True)
        df_d, df_h = market_data.get_ohlc(symbol)

        if df_d is None or len(df_d) < 26:
            print(f"  [SCANNER] {symbol} → no_data (df_d rows={len(df_d) if df_d is not None else 'None'})", flush=True)
            return self._base_result(symbol)

        cross_d, bullish_d = strategies.macd_state(df_d)
        cross_h, bullish_h = 0, False
        if df_h is not None and len(df_h) >= 26:
            cross_h, bullish_h = strategies.macd_state(df_h)

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
        rs = market_data.get_relative_strength(symbol)
        if   rs > 3.0: rs_pts = 15.0
        elif rs > 1.0: rs_pts = 10.0
        elif rs > 0.0: rs_pts = 5.0
        else:          rs_pts = 0.0

        score = trend_pts + signal_pts + vol_pts + rs_pts

        earnings_soon = market_data.get_earnings_flag(symbol)

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

        print(
            f"  [SCANNER] {symbol} → signal={label} score={score:.0f}"
            f"  trend={'↑' if bullish_d else '↓'} vol={vol_ratio:.1f}x rs={rs:+.1f}%",
            flush=True,
        )
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

    _MAX_SCAN_SECS = 120  # raised to accommodate verbose debug logging

    def scan_all(self) -> tuple[list[dict], dict]:
        scan_start = time.time()
        print(f"  [SCANNER] scan_all() start", flush=True)

        print(f"  [SCANNER] calling batch_scan_populate…", flush=True)
        market_data.batch_scan_populate(config.SYMBOLS)
        print(f"  [SCANNER] batch_scan_populate done ({time.time()-scan_start:.1f}s)", flush=True)

        print(f"  [SCANNER] fetching SPY regime…", flush=True)
        spy_regime = market_data.get_spy_regime()
        print(f"  [SCANNER] regime={spy_regime['regime']} spy=${spy_regime['spy_price']}", flush=True)

        results: list[dict] = []
        timed_out = False

        for i, sym in enumerate(config.SYMBOLS):
            elapsed = time.time() - scan_start
            if elapsed > self._MAX_SCAN_SECS:
                remaining = len(config.SYMBOLS) - i
                print(
                    f"  [SCANNER] {self._MAX_SCAN_SECS}s limit at symbol {i}/{len(config.SYMBOLS)}"
                    f" — skipping {remaining} symbols",
                    flush=True,
                )
                timed_out = True
                break

            try:
                results.append(self._score_symbol(sym, spy_regime))
            except Exception as e:
                print(f"  [SCANNER] {sym} scoring exception: {e}", flush=True)
                traceback.print_exc(file=sys.stdout)
                sys.stdout.flush()
                results.append({**self._base_result(sym), "no_data": True})

        # Pad skipped symbols with no_data
        scored_syms = {r["symbol"] for r in results}
        for sym in config.SYMBOLS:
            if sym not in scored_syms:
                results.append({**self._base_result(sym), "no_data": True})

        results.sort(key=lambda x: x["score"], reverse=True)
        elapsed_total = time.time() - scan_start
        scored_count  = sum(1 for r in results if not r.get("no_data"))
        buys          = [r["symbol"] for r in results if r["signal"] == 1]
        print(
            f"  [SCANNER] scan_all() COMPLETE — {scored_count}/{len(config.SYMBOLS)} scored"
            f"  in {elapsed_total:.1f}s  regime={spy_regime['regime']}"
            f"  BUY signals: {buys or 'none'}"
            f"  {'(PARTIAL timeout)' if timed_out else ''}",
            flush=True,
        )
        return results, spy_regime

    @staticmethod
    def _base_result(symbol: str) -> dict:
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

    # ------------------------------------------------------------------ #
    #  Thread                                                              #
    # ------------------------------------------------------------------ #

    def run_loop(self, trader):
        print(f"  [SCANNER] run_loop() entered — thread={threading.current_thread().name}", flush=True)
        self._running = True

        def _earnings_loop():
            print("  [SCANNER] earnings thread started", flush=True)
            market_data.refresh_earnings_cache(config.SYMBOLS)
            while self._running:
                time.sleep(14400)
                market_data.refresh_earnings_cache(config.SYMBOLS)

        threading.Thread(target=_earnings_loop, daemon=True, name="earnings").start()

        scan_num = 0
        while self._running:
            scan_num += 1

            # Cadence depends on whether the market is open.
            market_open = self._market_open()
            interval    = config.SCAN_INTERVAL_OPEN if market_open else config.SCAN_INTERVAL_CLOSED
            mode        = "OPEN" if market_open else "CLOSED"
            print(f"\n  [SCAN] market={mode}, next in {interval // 60}min", flush=True)
            print(f"  [SCANNER] ===== SCAN #{scan_num} START =====", flush=True)
            try:
                results, spy_regime = self.scan_all()
                ts = time.time()
                with self._lock:
                    self._results      = results
                    self._regime       = spy_regime
                    self._last_scan_ts = ts
                trader.update_scan(results, ts, spy_regime)
                print(f"  [SCANNER] ===== SCAN #{scan_num} DONE — sleeping {interval}s ({mode}) =====\n", flush=True)
            except Exception as e:
                print(f"  [SCANNER] ===== SCAN #{scan_num} EXCEPTION =====", flush=True)
                traceback.print_exc(file=sys.stdout)
                sys.stdout.flush()
            time.sleep(interval)

    @staticmethod
    def _market_open() -> bool:
        """True if US equities regular session is open right now (ET, Mon–Fri 9:30–16:00)."""
        from datetime import datetime, timezone, timedelta
        now_utc = datetime.now(timezone.utc)
        et_off  = timedelta(hours=-4 if 3 <= now_utc.month <= 11 else -5)  # rough DST
        et_now  = now_utc + et_off
        if et_now.weekday() >= 5:
            return False
        open_t  = et_now.replace(hour=9,  minute=30, second=0, microsecond=0)
        close_t = et_now.replace(hour=16, minute=0,  second=0, microsecond=0)
        return open_t <= et_now <= close_t

    def get_results(self) -> list[dict]:
        with self._lock:
            return list(self._results)

    def get_regime(self) -> dict:
        with self._lock:
            return dict(self._regime)

    @property
    def last_scan_ts(self) -> float:
        return self._last_scan_ts
