"""
backtest.py — historical simulation of the EXACT live strategy.

Runs the same multi-timeframe MACD strategy we trade live, over historical
candles, so we can see whether it makes money before risking anything. It calls
the SAME functions the live bot uses (strategies.score_symbol for entries,
strategies.manage_position for exits, plus the shared sizing / sector / regime
helpers), so the backtest and the live bot can never drift apart.

────────────────────────────────────────────────────────────────────────────
NO LOOKAHEAD BIAS — the one rule that matters
────────────────────────────────────────────────────────────────────────────
The simulation walks forward one trading day at a time. At each day D:

  • Every signal is computed from candles up to and including D's close ONLY.
    We slice each symbol's history at D and call the live scoring function on
    that slice — it physically cannot see future bars.
  • Decisions made at D's close are FILLED AT THE NEXT BAR'S OPEN (D+1), never
    at the close we just looked at. You cannot trade on a price you haven't
    seen print yet.
  • A position opened/closed at D+1's open is only marked-to-market from D+1
    onward — never retroactively into D.

Because the MACD/EMA/SMA indicators are causal (value at bar i depends only on
bars ≤ i), slicing-then-scoring is identical to having scored live at that
moment — but slicing makes the no-lookahead guarantee explicit and auditable.

Costs: 0.05% slippage on every entry and exit; fills at the next bar's open.

Read-only: this NEVER writes to the live trades database. All results live in
memory and are exposed via get_status().
────────────────────────────────────────────────────────────────────────────
"""

from __future__ import annotations

import math
import threading
import time
import traceback
from bisect import bisect_left, bisect_right
from datetime import date, datetime, timedelta

import pandas as pd

import config
import market_data
import strategies

# ── Constants ─────────────────────────────────────────────────────────────
SLIPPAGE        = 0.0005     # 0.05% on entry and exit
_REGIME_BARS    = 50         # SPY bars the regime filter needs (SMA50) — a floor
_WARMUP_MARGIN  = 25         # extra calendar days of cushion on top of the warmup
_HOURLY_WINDOW  = 300        # trailing hourly bars used for the 1H entry signal

# Sizing/exit parameters live in strategies.py so the backtester and the live
# bot share one source of truth (aliased here for readability).
_ATR_STOP_MULT  = strategies.ATR_STOP_MULT
_ATR_TRAIL_MULT = strategies.ATR_TRAIL_MULT
_MAX_HOLD_TRAIL = strategies.MAX_HOLD_TRAIL


def _warmup_calendar_days(warmup_bars: int) -> int:
    """Convert a trading-bar warmup requirement into calendar days to fetch.

    ~252 trading days per 365 calendar days → ×1.45, plus a margin for holiday
    clusters. So a 200-bar momentum warmup fetches ~315 calendar days of history
    BEFORE the start date — enough for day-one SMA200/momentum to be real.
    """
    return int(warmup_bars * 365 / 252) + _WARMUP_MARGIN

# ── Run state (single backtest at a time) ─────────────────────────────────
_state: dict = {
    "status":     "idle",   # idle | fetching | running | done | error
    "progress":   0.0,      # 0.0 .. 1.0
    "message":    "",
    "mode":       "",
    "params":     None,
    "result":     None,     # metrics dict when status == done
    "error":      None,
    "started_at": None,
    "finished_at": None,
}
_state_lock = threading.Lock()


def _set(**kw) -> None:
    with _state_lock:
        _state.update(kw)


def get_status() -> dict:
    """Thread-safe snapshot of the current/last backtest run."""
    with _state_lock:
        return dict(_state)


def is_running() -> bool:
    with _state_lock:
        return _state["status"] in ("fetching", "running")


def _annualised_sharpe(equity_curve: list[dict]) -> float:
    """Annualised Sharpe (risk-free = 0) from a daily equity curve."""
    eqs  = [pt["equity"] for pt in equity_curve]
    rets = [eqs[i] / eqs[i - 1] - 1 for i in range(1, len(eqs)) if eqs[i - 1] > 0]
    if len(rets) <= 1:
        return 0.0
    mean = sum(rets) / len(rets)
    var  = sum((r - mean) ** 2 for r in rets) / (len(rets) - 1)
    std  = math.sqrt(var)
    return (mean / std * math.sqrt(252)) if std > 0 else 0.0


# ── Public entry point ────────────────────────────────────────────────────

def start(start_date: str, end_date: str, capital: float, mode: str,
          split: str = "full", split_date: str | None = None,
          strategy: str = "macd_mtf", sizing: str = "flat",
          exit_mode: str = "fixed_tp", max_positions: int = config.MAX_POSITIONS,
          symbols: list[str] | None = None) -> bool:
    """Kick off a backtest in a background thread. Returns False if one is
    already running.

    split         : 'full' | 'train' | 'validation' — which slice of [start,end]
                    to simulate. split_date is the train/validation boundary
                    (defaults to 60% of the way through the range if not given).
    strategy      : registry key in strategies.STRATEGIES (default 'macd_mtf').
    sizing        : 'flat' (5% of cash, default) | 'vol_conviction' (ATR
                    risk-parity × conviction by rank).
    exit_mode     : 'fixed_tp' (4% TP + 2% trail + BE + max-hold, default) |
                    'trail_only' (ATR trail, no TP, extended max-hold).
    max_positions : max simultaneous open positions for THIS backtest only
                    (default = config.MAX_POSITIONS; the live bot is untouched).
    """
    if is_running():
        return False
    syms = symbols or [s for s in config.SYMBOLS if s != "SPY"]
    split     = split if split in ("full", "train", "validation") else "full"
    sizing    = sizing if sizing in ("flat", "vol_conviction") else "flat"
    exit_mode = exit_mode if exit_mode in ("fixed_tp", "trail_only") else "fixed_tp"
    try:
        max_positions = max(1, min(int(max_positions), 20))   # clamp to a sane range
    except (TypeError, ValueError):
        max_positions = config.MAX_POSITIONS
    strat = strategies.get_strategy(strategy)   # resolve/validate now
    _set(status="fetching", progress=0.0, message="Starting…", mode=mode,
         result=None, error=None, started_at=time.time(), finished_at=None,
         params={"start": start_date, "end": end_date, "capital": capital,
                 "mode": mode, "split": split, "split_date": split_date,
                 "strategy": strat.name, "sizing": sizing, "exit_mode": exit_mode,
                 "max_positions": max_positions, "symbols": syms})
    threading.Thread(
        target=_run, name="backtest",
        args=(start_date, end_date, float(capital), mode, split, split_date,
              strat.name, sizing, exit_mode, max_positions, syms),
        daemon=True,
    ).start()
    return True


# ── Data loading ──────────────────────────────────────────────────────────

class _Series:
    """A symbol's daily (and optional hourly) history with fast date lookups."""
    def __init__(self, df: pd.DataFrame):
        self.df    = df.reset_index(drop=True)
        # date (python date) of each bar, ascending — for bisect lookups
        self.dates = [ts.date() for ts in self.df["datetime"]]
        self.open  = self.df["open"].tolist()
        self.close = self.df["close"].tolist()

    def slice_through(self, d: date) -> pd.DataFrame | None:
        """All bars with date <= d (point-in-time view)."""
        i = bisect_right(self.dates, d) - 1
        return self.df.iloc[: i + 1] if i >= 0 else None

    def open_on(self, d: date) -> float | None:
        """Open price of the bar exactly on date d, else None."""
        i = bisect_left(self.dates, d)
        if i < len(self.dates) and self.dates[i] == d:
            return float(self.open[i])
        return None

    def close_asof(self, d: date) -> float | None:
        """Most recent close at or before date d (carried forward over gaps)."""
        i = bisect_right(self.dates, d) - 1
        return float(self.close[i]) if i >= 0 else None


class _HourlySeries:
    """Hourly bars for the 1H entry signal, bounded to a trailing window."""
    def __init__(self, df: pd.DataFrame):
        self.df    = df.reset_index(drop=True)
        self.dates = [ts.date() for ts in self.df["datetime"]]

    def slice_through(self, d: date) -> pd.DataFrame | None:
        """Trailing window of hourly bars with date <= d (point-in-time)."""
        i = bisect_right(self.dates, d) - 1
        if i < 0:
            return None
        lo = max(0, i + 1 - _HOURLY_WINDOW)
        return self.df.iloc[lo: i + 1]


# ── The simulation ────────────────────────────────────────────────────────

def _run(start_date: str, end_date: str, capital: float, mode: str,
         split: str, split_date: str | None, strategy: str,
         sizing: str, exit_mode: str, max_positions: int,
         symbols: list[str]) -> None:
    try:
        strat = strategies.get_strategy(strategy)
        print(f"[BT] sizing = {sizing} | exit = {exit_mode} | "
              f"max_positions = {max_positions}", flush=True)
        user_start  = datetime.strptime(start_date, "%Y-%m-%d").date()
        end         = datetime.strptime(end_date, "%Y-%m-%d").date()

        # Train/validation boundary. Default: 60% of the way through the range.
        if split_date:
            split_dt = datetime.strptime(split_date[:10], "%Y-%m-%d").date()
        else:
            span     = (end - user_start).days
            split_dt = user_start + timedelta(days=int(span * 0.60))
        split_dt = min(max(split_dt, user_start), end)   # clamp inside range

        # The slice we actually SIMULATE. We always fetch the full range below
        # (so the disk cache is shared across slices and no extra API calls are
        # made), then restrict the walk-forward window here. Train stops at the
        # split, so validation-period data can never leak into a train run.
        if   split == "train":
            sim_start, sim_end = user_start, split_dt
        elif split == "validation":
            sim_start, sim_end = split_dt, end
        else:  # full
            sim_start, sim_end = user_start, end

        # How much history this strategy needs before its first valid signal.
        # The regime filter (SMA50) is a floor every run shares.
        warmup_bars     = max(strat.warmup_bars, _REGIME_BARS)
        warmup_calendar = _warmup_calendar_days(warmup_bars)

        # Fetch from warmup BEFORE the user start, through the full end. This one
        # window covers every slice: FULL/TRAIN warm up before user_start, and
        # VALIDATION (which starts later, at split_dt) gets even more prior
        # history for free. Warmup bars are all in the PAST relative to the first
        # tradeable day, so there is no lookahead.
        fetch_start = (user_start - timedelta(days=warmup_calendar)).isoformat()

        print(f"[BT] strategy = {strat.name}", flush=True)
        print(f"[BT] strategy warmup_bars required = {strat.warmup_bars} "
              f"(engine uses {warmup_bars} incl. regime SMA50 floor; "
              f"prefetching {warmup_calendar} calendar days before {user_start})", flush=True)

        # SPY drives the trading calendar and the regime filter.
        all_syms = list(dict.fromkeys(symbols + ["SPY"]))
        hourly   = (mode == "hourly")

        # Total fetch units for progress (daily for all; hourly for tradables).
        total_fetch = len(all_syms) + (len(symbols) if hourly else 0)
        done_fetch  = 0

        daily:  dict[str, _Series]       = {}
        hourly_data: dict[str, _HourlySeries] = {}

        for sym in all_syms:
            _set(message=f"Fetching {sym} daily…",
                 progress=0.45 * done_fetch / max(total_fetch, 1))
            df = market_data.fetch_history(sym, "day", fetch_start, end_date)
            done_fetch += 1
            if df is not None and len(df):
                daily[sym] = _Series(df)

        if hourly:
            for sym in symbols:
                _set(message=f"Fetching {sym} hourly…",
                     progress=0.45 * done_fetch / max(total_fetch, 1))
                dfh = market_data.fetch_history(sym, "hour", fetch_start, end_date)
                done_fetch += 1
                if dfh is not None and len(dfh):
                    hourly_data[sym] = _HourlySeries(dfh)

        if "SPY" not in daily:
            print(f"[BT] insufficient data: got 0 SPY bars, need {warmup_bars} "
                  f"— SPY fetch returned nothing (rate-limited / no API key / range "
                  f"beyond plan history?)", flush=True)
            _set(status="error", error="No SPY data returned — cannot run backtest "
                 f"(need ~{warmup_bars} SPY bars). Check API key / rate limits / history depth.",
                 finished_at=time.time())
            return

        spy = daily["SPY"]
        bar_counts = {s: len(ser.df) for s, ser in daily.items()}
        min_sym    = min(bar_counts, key=bar_counts.get)
        print(f"[BT] data loaded: SPY {len(spy.df)} daily bars, "
              f"range {spy.dates[0]} to {spy.dates[-1]} "
              f"(thinnest symbol {min_sym}={bar_counts[min_sym]} bars; "
              f"{len(daily)-1}/{len(symbols)} tradeable symbols loaded)", flush=True)

        # Master calendar: SPY trading days within the SIMULATED slice only —
        # this is the TRADEABLE window. Warmup bars sit BEFORE sim_start.
        calendar      = [d for d in spy.dates if sim_start <= d <= sim_end]
        warmup_avail  = sum(1 for d in spy.dates if d < sim_start)   # bars before the window
        print(f"[BT] tradeable window after warmup: {sim_start} to {sim_end}, "
              f"{len(calendar)} trading days "
              f"({warmup_avail} warmup bars available before it)", flush=True)

        if warmup_avail < warmup_bars:
            # Not necessarily fatal (early signals just stay flat), but flag it so
            # a thin warmup is never mistaken for a strategy that 'does nothing'.
            print(f"[BT] insufficient data: got {warmup_avail} warmup bars before "
                  f"{sim_start}, need {warmup_bars} — early signals will be no-data "
                  f"(extend the start date or fetch more history).", flush=True)

        # Need a previous bar to fill against, and SPY bars for the regime.
        if len(calendar) < 2:
            avail_first = spy.dates[0] if spy.dates else "—"
            avail_last  = spy.dates[-1] if spy.dates else "—"
            reason = (
                f"no SPY trading days fall inside {sim_start}..{sim_end}; "
                f"loaded data only covers {avail_first}..{avail_last}"
            )
            print(f"[BT] insufficient data: 0 tradeable days — {reason}. "
                  f"This is a DATA-COVERAGE problem, not a slice bug.", flush=True)
            _set(status="error",
                 error=f"{split.upper()} slice has no trading days "
                       f"({sim_start} → {sim_end}). Loaded data covers "
                       f"{avail_first} → {avail_last}. Likely the requested range is "
                       f"outside your Polygon history depth, or the fetch was rate-limited.",
                 finished_at=time.time())
            return

        tradables = [s for s in symbols if s in daily]

        # ---- Simulation state ----
        balance   = capital
        positions: dict[str, dict] = {}
        trades:    list[dict]      = []      # closed round-trips
        equity_curve: list[dict]   = []
        pending_exits:  list[tuple[str, str]] = []   # (symbol, action) → fill next open
        pending_entries: list[dict]           = []   # score dicts → fill next open

        def _open_position(sym: str, fill_open: float, fdate: date,
                           size_usd: float, atr_at_entry: float | None) -> None:
            nonlocal balance
            entry_px = fill_open * (1 + SLIPPAGE)
            if entry_px <= 0 or size_usd <= 0 or size_usd > balance:
                return
            shares = size_usd / entry_px
            balance -= size_usd
            positions[sym] = {
                "shares":           shares,
                "entry_price":      entry_px,
                "highest_price":    entry_px,
                "size_usd":         size_usd,
                "entry_date":       fdate.isoformat(),
                "breakeven_active": False,
                "atr":              atr_at_entry,   # ATR at entry (for trail_only)
            }

        def _close_position(sym: str, fill_open: float, fdate: date, action: str) -> None:
            nonlocal balance
            pos = positions.pop(sym, None)
            if pos is None:
                return
            exit_px  = fill_open * (1 - SLIPPAGE)
            pnl      = (exit_px - pos["entry_price"]) * pos["shares"]
            balance += pos["shares"] * exit_px
            entry_d  = date.fromisoformat(pos["entry_date"])
            trades.append({
                "symbol":      sym,
                "sector":      config.SECTOR_MAP.get(sym, "Other"),
                "entry_date":  pos["entry_date"],
                "entry_price": round(pos["entry_price"], 4),
                "exit_date":   fdate.isoformat(),
                "exit_price":  round(exit_px, 4),
                "shares":      round(pos["shares"], 6),
                "pnl":         round(pnl, 2),
                "pnl_pct":     round((exit_px / pos["entry_price"] - 1) * 100, 2),
                "exit_reason": action,
                "hold_days":   strategies.count_trading_days(entry_d, fdate),
            })

        def _size_position(sym: str, fill_open: float, opp: dict, sizing: str,
                           equity_now: float, cash: float) -> float:
            """Dollar size for a new position under the selected sizing mode.

            'flat' is exactly the old behaviour (5% of cash, regime-scaled).
            'vol_conviction' targets a constant 1% risk via a 2×ATR stop, scaled
            by momentum-rank conviction, capped at 20% of equity (and cash). ATR
            comes from the decision bar (opp['_atr']) — no lookahead.
            """
            regime = opp.get("_regime", "NEUTRAL")
            if sizing != "vol_conviction":
                return strategies.position_size_usd(cash, regime)

            atr_v    = opp.get("_atr")
            rank     = opp.get("_rank", 99)
            entry_px = fill_open * (1 + SLIPPAGE)
            # Same shared sizing the live bot uses (single source of truth).
            final_size = strategies.vol_conviction_size(equity_now, cash, entry_px, atr_v, rank)
            if final_size <= 0:
                return strategies.position_size_usd(cash, regime)   # safe fallback

            stop_dist = _ATR_STOP_MULT * atr_v
            base_size = (strategies.RISK_PER_TRADE * equity_now) * entry_px / stop_dist
            print(f"[SIZING] symbol={sym} atr={atr_v:.4f} stop_dist={stop_dist:.4f} "
                  f"base_size={base_size:.2f} conviction_mult={strategies.conviction_mult(rank)} "
                  f"final_size={final_size:.2f}", flush=True)
            return final_size

        last_idx = len(calendar) - 1
        for i, D in enumerate(calendar):
            if i % 10 == 0:
                _set(status="running", progress=0.45 + 0.5 * i / len(calendar),
                     message=f"Simulating {D.isoformat()} — equity ${balance:,.0f}, "
                             f"{len(positions)} open, {len(trades)} trades")

            # 1) Execute decisions made yesterday at TODAY's open (next-bar fill).
            for sym, action in pending_exits:
                px = daily[sym].open_on(D) if sym in daily else None
                if px is not None and sym in positions:
                    _close_position(sym, px, D, action)
            # Equity at today's open — basis for vol_conviction risk/caps.
            equity_now = balance + sum(
                p["shares"] * (daily[s].open_on(D) or daily[s].close_asof(D) or p["entry_price"])
                for s, p in positions.items())
            for opp in pending_entries:
                sym = opp["symbol"]
                if len(positions) >= max_positions:   # backtest override (live bot unaffected)
                    break
                if sym in positions or strategies.sector_blocked(sym, positions.keys()):
                    continue
                px = daily[sym].open_on(D) if sym in daily else None
                if px is None or px <= 0:
                    continue
                size_usd = _size_position(sym, px, opp, sizing, equity_now, balance)
                _open_position(sym, px, D, size_usd, opp.get("_atr"))
            pending_exits, pending_entries = [], []

            # 2) Mark-to-market at TODAY's close.
            pos_val = sum(p["shares"] * (daily[s].close_asof(D) or p["entry_price"])
                          for s, p in positions.items())
            equity_curve.append({"date": D.isoformat(), "equity": round(balance + pos_val, 2)})

            # On the final bar there is no next open to fill against — stop deciding.
            if i == last_idx:
                break

            # 3) Decide at TODAY's close, using data up to and including D only.
            spy_sl = spy.slice_through(D)
            regime = strategies.spy_regime(spy_sl)["regime"]

            scores: dict[str, dict] = {}
            for sym in tradables:
                d_sl = daily[sym].slice_through(D)
                if hourly:
                    h_sl = hourly_data[sym].slice_through(D) if sym in hourly_data else None
                else:
                    h_sl = d_sl   # daily-only: trend AND entry both on daily
                rs = strategies.relative_strength(d_sl, spy_sl)
                # Selected strategy turns this point-in-time view into a signal.
                scores[sym] = strat.generate_signal(sym, d_sl, h_sl, spy_sl, rs,
                                                    earnings_soon=False)

            # Exits: price-based rule (mode-dependent), then time-exit, then
            # bearish signal. Decided at D's close; filled at the next open.
            for sym in list(positions.keys()):
                c = daily[sym].close_asof(D)
                if c is None:
                    continue
                pos = positions[sym]
                if exit_mode == "trail_only":
                    # ATR trail, NO take-profit; extended max-hold.
                    atr_v  = pos.get("atr") or (c * 0.01)   # fallback never reintroduces a TP
                    action = strategies.manage_position_atr(pos, c, atr_v, _ATR_TRAIL_MULT)
                    max_hold = _MAX_HOLD_TRAIL
                else:
                    action   = strategies.manage_position(pos, c)   # 4% TP + 2% trail + BE
                    max_hold = config.MAX_HOLD_DAYS
                if action is None:
                    hold = strategies.count_trading_days(date.fromisoformat(pos["entry_date"]), D)
                    if hold >= max_hold:
                        action = "TIME EXIT"
                if action is None and scores.get(sym, {}).get("signal") == -1:
                    action = "SELL"
                if action:
                    pending_exits.append((sym, action))

            # Entries: admitted by the strategy (default = BUY + regime-aware
            # score gate), best score first.
            buys = sorted(
                (scores[s] for s in tradables if strat.entry_admits(scores[s], regime)),
                key=lambda r: r["score"], reverse=True,
            )
            for rank, opp in enumerate(buys, 1):
                opp["_regime"] = regime
                opp["_rank"]   = rank   # 1 = strongest signal (for conviction sizing)
                # ATR at the DECISION bar (data up to D only) — no lookahead.
                opp["_atr"]    = strategies.atr(daily[opp["symbol"]].slice_through(D))
                pending_entries.append(opp)

        # Force-close anything still open at the final close, for clean accounting.
        final_date = calendar[last_idx]
        for sym in list(positions.keys()):
            px = daily[sym].close_asof(final_date)
            if px is not None:
                _close_position(sym, px, final_date, "END OF TEST")
        equity_curve[-1]["equity"] = round(balance, 2)

        # ---- SPY buy-and-hold benchmark over the SAME slice ----
        # Buy SPY with the full starting capital at the first bar's open, hold to
        # the end. Uses the SPY daily series already fetched for the regime
        # filter — no extra API calls. Marked to market on the same calendar so
        # the curve aligns with the strategy's day by day.
        spy_first_open = spy.open_on(calendar[0]) or spy.close_asof(calendar[0])
        bench_shares   = (capital / spy_first_open) if spy_first_open else 0.0
        benchmark_curve = [
            {"date": D.isoformat(),
             "equity": round(bench_shares * (spy.close_asof(D) or spy_first_open), 2)}
            for D in calendar
        ]

        result = _metrics(capital, balance, equity_curve, trades, mode, symbols,
                          start_date, end_date, hourly_data,
                          benchmark_curve=benchmark_curve, split=split,
                          split_date=split_dt.isoformat(),
                          sim_start=sim_start.isoformat(),
                          sim_end=sim_end.isoformat(),
                          strategy=strat.name, strategy_label=strat.label,
                          sizing=sizing, exit_mode=exit_mode,
                          max_positions=max_positions)
        _set(status="done", progress=1.0, result=result, finished_at=time.time(),
             message=f"Done [{strat.name} · {split.upper()}] — {len(trades)} trades, "
                     f"{result['total_return_pct']:+.1f}% return "
                     f"(SPY {result['benchmark']['return_pct']:+.1f}%)")

    except Exception as exc:  # noqa: BLE001
        traceback.print_exc()
        _set(status="error", error=f"{type(exc).__name__}: {exc}",
             finished_at=time.time())


# ── Metrics ───────────────────────────────────────────────────────────────

def _metrics(initial: float, final_balance: float, equity_curve: list[dict],
             trades: list[dict], mode: str, symbols: list[str],
             start_date: str, end_date: str, hourly_data: dict,
             benchmark_curve: list[dict] | None = None, split: str = "full",
             split_date: str = "", sim_start: str = "", sim_end: str = "",
             strategy: str = "macd_mtf", strategy_label: str = "",
             sizing: str = "flat", exit_mode: str = "fixed_tp",
             max_positions: int = 3) -> dict:
    final_equity = equity_curve[-1]["equity"] if equity_curve else final_balance
    total_return = (final_equity / initial - 1) * 100 if initial else 0.0

    wins   = [t for t in trades if t["pnl"] > 0]
    losses = [t for t in trades if t["pnl"] <= 0]
    gross_wins   = sum(t["pnl"] for t in wins)
    gross_losses = abs(sum(t["pnl"] for t in losses))
    total_pnl    = sum(t["pnl"] for t in trades)

    avg_win  = gross_wins / len(wins)     if wins   else 0.0
    avg_loss = gross_losses / len(losses) if losses else 0.0
    win_rate = len(wins) / len(trades) * 100 if trades else 0.0
    profit_factor = (gross_wins / gross_losses) if gross_losses > 0 else (
        float("inf") if gross_wins > 0 else 0.0)
    expectancy = total_pnl / len(trades) if trades else 0.0

    # Max drawdown + drawdown curve from the equity curve.
    peak = initial
    max_dd = 0.0
    dd_curve: list[dict] = []
    for pt in equity_curve:
        eq = pt["equity"]
        peak = max(peak, eq)
        dd = (eq / peak - 1) * 100 if peak else 0.0
        max_dd = min(max_dd, dd)
        dd_curve.append({"date": pt["date"], "drawdown": round(dd, 2)})

    # Annualised Sharpe from daily equity returns (risk-free = 0).
    sharpe = _annualised_sharpe(equity_curve)

    # SPY buy-and-hold benchmark over the same slice — the scoreboard.
    benchmark_curve = benchmark_curve or []
    bench_final  = benchmark_curve[-1]["equity"] if benchmark_curve else initial
    bench_return = (bench_final / initial - 1) * 100 if initial else 0.0
    bench_sharpe = _annualised_sharpe(benchmark_curve)
    alpha        = total_return - bench_return
    benchmark = {
        "name":         "SPY buy & hold",
        "final_equity": round(bench_final, 2),
        "return_pct":   round(bench_return, 2),
        "sharpe":       round(bench_sharpe, 2),
    }

    # Per-symbol breakdown.
    by_symbol: dict[str, dict] = {}
    for t in trades:
        b = by_symbol.setdefault(t["symbol"], {"symbol": t["symbol"], "trades": 0,
                                               "wins": 0, "pnl": 0.0})
        b["trades"] += 1
        b["wins"]   += 1 if t["pnl"] > 0 else 0
        b["pnl"]     = round(b["pnl"] + t["pnl"], 2)
    by_symbol_list = sorted(
        [{**b, "win_rate": round(b["wins"] / b["trades"] * 100, 1)}
         for b in by_symbol.values()],
        key=lambda x: x["pnl"], reverse=True,
    )

    best  = max(trades, key=lambda t: t["pnl"], default=None)
    worst = min(trades, key=lambda t: t["pnl"], default=None)

    mode_label = (
        "1H entry + 1D trend (multi-timeframe)" if mode == "hourly"
        else "DAILY-ONLY (trend AND entry on daily candles)"
    )

    # Prominent slice banner. Validation is the out-of-sample test you must not
    # have tuned on.
    split_titles = {
        "full":       "FULL RANGE",
        "train":      "TRAIN SET (in-sample — tune here)",
        "validation": "VALIDATION SET — data NOT used for tuning",
    }
    split_banner = (
        f"{split_titles.get(split, 'FULL RANGE')} — {sim_start} to {sim_end}"
    )

    sizing_label = (
        f"vol-scaled conviction (1% risk · {_ATR_STOP_MULT:g}×ATR stop · "
        f"rank 1.5/1.25/1.0× · ≤{int(strategies.MAX_POSITION_PCT*100)}% cap)"
        if sizing == "vol_conviction"
        else f"flat ({config.TRADE_SIZE_PCT*100:g}% of cash)"
    )
    exit_label = (
        f"trail-only ({_ATR_TRAIL_MULT:g}×ATR trail · no TP · max-hold {_MAX_HOLD_TRAIL}d)"
        if exit_mode == "trail_only"
        else f"fixed-TP ({config.TAKE_PROFIT_PCT*100:g}% TP · {config.STOP_LOSS_PCT*100:g}% trail · "
             f"BE · max-hold {config.MAX_HOLD_DAYS}d)"
    )

    return {
        "label":            "BACKTEST — Simulated, not live trades",
        "strategy":         strategy,
        "strategy_label":   strategy_label,
        "sizing":           sizing,
        "sizing_label":     sizing_label,
        "exit_mode":        exit_mode,
        "exit_label":       exit_label,
        "max_positions":    max_positions,
        "mode":             mode,
        "mode_label":       mode_label,
        "split":            split,
        "split_date":       split_date,
        "sim_start":        sim_start,
        "sim_end":          sim_end,
        "split_banner":     split_banner,
        "benchmark":        benchmark,
        "alpha":            round(alpha, 2),
        "beat_benchmark":   total_return > bench_return,
        "beat_sharpe":      sharpe > bench_sharpe,
        "benchmark_curve":  benchmark_curve,
        "start_date":       start_date,
        "end_date":         end_date,
        "symbols":          symbols,
        "initial_balance":  round(initial, 2),
        "final_equity":     round(final_equity, 2),
        "total_return_pct": round(total_return, 2),
        "total_trades":     len(trades),
        "win_rate":         round(win_rate, 1),
        "profit_factor":    (round(profit_factor, 2) if profit_factor != float("inf") else None),
        "avg_win":          round(avg_win, 2),
        "avg_loss":         round(avg_loss, 2),
        "expectancy":       round(expectancy, 2),
        "gross_wins":       round(gross_wins, 2),
        "gross_losses":     round(gross_losses, 2),
        "max_drawdown_pct": round(max_dd, 2),
        "sharpe":           round(sharpe, 2),
        "by_symbol":        by_symbol_list,
        "best_trade":       best,
        "worst_trade":      worst,
        "equity_curve":     equity_curve,
        "drawdown_curve":   dd_curve,
        "trades":           trades,
        "slippage_pct":     SLIPPAGE * 100,
    }
