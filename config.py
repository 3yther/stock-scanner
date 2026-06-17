import os
from dotenv import load_dotenv

load_dotenv()

EMAIL_ADDRESS  = os.getenv("EMAIL_ADDRESS", "")
EMAIL_PASSWORD = os.getenv("EMAIL_PASSWORD", "")

# ── Universe: 24 symbols ───────────────────────────────────────────────────
SYMBOLS = [
    "AAPL", "MSFT", "GOOGL", "AMZN", "NVDA", "META", "TSLA",
    "JPM",  "JNJ",  "V",     "PG",   "UNH",  "HD",   "MA",
    "BAC",  "XOM",  "PFE",   "ABBV", "COST", "BRK-B",
    "SPCX", "SPY",  "QQQ",   "AMD",
]

# ── Sector mapping ─────────────────────────────────────────────────────────
SECTOR_MAP: dict[str, str] = {
    # Technology
    "AAPL": "Technology", "MSFT": "Technology", "NVDA": "Technology",
    "META": "Technology", "AMD":  "Technology",
    # Communication Services
    "GOOGL": "Comm.",
    # Consumer Discretionary
    "AMZN": "Disc.", "TSLA": "Disc.", "HD": "Disc.",
    # Financials
    "JPM":   "Financials", "V":    "Financials", "MA":   "Financials",
    "BAC":   "Financials", "BRK-B":"Financials",
    # Health Care
    "JNJ":  "Health", "UNH":  "Health", "PFE":  "Health", "ABBV": "Health",
    # Consumer Staples
    "PG": "Staples", "COST": "Staples",
    # Energy
    "XOM": "Energy",
    # ETF
    "SPCX": "ETF", "SPY": "ETF", "QQQ": "ETF",
}

MAX_POSITIONS       = 3       # default cap (backtest default; live uses LIVE_MAX_POSITIONS)
TRADE_SIZE_PCT      = 0.05    # 5% of balance per position (bull regime)
STOP_LOSS_PCT       = 0.02    # trailing stop 2% below high watermark
TAKE_PROFIT_PCT     = 0.04    # 4% above entry
MAX_DAILY_LOSS      = 0.05    # halt if daily P&L < -5% of initial balance

# Smarter exits
BREAKEVEN_TRIGGER   = 0.02    # move SL to entry once position is up 2%
MAX_HOLD_DAYS       = 5       # force-close after 5 trading days

# Volume & score gates
MIN_VOL_RATIO       = 1.2     # minimum vol ratio for a BUY entry
MIN_SCORE_BULL      = 55.0    # min score to enter in BULLISH / NEUTRAL regime
MIN_SCORE_BEAR      = 80.0    # min score to enter in BEARISH regime
BEAR_POSITION_SCALE = 0.5     # halve position size in bear regime

INITIAL_BALANCE     = 1000.0

# ── Live bot strategy config ("T4") ─────────────────────────────────────────
# Validated on out-of-sample (validation) data: +28.36% vs SPY +17.36%,
# Sharpe 2.28 vs 1.82. Strong in trends, weaker in sideways markets.
# The sizing/exit numeric params live in strategies.py (shared with the
# backtester so live behaviour matches what was validated).
LIVE_SIZING_MODE    = "vol_conviction"   # "flat" | "vol_conviction"
LIVE_EXIT_MODE      = "trail_only"       # "fixed_tp" | "trail_only"
LIVE_MAX_POSITIONS  = 5                  # max simultaneous open positions (live bot)

# ── Crypto Bot regime filter ─────────────────────────────────────────────────
CRYPTO_REGIME_FILTER   = True    # regime-aware TJR sizing (default ON)
CRYPTO_REGIME_INTERVAL = "1h"    # timeframe the regime is computed on
CRYPTO_REGIME_REFRESH  = 600     # seconds between regime re-checks (1h regime is slow)

# ── SMT divergence (BTC vs ETH) — toggleable confluence sizing ───────────────
# Additive sizing only — SMT never gates a buy, it scales it. When an FVG/MSS
# buy agrees with the SMT bias it's boosted; when it disagrees it's reduced.
SMT_ENABLED         = True   # compute SMT + apply confluence sizing
SMT_CONFLUENCE_MULT = 1.3    # × when SMT agrees with the buy direction
SMT_DISAGREE_MULT   = 0.7    # × when SMT disagrees (reduce, don't block)
SMT_SWING_BARS      = 1      # swing-pivot bars (reuse the MSS swing definition)

# Scan cadence depends on market state (chosen per-scan in scanner.run_loop).
SCAN_INTERVAL_OPEN   = 300    # market OPEN   → scan every 5 minutes
SCAN_INTERVAL_CLOSED = 1800   # market CLOSED → scan every 30 minutes
SCAN_INTERVAL        = SCAN_INTERVAL_CLOSED   # fallback / legacy default
TRADE_LOOP_INTERVAL  = 60     # trader checks positions every 60 seconds

DASHBOARD_PORT      = 5002
DB_PATH             = "trades.db"
