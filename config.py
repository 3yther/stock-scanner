import os
from dotenv import load_dotenv

load_dotenv()

EMAIL_ADDRESS  = os.getenv("EMAIL_ADDRESS", "")
EMAIL_PASSWORD = os.getenv("EMAIL_PASSWORD", "")

# ── Universe: 60 symbols (top S&P 500 + SPCX) ─────────────────────────────
SYMBOLS = [
    # Original 21
    "AAPL", "MSFT", "GOOGL", "AMZN", "NVDA", "META", "TSLA",
    "BRK-B", "JPM", "JNJ", "V", "PG", "UNH", "HD", "MA",
    "BAC", "XOM", "PFE", "ABBV", "COST", "SPCX",
    # Next 39 most liquid S&P 500
    "LLY", "AVGO", "WMT", "ORCL", "CRM", "CVX", "MRK", "AMD", "ADBE", "NFLX",
    "PEP", "KO", "TMO", "CSCO", "ACN", "MCD", "ABT", "LIN", "INTC", "DIS",
    "WFC", "VZ", "CAT", "INTU", "IBM", "QCOM", "GE", "AMGN", "NOW", "ISRG",
    "SPGI", "UBER", "T", "NEE", "RTX", "BKNG", "PM", "GS", "HON",
]

# ── Sector mapping ─────────────────────────────────────────────────────────
SECTOR_MAP: dict[str, str] = {
    # Technology
    "AAPL": "Technology", "MSFT": "Technology", "NVDA": "Technology",
    "META": "Technology", "AVGO": "Technology", "ORCL": "Technology",
    "CRM":  "Technology", "AMD":  "Technology", "ADBE": "Technology",
    "CSCO": "Technology", "ACN":  "Technology", "INTU": "Technology",
    "IBM":  "Technology", "QCOM": "Technology", "NOW":  "Technology",
    "INTC": "Technology",
    # Consumer Discretionary
    "AMZN": "Disc.",       "TSLA": "Disc.",       "HD":   "Disc.",
    "MCD":  "Disc.",       "BKNG": "Disc.",        "UBER": "Disc.",
    "NFLX": "Disc.",
    # Communication Services
    "GOOGL": "Comm.",  "VZ": "Comm.",  "DIS": "Comm.",  "T": "Comm.",
    # Financials
    "BRK-B": "Financials", "JPM": "Financials", "V":  "Financials",
    "MA":    "Financials", "BAC": "Financials", "WFC": "Financials",
    "GS":    "Financials", "SPGI": "Financials",
    # Health Care
    "JNJ": "Health",  "UNH":  "Health",  "PFE":  "Health",
    "ABBV": "Health", "LLY":  "Health",  "MRK":  "Health",
    "TMO": "Health",  "ABT":  "Health",  "AMGN": "Health",
    "ISRG": "Health",
    # Consumer Staples
    "PG":   "Staples", "COST": "Staples", "PEP": "Staples",
    "KO":   "Staples", "PM":   "Staples", "WMT": "Staples",
    # Industrials
    "CAT": "Industrials", "GE":  "Industrials",
    "RTX": "Industrials", "HON": "Industrials",
    # Energy
    "XOM": "Energy", "CVX": "Energy",
    # Materials
    "LIN": "Materials",
    # Utilities
    "NEE": "Utilities",
    # ETF
    "SPCX": "ETF",
}

MAX_POSITIONS       = 3       # max simultaneous open positions
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

SCAN_INTERVAL       = 300     # scanner runs every 5 minutes
TRADE_LOOP_INTERVAL = 60      # trader checks positions every 60 seconds

DASHBOARD_PORT      = 5002
DB_PATH             = "trades.db"
