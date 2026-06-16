"""
dca_bot.py — paper-trading DCA bot for BTC/ETH, modulated by TJR signals.

Runs 24/7 in its own thread, fully independent of the stock bot's market-hours
loop. Buys only (DCA accumulation); the ledger lives in tjr_buys — it NEVER
touches the stock `trades` table.

Base behaviour : buy BASE_DCA_USD of each symbol every INTERVAL_HOURS.
TJR modulation :
  - Bullish MSS detected            → buy 2.5× base immediately  (type=tjr_mss)
  - Price enters a Bullish FVG zone → buy 1.5× base              (type=tjr_fvg)
  - Bearish MSS detected            → skip the NEXT scheduled interval buy
  - No sweep yet                    → normal interval DCA continues

State (balance / holdings / next interval) is reconstructed from the tjr_buys
ledger on startup, so restarts don't double-buy.
"""

import threading
import time
from datetime import datetime, timezone

import config
import notifier
from quant import crypto_data, crypto_db, tjr_strategy, regime

UTC = timezone.utc

INITIAL_BALANCE = 1000.0
BASE_DCA_USD    = 10.0
INTERVAL_HOURS  = 6          # base DCA cadence
LOOP_SECONDS    = 60         # how often the bot wakes
CANDLE_LIMIT    = 300        # 5m candles pulled per cycle (~25h)

MSS_MULT = 2.5
FVG_MULT = 1.5


class CryptoDCABot:
    def __init__(self, symbols=("BTC", "ETH"), initial_balance=INITIAL_BALANCE,
                 base_dca_usd=BASE_DCA_USD, interval_hours=INTERVAL_HOURS,
                 loop_seconds=LOOP_SECONDS):
        self.symbols         = list(symbols)
        self.initial_balance = initial_balance
        self.base_dca_usd    = base_dca_usd
        self.interval_s      = interval_hours * 3600
        self.loop_seconds    = loop_seconds

        self.balance  = initial_balance
        self.holdings = {s: 0.0 for s in self.symbols}     # qty per symbol
        self.prices   = {s: 0.0 for s in self.symbols}
        self._skip_next   = {s: False for s in self.symbols}
        self._next_interval = {s: 0.0 for s in self.symbols}
        self._regime      = {s: "UNKNOWN" for s in self.symbols}   # current regime per symbol
        self._regime_data = {s: {} for s in self.symbols}
        self._regime_ts   = 0.0
        self._lock    = threading.Lock()
        self._running = False
        self._reconstruct()

    # ── State from the ledger (survives restarts) ─────────────────────────

    def _reconstruct(self):
        """Rebuild balance / holdings / interval schedule from tjr_buys."""
        spent = 0.0
        last_interval = {s: 0.0 for s in self.symbols}
        for b in crypto_db.get_recent_buys(5000):
            sym = b["symbol"]
            if sym not in self.holdings:
                continue
            usd, price = float(b["usd_amount"]), float(b["price"])
            spent += usd
            if price > 0:
                self.holdings[sym] += usd / price
            if b["type"] == "dca_interval":
                ts = _epoch(b["timestamp"])
                last_interval[sym] = max(last_interval[sym], ts)
        self.balance = max(0.0, self.initial_balance - spent)
        now = time.time()
        for s in self.symbols:
            # Next interval is one period after the last one (or "now" if none).
            self._next_interval[s] = (last_interval[s] + self.interval_s) if last_interval[s] else now
        print(f"[DCA] reconstructed: balance ${self.balance:,.2f} "
              f"holdings={{ {', '.join(f'{s}:{self.holdings[s]:.6f}' for s in self.symbols)} }}", flush=True)

    # ── Buying ────────────────────────────────────────────────────────────

    def _buy(self, symbol, usd, price, type_, multiplier, regime_=None):
        if price <= 0:
            return
        usd = min(usd, self.balance)
        if usd < 0.01:
            print(f"[DCA] {symbol} {type_}: insufficient balance, skipping", flush=True)
            return
        reg = regime_ or self._regime.get(symbol)
        qty = usd / price
        with self._lock:
            self.balance -= usd
            self.holdings[symbol] += qty
        ts = datetime.now(UTC).isoformat()
        crypto_db.log_buy(symbol, ts, type_, round(usd, 2), round(price, 2), multiplier, reg)
        print(f"[DCA] BUY {symbol} ${usd:,.2f} @ ${price:,.2f} "
              f"({type_} ×{multiplier} · {reg}) | bal ${self.balance:,.2f} | {symbol} {self.holdings[symbol]:.6f}",
              flush=True)

    # ── Regime (re-checked slowly; 1h regime barely moves) ────────────────

    def _refresh_regime(self):
        """Recompute the per-symbol regime (always — so it's logged/displayed
        even when the filter is off; only the *policy* is gated by the flag)."""
        if time.time() - self._regime_ts < config.CRYPTO_REGIME_REFRESH:
            return
        self._regime_ts = time.time()
        for s in self.symbols:
            try:
                r = regime.for_symbol(s, config.CRYPTO_REGIME_INTERVAL)
                self._regime[s] = r.get("regime", "UNKNOWN")
                self._regime_data[s] = r
            except Exception as exc:
                print(f"[DCA] regime {s} error: {exc}", flush=True)
        print(f"[DCA] regime: {{ {', '.join(f'{s}:{self._regime[s]}' for s in self.symbols)} }}"
              f" (filter={'ON' if config.CRYPTO_REGIME_FILTER else 'OFF'})", flush=True)

    def _alert(self, headline: str, symbol: str, signal_price: float):
        """Email a Crypto Bot TJR alert (no-op if email isn't configured)."""
        print(f"[DCA] ALERT {headline} @ ${signal_price:,.2f}", flush=True)
        try:
            notifier.crypto_alert(headline, symbol, signal_price, dict(self.prices))
        except Exception as exc:
            print(f"[DCA] alert error: {exc}", flush=True)

    # ── One cycle ─────────────────────────────────────────────────────────

    def _cycle(self):
        now = datetime.now(UTC)
        self._refresh_regime()
        for sym in self.symbols:
            try:
                candles = crypto_data.get_candles(sym, "5m", CANDLE_LIMIT)
                if not candles:
                    continue
                price = float(candles[-1]["close"])
                self.prices[sym] = price

                res = tjr_strategy.process(sym, candles, live_price=price, now=now)

                # Regime-aware sizing policy (flag gates whether it's applied).
                reg = self._regime.get(sym, "UNKNOWN")
                pol = regime.sizing_policy(reg) if config.CRYPTO_REGIME_FILTER \
                      else regime.sizing_policy("UNKNOWN")   # UNKNOWN = full weight

                # Bullish MSS → scaled buy (regime-permitting); Bearish → skip next interval.
                if res.get("mss"):
                    mdir   = res["mss"]["direction"]
                    sig_px = float(res["mss"]["price"])
                    if mdir == "bull":
                        if pol["mss_enabled"] and pol["mss"] > 0:
                            self._buy(sym, self.base_dca_usd * pol["mss"], price, "tjr_mss", pol["mss"], reg)
                            self._alert(f"Crypto Bot TJR: Bullish MSS on {sym} — buying {pol['mss']:g}×", sym, sig_px)
                        else:
                            self._alert(f"Crypto Bot TJR: Bullish MSS on {sym} — scaling suppressed ({reg})", sym, sig_px)
                    else:
                        self._skip_next[sym] = True
                        print(f"[DCA] {sym} bearish MSS — next interval buy skipped", flush=True)
                        self._alert(f"Crypto Bot TJR: Bearish MSS on {sym} — skipping next buy", sym, sig_px)

                # FVG zone entries: bullish → buy (regime mult); bearish → alert only.
                filled = res.get("filled_fvgs", [])
                if any(f.get("direction") == "bull" for f in filled):
                    if pol["fvg_enabled"] and pol["fvg"] > 0:
                        self._buy(sym, self.base_dca_usd * pol["fvg"], price, "tjr_fvg", pol["fvg"], reg)
                        self._alert(f"Crypto Bot TJR: Price in Bullish FVG on {sym} — buying {pol['fvg']:g}×", sym, price)
                    else:
                        self._alert(f"Crypto Bot TJR: Price in Bullish FVG on {sym} — suppressed ({reg})", sym, price)
                if any(f.get("direction") == "bear" for f in filled):
                    self._alert(f"Crypto Bot TJR: Price in Bearish FVG on {sym} — watching", sym, price)

                # Scheduled interval DCA (regime scales or pauses it).
                if time.time() >= self._next_interval[sym]:
                    if self._skip_next[sym]:
                        self._skip_next[sym] = False
                        print(f"[DCA] {sym} interval buy skipped (bearish MSS)", flush=True)
                    elif pol["interval"] <= 0:
                        print(f"[DCA] {sym} interval buy paused ({reg})", flush=True)
                    else:
                        self._buy(sym, self.base_dca_usd * pol["interval"], price, "dca_interval", pol["interval"], reg)
                    self._next_interval[sym] = time.time() + self.interval_s

            except Exception as exc:
                print(f"[DCA] {sym} cycle error: {exc}", flush=True)

    # ── Thread loop ───────────────────────────────────────────────────────

    def run(self):
        self._running = True
        print(f"[DCA] Crypto Bot started — base ${self.base_dca_usd}/",
              f"{int(self.interval_s/3600)}h, symbols {self.symbols}, source={crypto_data.source()}",
              flush=True)
        while self._running:
            try:
                self._cycle()
            except Exception as exc:
                print(f"[DCA] loop error: {exc}", flush=True)
            time.sleep(self.loop_seconds)

    def stop(self):
        self._running = False

    # ── Dashboard / API snapshot ──────────────────────────────────────────

    def status(self) -> dict:
        with self._lock:
            holdings_value = sum(self.holdings[s] * self.prices.get(s, 0.0) for s in self.symbols)
            equity = self.balance + holdings_value
            return {
                "initial_balance": round(self.initial_balance, 2),
                "balance":         round(self.balance, 2),
                "holdings_value":  round(holdings_value, 2),
                "equity":          round(equity, 2),
                "total_pnl":       round(equity - self.initial_balance, 2),
                "total_pnl_pct":   round((equity / self.initial_balance - 1) * 100, 2) if self.initial_balance else 0.0,
                "holdings":        {s: round(self.holdings[s], 8) for s in self.symbols},
                "prices":          {s: round(self.prices.get(s, 0.0), 2) for s in self.symbols},
                "base_dca_usd":    self.base_dca_usd,
                "interval_hours":  int(self.interval_s / 3600),
                "source":          crypto_data.source(),
                "regime":          {s: self._regime.get(s, "UNKNOWN") for s in self.symbols},
                "regime_filter":   config.CRYPTO_REGIME_FILTER,
            }


def _epoch(iso_or_ts) -> float:
    try:
        s = str(iso_or_ts)
        if s.isdigit():
            return float(s)
        return datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp()
    except Exception:
        return 0.0
