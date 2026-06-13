"""
Multi-position stock paper trader — v2.

New features:
  - Sector diversification: max 1 position per sector
  - Earnings avoidance: skip BUYs with earnings within ~2 trading days
  - Market regime: halve position size + require higher score in BEARISH regime
  - Break-even stop: once a position is up BREAKEVEN_TRIGGER %, SL moves to entry
  - Time-based exit: force-close after MAX_HOLD_DAYS trading days (dead money)
  - Hourly equity snapshots saved to database for /stats equity curve
"""

import threading
import time
from datetime import date, datetime, timedelta, timezone

import config
import database as db
import market_data
import notifier
import strategies

# Printed at import time — confirms which DB the trader module sees
print(
    f"[TRADER DB] backend={'postgresql' if db.USE_PG else 'sqlite'}"
    f"  location={db.db_location()}"
    f"  module_id={id(db)}",
    flush=True,
)


class StockPaperTrader:
    def __init__(self):
        self.balance         = config.INITIAL_BALANCE
        self.initial_balance = config.INITIAL_BALANCE

        # Open positions: symbol → {shares, entry_price, highest_price, size_usd,
        #                            entry_date, breakeven_active}
        self.positions: dict[str, dict] = {}

        self.current_prices: dict[str, float] = {}
        self._trades: list[dict] = []
        self._lock    = threading.Lock()
        self._running = False

        # Daily loss tracking
        self.daily_pnl    = self._load_today_pnl()
        self.trading_date = datetime.now(timezone.utc).date()
        self.daily_halted = False

        # Kill switch
        self.kill_switch = db.get_kill_switch()

        # Scanner state (updated by scanner thread)
        self._last_scan:    list[dict] = []
        self._last_scan_ts: float      = 0.0
        self._market_regime: dict      = {"regime": "NEUTRAL", "spy_price": 0.0, "spy_50sma": 0.0}
        self._scan_lock = threading.Lock()
        self._processed_scan_ts: float = 0.0

        # Equity snapshot throttle
        self._last_snapshot_ts: float = 0.0

    # ------------------------------------------------------------------ #
    #  Bootstrap                                                           #
    # ------------------------------------------------------------------ #

    def _load_today_pnl(self) -> float:
        today = datetime.now(timezone.utc).date().isoformat()
        try:
            return sum(
                t["pnl"] for t in db.get_recent_trades(500)
                if t["timestamp"][:10] == today and t["action"] != "BUY"
            )
        except Exception:
            return 0.0

    # ------------------------------------------------------------------ #
    #  Scanner interface                                                   #
    # ------------------------------------------------------------------ #

    def update_scan(self, results: list[dict], ts: float, regime: dict):
        with self._scan_lock:
            self._last_scan     = results
            self._last_scan_ts  = ts
            self._market_regime = regime

    def _get_scan(self) -> tuple[list[dict], float, dict]:
        with self._scan_lock:
            return list(self._last_scan), self._last_scan_ts, dict(self._market_regime)

    # ------------------------------------------------------------------ #
    #  Daily state                                                         #
    # ------------------------------------------------------------------ #

    def _check_daily_reset(self):
        today = datetime.now(timezone.utc).date()
        if today != self.trading_date:
            self._save_equity_snapshot()  # save yesterday's final equity
            self.trading_date = today
            self.daily_pnl    = 0.0
            self.daily_halted = False
            print("  [DAILY] UTC midnight — daily counter reset.")

    def _check_daily_limit(self):
        limit = -(config.MAX_DAILY_LOSS * self.initial_balance)
        if self.daily_pnl <= limit and not self.daily_halted:
            self.daily_halted = True
            print(f"  [DAILY] Loss limit hit (P&L ${self.daily_pnl:,.2f}) — halting.")

    # ------------------------------------------------------------------ #
    #  Equity snapshot                                                     #
    # ------------------------------------------------------------------ #

    def _save_equity_snapshot(self):
        now = time.time()
        if now - self._last_snapshot_ts < 3600:
            return
        pos_value = sum(
            self.current_prices.get(s, p["entry_price"]) * p["shares"]
            for s, p in self.positions.items()
        )
        equity = self.balance + pos_value
        db.save_equity_snapshot(equity, self.balance, pos_value)
        self._last_snapshot_ts = now

    # ------------------------------------------------------------------ #
    #  Utilities                                                           #
    # ------------------------------------------------------------------ #

    @staticmethod
    def _count_trading_days(start: date, end: date) -> int:
        """Count Mon-Fri days strictly between start and end (exclusive of start)."""
        return strategies.count_trading_days(start, end)

    # ------------------------------------------------------------------ #
    #  Trade execution                                                     #
    # ------------------------------------------------------------------ #

    def _execute_buy(self, symbol: str, price: float, regime: str = "NEUTRAL"):
        if symbol in self.positions:
            return
        if len(self.positions) >= config.MAX_POSITIONS:
            return
        if price <= 0:
            return

        # Sector diversification: only one position per sector (shared rule)
        if strategies.sector_blocked(symbol, self.positions.keys()):
            sym_sector = config.SECTOR_MAP.get(symbol, "Other")
            print(f"  [TRADER] Skip {symbol} — sector '{sym_sector}' already held")
            return

        # Position size (halved in bear regime) — shared rule
        size_usd = strategies.position_size_usd(self.balance, regime)
        if size_usd > self.balance:
            return
        shares        = size_usd / price
        self.balance -= size_usd
        self.positions[symbol] = {
            "shares":           shares,
            "entry_price":      price,
            "highest_price":    price,
            "size_usd":         size_usd,
            "entry_date":       datetime.now(timezone.utc).date().isoformat(),
            "breakeven_active": False,
        }

        sl = round(price * (1 - config.STOP_LOSS_PCT), 2)
        tp = round(price * (1 + config.TAKE_PROFIT_PCT), 2)

        trade = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "symbol":    symbol,
            "action":    "BUY",
            "price":     price,
            "shares":    shares,
            "pnl":       0.0,
            "balance":   self.balance,
        }
        self._trades.append(trade)
        try:
            print(f"[TRADE WRITE] BUY {symbol} @ {price} -> DB={db.db_location()}", flush=True)
            db.log_trade(trade)
        except Exception as _dbe:
            print(f"[TRADE WRITE] ERROR BUY {symbol}: {_dbe}", flush=True)
        notifier.trade_alert("BUY", symbol, price, shares, 0.0, self.balance, sl, tp)
        regime_tag = f" [BEAR×{config.BEAR_POSITION_SCALE}]" if regime == "BEARISH" else ""
        print(
            f"  [PAPER] BUY   {symbol:<6} {shares:.4f} sh @ ${price:>10,.2f}"
            f"  |  SL ${sl:,.2f}  TP ${tp:,.2f}  |  cash ${self.balance:,.2f}{regime_tag}"
        )

    def _close_position(self, symbol: str, price: float, action: str):
        pos = self.positions.get(symbol)
        if pos is None:
            return
        pnl           = (price - pos["entry_price"]) * pos["shares"]
        proceeds      = pos["shares"] * price
        self.balance += proceeds
        self.daily_pnl += pnl
        sign = "+" if pnl >= 0 else ""

        trade = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "symbol":    symbol,
            "action":    action,
            "price":     price,
            "shares":    pos["shares"],
            "pnl":       pnl,
            "balance":   self.balance,
        }
        self._trades.append(trade)
        try:
            print(f"[TRADE WRITE] {action} {symbol} @ {price} -> DB={db.db_location()}", flush=True)
            db.log_trade(trade)
        except Exception as _dbe:
            print(f"[TRADE WRITE] ERROR {action} {symbol}: {_dbe}", flush=True)
        notifier.trade_alert(action, symbol, price, pos["shares"], pnl, self.balance)

        label = f"[{action:<16}]"
        print(
            f"  [PAPER] {label} {symbol:<6} {pos['shares']:.4f} sh @ ${price:>10,.2f}"
            f"  |  P&L: {sign}${abs(pnl):,.2f}  |  cash ${self.balance:,.2f}"
        )
        del self.positions[symbol]

    # ------------------------------------------------------------------ #
    #  Position management                                                 #
    # ------------------------------------------------------------------ #

    def _refresh_prices(self):
        symbols_to_check = set(self.positions.keys())
        scan, _, _ = self._get_scan()
        for r in scan:
            if r["signal"] == 1:
                symbols_to_check.add(r["symbol"])
            if len(symbols_to_check) >= 10:
                break
        for sym in symbols_to_check:
            p = market_data.get_price(sym)
            if p:
                with self._lock:
                    self.current_prices[sym] = p

    def _check_exits(self):
        today = datetime.now(timezone.utc).date()
        for sym in list(self.positions.keys()):
            price = self.current_prices.get(sym)
            if not price:
                continue
            pos = self.positions[sym]

            # Shared trailing-stop / break-even / take-profit rule. Mutates pos.
            be_before = pos["breakeven_active"]
            action    = strategies.manage_position(pos, price)
            if not be_before and pos["breakeven_active"]:
                print(
                    f"  [PAPER] BE STOP  {sym:<6} — SL locked at entry"
                    f" ${pos['entry_price']:,.2f}"
                )

            if action:
                self._close_position(sym, price, action)
            else:
                # Time-based exit after MAX_HOLD_DAYS trading days
                entry_date = date.fromisoformat(pos["entry_date"])
                hold_days  = self._count_trading_days(entry_date, today)
                if hold_days >= config.MAX_HOLD_DAYS:
                    self._close_position(sym, price, "TIME EXIT")

    def _process_scan(self, scan: list[dict], regime: str):
        scan_by_sym = {r["symbol"]: r for r in scan}

        # Close positions where signal turned bearish
        for sym in list(self.positions.keys()):
            r = scan_by_sym.get(sym)
            if r and r["signal"] == -1:
                price = self.current_prices.get(sym) or r["price"]
                if price:
                    self._close_position(sym, price, "SELL")

        # Min score threshold based on regime (shared rule)
        min_score = strategies.min_score_for_regime(regime)

        # Top BUY candidates filtered by score, earnings, sector
        top_buys = [
            r for r in scan
            if r["signal"] == 1
            and not r.get("earnings_soon", False)
            and r["score"] >= min_score
        ]

        for opp in top_buys:
            if len(self.positions) >= config.MAX_POSITIONS:
                break
            sym = opp["symbol"]
            if sym in self.positions:
                continue
            price = self.current_prices.get(sym) or opp["price"]
            if price > 0:
                self._execute_buy(sym, price, regime)

    # ------------------------------------------------------------------ #
    #  Main loop                                                           #
    # ------------------------------------------------------------------ #

    def run(self):
        self._running = True
        print(f"\n  Balance      : ${self.balance:,.2f} (paper)")
        print(f"  Max positions: {config.MAX_POSITIONS}")
        print(f"  Position size: {config.TRADE_SIZE_PCT*100:.0f}%  (BEAR: {config.TRADE_SIZE_PCT*config.BEAR_POSITION_SCALE*100:.1f}%)")
        print(f"  Trail stop   : {config.STOP_LOSS_PCT*100:.0f}%   BE trigger: {config.BREAKEVEN_TRIGGER*100:.0f}%")
        print(f"  TP           : {config.TAKE_PROFIT_PCT*100:.0f}%   Max hold: {config.MAX_HOLD_DAYS} trading days")
        print(f"  Daily limit  : {config.MAX_DAILY_LOSS*100:.0f}%   Loop: every {config.TRADE_LOOP_INTERVAL}s\n")

        while self._running:
            try:
                self._refresh_prices()

                with self._lock:
                    self._check_daily_reset()
                    self.kill_switch = db.get_kill_switch()
                    self._check_daily_limit()
                    halted = self.kill_switch or self.daily_halted
                    self._check_exits()

                    scan, scan_ts, regime_data = self._get_scan()
                    regime = regime_data.get("regime", "NEUTRAL")
                    if not halted and scan_ts > self._processed_scan_ts and scan:
                        self._process_scan(scan, regime)
                        self._processed_scan_ts = scan_ts

                    self._save_equity_snapshot()

            except Exception as e:
                print(f"  [TRADER] Error: {e}")

            time.sleep(config.TRADE_LOOP_INTERVAL)

    def stop(self):
        self._running = False

    # ------------------------------------------------------------------ #
    #  Dashboard data                                                      #
    # ------------------------------------------------------------------ #

    def status(self) -> dict:
        with self._lock:
            today = datetime.now(timezone.utc).date()
            pos_value = sum(
                self.current_prices.get(sym, pos["entry_price"]) * pos["shares"]
                for sym, pos in self.positions.items()
            )
            equity    = self.balance + pos_value
            total_pnl = equity - self.initial_balance

            closed   = [t for t in self._trades if t["action"] != "BUY"]
            wins     = [t for t in closed if t["pnl"] > 0]
            win_rate = len(wins) / len(closed) * 100 if closed else 0.0

            daily_limit = -(config.MAX_DAILY_LOSS * self.initial_balance)

            halt_reason = ""
            if self.kill_switch:
                halt_reason = "Kill switch"
            elif self.daily_halted:
                halt_reason = "Daily loss limit"

            regime_data = dict(self._market_regime)

            positions_list = []
            for sym, pos in self.positions.items():
                price    = self.current_prices.get(sym, pos["entry_price"])
                upnl     = (price - pos["entry_price"]) * pos["shares"]
                upnl_pct = (price - pos["entry_price"]) / pos["entry_price"] * 100 \
                           if pos["entry_price"] else 0
                trail_sl = pos["highest_price"] * (1 - config.STOP_LOSS_PCT)
                if pos["breakeven_active"]:
                    trail_sl = max(trail_sl, pos["entry_price"])
                tp_price = pos["entry_price"] * (1 + config.TAKE_PROFIT_PCT)
                entry_d  = date.fromisoformat(pos["entry_date"])
                hold_days = self._count_trading_days(entry_d, today)
                positions_list.append({
                    "symbol":            sym,
                    "sector":            config.SECTOR_MAP.get(sym, "Other"),
                    "shares":            round(pos["shares"], 4),
                    "entry_price":       round(pos["entry_price"], 2),
                    "current_price":     round(price, 2),
                    "highest_price":     round(pos["highest_price"], 2),
                    "unrealized_pnl":    round(upnl, 2),
                    "unrealized_pct":    round(upnl_pct, 2),
                    "stop_loss_price":   round(trail_sl, 2),
                    "take_profit_price": round(tp_price, 2),
                    "breakeven_active":  pos["breakeven_active"],
                    "hold_days":         hold_days,
                    "size_usd":          round(pos["size_usd"], 2),
                })

            return {
                "balance":         round(self.balance, 2),
                "initial_balance": self.initial_balance,
                "equity":          round(equity, 2),
                "total_pnl":       round(total_pnl, 2),
                "total_pnl_pct":   round(total_pnl / self.initial_balance * 100, 4),
                "num_positions":   len(self.positions),
                "max_positions":   config.MAX_POSITIONS,
                "total_trades":    len(closed),
                "win_rate":        round(win_rate, 1),
                "daily_pnl":       round(self.daily_pnl, 2),
                "daily_limit":     round(daily_limit, 2),
                "daily_halted":    self.daily_halted,
                "kill_switch":     self.kill_switch,
                "halted":          self.kill_switch or self.daily_halted,
                "halt_reason":     halt_reason,
                "positions":       positions_list,
                "market_regime":   regime_data.get("regime", "NEUTRAL"),
                "spy_price":       regime_data.get("spy_price", 0.0),
                "spy_50sma":       regime_data.get("spy_50sma", 0.0),
                "trade_size_pct":  config.TRADE_SIZE_PCT * 100,
                "stop_loss_pct":   config.STOP_LOSS_PCT  * 100,
                "take_profit_pct": config.TAKE_PROFIT_PCT * 100,
            }
