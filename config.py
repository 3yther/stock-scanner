import os
from dotenv import load_dotenv

load_dotenv()

EMAIL_ADDRESS  = os.getenv("EMAIL_ADDRESS", "")
EMAIL_PASSWORD = os.getenv("EMAIL_PASSWORD", "")

# Universe: top-20 S&P 500 + SPCX (data from 2025-06-12)
SYMBOLS = [
    "AAPL", "MSFT", "GOOGL", "AMZN", "NVDA", "META", "TSLA",
    "BRK-B", "JPM", "JNJ", "V", "PG", "UNH", "HD", "MA",
    "BAC", "XOM", "PFE", "ABBV", "COST",
    "SPCX",
]

MAX_POSITIONS      = 3       # max simultaneous open positions
TRADE_SIZE_PCT     = 0.05    # 5% of balance per position
STOP_LOSS_PCT      = 0.02    # trailing stop 2% below high watermark
TAKE_PROFIT_PCT    = 0.04    # 4% above entry
MAX_DAILY_LOSS     = 0.05    # halt if daily P&L < -5% of initial balance

INITIAL_BALANCE    = 10000.0

SCAN_INTERVAL      = 300     # scanner runs every 5 minutes
TRADE_LOOP_INTERVAL = 60     # trader checks positions every 60 seconds

DASHBOARD_PORT     = 5002
DB_PATH            = "trades.db"
