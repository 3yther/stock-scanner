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

from quant import crypto_data, crypto_db, tjr_strategy

UTC = timezone.utc

INITIAL_BALANCE = 1000.0
BASE_DCA_USD    = 10.0
INTERVAL_HOURS  = 6          # base DCA cadence
LOOP_SECONDS    = 60         # how often the bot wakes
CANDLE_LIMIT    = 300        # 5m candles pulled per cycle (~25h)

MSS_MULT = 2.5
FVG_MULT = 1.5

# Optional hook set by Phase 5 (email alerts): fn(kind, symbol, price, mult)
on_signal = None


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

    def _buy(self, symbol, usd, price, type_, multiplier):
        if price <= 0:
            return
        usd = min(usd, self.balance)
        if usd < 0.01:
            print(f"[DCA] {symbol} {type_}: insufficient balance, skipping", flush=True)
            return
        qty = usd / price
        with self._lock:
            self.balance -= usd
            self.holdings[symbol] += qty
        ts = datetime.now(UTC).isoformat()
        crypto_db.log_buy(symbol, ts, type_, round(usd, 2), round(price, 2), multiplier)
        print(f"[DCA] BUY {symbol} ${usd:,.2f} @ ${price:,.2f} "
              f"({type_} ×{multiplier}) | bal ${self.balance:,.2f} | {symbol} {self.holdings[symbol]:.6f}",
              flush=True)
        if on_signal and type_ in ("tjr_mss", "tjr_fvg"):
            try:
                on_signal(type_, symbol, price, multiplier)
            except Exception as exc:
                print(f"[DCA] signal hook error: {exc}", flush=True)

    # ── One cycle ─────────────────────────────────────────────────────────

    def _cycle(self):
        now = datetime.now(UTC)
        for sym in self.symbols:
            try:
                candles = crypto_data.get_candles(sym, "5m", CANDLE_LIMIT)
                if not candles:
                    continue
                price = float(candles[-1]["close"])
                self.prices[sym] = price

                res = tjr_strategy.process(sym, candles, live_price=price, now=now)

                # Bullish MSS → 2.5× immediate; Bearish MSS → skip next interval.
                if res.get("mss"):
                    if res["mss"]["direction"] == "bull":
                        self._buy(sym, self.base_dca_usd * MSS_MULT, price, "tjr_mss", MSS_MULT)
                    else:
                        self._skip_next[sym] = True
                        print(f"[DCA] {sym} bearish MSS — next interval buy skipped", flush=True)

                # Price entered a Bullish FVG zone → 1.5×.
                for f in res.get("filled_fvgs", []):
                    if f.get("direction") == "bull":
                        self._buy(sym, self.base_dca_usd * FVG_MULT, price, "tjr_fvg", FVG_MULT)
                        break   # at most one FVG buy per cycle per symbol

                # Scheduled interval DCA.
                if time.time() >= self._next_interval[sym]:
                    if self._skip_next[sym]:
                        self._skip_next[sym] = False
                        print(f"[DCA] {sym} interval buy skipped (bearish MSS)", flush=True)
                    else:
                        self._buy(sym, self.base_dca_usd, price, "dca_interval", 1.0)
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
            }


def _epoch(iso_or_ts) -> float:
    try:
        s = str(iso_or_ts)
        if s.isdigit():
            return float(s)
        return datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp()
    except Exception:
        return 0.0
