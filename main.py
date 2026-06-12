"""
Stock Scanner & Paper Trading Bot
----------------------------------
Data source : Polygon.io REST API (POLYGON_API_KEY env var)
Universe    : 20 S&P 500 + SPCX (21 symbols)
Strategy    : Multi-timeframe MACD (1D trend + 1H entry)
Dashboard   : http://localhost:5002
"""

import signal
import sys
import threading
import time

import config
import database as db
import dashboard
from paper_trader import StockPaperTrader
from scanner import Scanner


def main():
    # Ensure stdout is unbuffered so every print appears in Railway logs immediately
    sys.stdout.reconfigure(line_buffering=True)

    print("=" * 62, flush=True)
    print("  Stock Scanner  —  Paper Trading Bot", flush=True)
    print(f"  Universe    : {len(config.SYMBOLS)} symbols")
    print(f"  Strategy    : Multi-Timeframe MACD (1D trend + 1H entry)")
    print(f"  Balance     : ${config.INITIAL_BALANCE:,.2f}  |  Max positions: {config.MAX_POSITIONS}")
    print(f"  Data source : Polygon.io (key={'SET' if __import__('os').getenv('POLYGON_API_KEY') else 'NOT SET'})")
    print("=" * 62)
    print(f"\n  Symbols: {', '.join(config.SYMBOLS)}\n")

    # 1. Database
    print("[1/4] Initialising database…")
    db.init_db()
    db_type = "PostgreSQL" if db.USE_PG else f"SQLite ({config.DB_PATH})"
    print(f"  DB type  : {db_type}", flush=True)
    try:
        n = db.count_trades()
        print(f"  Trades in DB: {n}", flush=True)
    except Exception as exc:
        print(f"  WARNING — could not count trades: {exc}", flush=True)

    # 2. Trader + Scanner
    print("[2/4] Creating trader and scanner…")
    trader  = StockPaperTrader()
    scanner = Scanner()

    # 3. Dashboard
    print(f"[3/4] Starting dashboard at http://localhost:{config.DASHBOARD_PORT} …")
    dashboard.configure(trader, scanner)
    dashboard.start_server(config.DASHBOARD_PORT)
    print(f"  Dashboard: http://localhost:{config.DASHBOARD_PORT}")

    # 4. Background threads
    print("[4/4] Launching scanner and trader threads…\n")

    scanner_thread = threading.Thread(
        target=scanner.run_loop,
        args=(trader,),
        daemon=True,
        name="scanner",
    )
    trader_thread = threading.Thread(
        target=trader.run,
        daemon=True,
        name="trader",
    )

    scanner_thread.start()
    print(f"  scanner thread started — alive={scanner_thread.is_alive()} id={scanner_thread.ident}", flush=True)

    # Give scanner a moment to populate results before trader starts acting
    time.sleep(2)
    trader_thread.start()
    print(f"  trader  thread started — alive={trader_thread.is_alive()}  id={trader_thread.ident}", flush=True)

    def _shutdown(sig, frame):
        print("\n\n  Shutting down…")
        trader.stop()
        sys.exit(0)

    signal.signal(signal.SIGINT,  _shutdown)
    signal.signal(signal.SIGTERM, _shutdown)

    print("  Press Ctrl-C to stop.\n")
    while True:
        time.sleep(30)
        st = trader.status()
        held = ", ".join(p["symbol"] for p in st["positions"]) or "none"
        print(
            f"  [{time.strftime('%H:%M:%S')}]"
            f"  equity=${st['equity']:,.2f}"
            f"  P&L={'+' if st['total_pnl'] >= 0 else ''}{st['total_pnl_pct']:.2f}%"
            f"  pos=[{held}]"
            f"  daily={'+' if st['daily_pnl'] >= 0 else ''}{st['daily_pnl']:,.2f}"
        )


if __name__ == "__main__":
    main()
