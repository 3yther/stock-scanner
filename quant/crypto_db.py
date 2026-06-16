"""
crypto_db.py — TJR / Crypto Bot tables in the SAME database as the stock bot,
but a completely SEPARATE set of tables. This module NEVER reads or writes the
stock `trades` table — the crypto paper ledger lives only in `tjr_buys`.

Reuses the stock app's connection layer (database._conn / USE_PG) so it works on
PostgreSQL (Railway) and SQLite (local) identically.

Tables:
  tjr_asia_range : id, date, symbol, asia_high, asia_low, created_at   (unique date+symbol)
  tjr_sweeps     : id, timestamp, symbol, direction(high/low), sweep_price, asia_level, created_at
  tjr_mss        : id, timestamp, symbol, direction(bull/bear), price, swing_level, created_at
  tjr_fvg        : id, timestamp, symbol, direction(bull/bear), top_price, bottom_price, filled, filled_at, created_at
  tjr_buys       : id, timestamp, symbol, type, usd_amount, price, multiplier, created_at
"""

import sqlite3
from datetime import datetime, timezone

import database as _db   # shared connection layer (stock + crypto live in one DB)

USE_PG = _db.USE_PG


# ── Low-level helpers (translate ?-placeholders for PG) ───────────────────

def _ph(sql: str) -> str:
    return sql.replace("?", "%s") if USE_PG else sql


def _insert(sql: str, params=()):
    """INSERT and return the new row id."""
    with _db._conn() as conn:
        if USE_PG:
            cur = conn.cursor()
            cur.execute(_ph(sql) + " RETURNING id", params)
            rid = cur.fetchone()[0]
            cur.close()
            return rid
        cur = conn.execute(sql, params)
        return cur.lastrowid


def _exec(sql: str, params=()):
    with _db._conn() as conn:
        if USE_PG:
            cur = conn.cursor()
            cur.execute(_ph(sql), params)
            cur.close()
        else:
            conn.execute(sql, params)


def _query(sql: str, params=()) -> list[dict]:
    with _db._conn() as conn:
        if USE_PG:
            cur = conn.cursor(cursor_factory=_db.psycopg2.extras.RealDictCursor)
            cur.execute(_ph(sql), params)
            rows = [dict(r) for r in cur.fetchall()]
            cur.close()
            return rows
        conn.row_factory = sqlite3.Row
        return [dict(r) for r in conn.execute(sql, params).fetchall()]


# ── Schema ─────────────────────────────────────────────────────────────────

_DDL_PG = [
    """CREATE TABLE IF NOT EXISTS tjr_asia_range (
         id SERIAL PRIMARY KEY, date TEXT NOT NULL, symbol TEXT NOT NULL,
         asia_high DOUBLE PRECISION NOT NULL, asia_low DOUBLE PRECISION NOT NULL,
         created_at TIMESTAMP DEFAULT now(), UNIQUE(date, symbol))""",
    """CREATE TABLE IF NOT EXISTS tjr_sweeps (
         id SERIAL PRIMARY KEY, timestamp TEXT NOT NULL, symbol TEXT NOT NULL,
         direction TEXT NOT NULL, sweep_price DOUBLE PRECISION NOT NULL,
         asia_level DOUBLE PRECISION NOT NULL, created_at TIMESTAMP DEFAULT now())""",
    """CREATE TABLE IF NOT EXISTS tjr_mss (
         id SERIAL PRIMARY KEY, timestamp TEXT NOT NULL, symbol TEXT NOT NULL,
         direction TEXT NOT NULL, price DOUBLE PRECISION NOT NULL,
         swing_level DOUBLE PRECISION NOT NULL, created_at TIMESTAMP DEFAULT now())""",
    """CREATE TABLE IF NOT EXISTS tjr_fvg (
         id SERIAL PRIMARY KEY, timestamp TEXT NOT NULL, symbol TEXT NOT NULL,
         direction TEXT NOT NULL, top_price DOUBLE PRECISION NOT NULL,
         bottom_price DOUBLE PRECISION NOT NULL, filled BOOLEAN NOT NULL DEFAULT FALSE,
         filled_at TEXT, created_at TIMESTAMP DEFAULT now())""",
    """CREATE TABLE IF NOT EXISTS tjr_buys (
         id SERIAL PRIMARY KEY, timestamp TEXT NOT NULL, symbol TEXT NOT NULL,
         type TEXT NOT NULL, usd_amount DOUBLE PRECISION NOT NULL,
         price DOUBLE PRECISION NOT NULL, multiplier DOUBLE PRECISION NOT NULL DEFAULT 1.0,
         regime TEXT, created_at TIMESTAMP DEFAULT now())""",
    """CREATE TABLE IF NOT EXISTS tjr_settings (key TEXT PRIMARY KEY, value TEXT NOT NULL)""",
]

_DDL_SQLITE = [
    """CREATE TABLE IF NOT EXISTS tjr_asia_range (
         id INTEGER PRIMARY KEY AUTOINCREMENT, date TEXT NOT NULL, symbol TEXT NOT NULL,
         asia_high REAL NOT NULL, asia_low REAL NOT NULL,
         created_at TEXT DEFAULT (datetime('now')), UNIQUE(date, symbol))""",
    """CREATE TABLE IF NOT EXISTS tjr_sweeps (
         id INTEGER PRIMARY KEY AUTOINCREMENT, timestamp TEXT NOT NULL, symbol TEXT NOT NULL,
         direction TEXT NOT NULL, sweep_price REAL NOT NULL, asia_level REAL NOT NULL,
         created_at TEXT DEFAULT (datetime('now')))""",
    """CREATE TABLE IF NOT EXISTS tjr_mss (
         id INTEGER PRIMARY KEY AUTOINCREMENT, timestamp TEXT NOT NULL, symbol TEXT NOT NULL,
         direction TEXT NOT NULL, price REAL NOT NULL, swing_level REAL NOT NULL,
         created_at TEXT DEFAULT (datetime('now')))""",
    """CREATE TABLE IF NOT EXISTS tjr_fvg (
         id INTEGER PRIMARY KEY AUTOINCREMENT, timestamp TEXT NOT NULL, symbol TEXT NOT NULL,
         direction TEXT NOT NULL, top_price REAL NOT NULL, bottom_price REAL NOT NULL,
         filled INTEGER NOT NULL DEFAULT 0, filled_at TEXT,
         created_at TEXT DEFAULT (datetime('now')))""",
    """CREATE TABLE IF NOT EXISTS tjr_buys (
         id INTEGER PRIMARY KEY AUTOINCREMENT, timestamp TEXT NOT NULL, symbol TEXT NOT NULL,
         type TEXT NOT NULL, usd_amount REAL NOT NULL, price REAL NOT NULL,
         multiplier REAL NOT NULL DEFAULT 1.0, regime TEXT, created_at TEXT DEFAULT (datetime('now')))""",
    """CREATE TABLE IF NOT EXISTS tjr_settings (key TEXT PRIMARY KEY, value TEXT NOT NULL)""",
]


def init_tjr_db():
    """Create the TJR tables if they don't exist. Safe to call repeatedly."""
    with _db._conn() as conn:
        if USE_PG:
            cur = conn.cursor()
            for ddl in _DDL_PG:
                cur.execute(ddl)
            cur.execute("ALTER TABLE tjr_buys ADD COLUMN IF NOT EXISTS regime TEXT")  # migrate existing
            cur.close()
        else:
            for ddl in _DDL_SQLITE:
                conn.execute(ddl)
            cols = [r[1] for r in conn.execute("PRAGMA table_info(tjr_buys)")]
            if "regime" not in cols:                                                  # migrate existing
                conn.execute("ALTER TABLE tjr_buys ADD COLUMN regime TEXT")
    print(f"[TJR DB] tables ready on {_db.db_location()}", flush=True)


# ── Asia range ─────────────────────────────────────────────────────────────

def save_asia_range(symbol: str, date: str, asia_high: float, asia_low: float):
    if USE_PG:
        _exec("""INSERT INTO tjr_asia_range (date, symbol, asia_high, asia_low)
                 VALUES (?, ?, ?, ?)
                 ON CONFLICT (date, symbol) DO UPDATE
                 SET asia_high = EXCLUDED.asia_high, asia_low = EXCLUDED.asia_low""",
              (date, symbol, asia_high, asia_low))
    else:
        _exec("""INSERT INTO tjr_asia_range (date, symbol, asia_high, asia_low)
                 VALUES (?, ?, ?, ?)
                 ON CONFLICT (date, symbol) DO UPDATE
                 SET asia_high = excluded.asia_high, asia_low = excluded.asia_low""",
              (date, symbol, asia_high, asia_low))


def get_asia_range(symbol: str, date: str) -> dict | None:
    rows = _query("SELECT * FROM tjr_asia_range WHERE symbol = ? AND date = ?",
                  (symbol, date))
    return rows[0] if rows else None


# ── Sweeps / MSS / FVG / buys ─────────────────────────────────────────────

def log_sweep(symbol, timestamp, direction, sweep_price, asia_level):
    return _insert("""INSERT INTO tjr_sweeps (timestamp, symbol, direction, sweep_price, asia_level)
                      VALUES (?, ?, ?, ?, ?)""",
                   (timestamp, symbol, direction, sweep_price, asia_level))


def log_mss(symbol, timestamp, direction, price, swing_level):
    return _insert("""INSERT INTO tjr_mss (timestamp, symbol, direction, price, swing_level)
                      VALUES (?, ?, ?, ?, ?)""",
                   (timestamp, symbol, direction, price, swing_level))


def get_latest_mss(symbol: str) -> dict | None:
    rows = _query("SELECT * FROM tjr_mss WHERE symbol = ? ORDER BY id DESC LIMIT 1", (symbol,))
    return rows[0] if rows else None


def log_fvg(symbol, timestamp, direction, top_price, bottom_price):
    return _insert("""INSERT INTO tjr_fvg (timestamp, symbol, direction, top_price, bottom_price, filled)
                      VALUES (?, ?, ?, ?, ?, ?)""",
                   (timestamp, symbol, direction, top_price, bottom_price,
                    False if USE_PG else 0))


def mark_fvg_filled(fvg_id: int, filled_at: str):
    _exec("UPDATE tjr_fvg SET filled = ?, filled_at = ? WHERE id = ?",
          ((True if USE_PG else 1), filled_at, fvg_id))


def get_active_fvgs(symbol: str, limit: int = 20) -> list[dict]:
    cond = "filled = FALSE" if USE_PG else "filled = 0"
    return _query(f"SELECT * FROM tjr_fvg WHERE symbol = ? AND {cond} "
                  f"ORDER BY id DESC LIMIT ?", (symbol, limit))


def get_recent_fvgs(symbol: str, limit: int = 40) -> list[dict]:
    """Recent FVGs (active AND filled) for chart overlays."""
    return _query("SELECT * FROM tjr_fvg WHERE symbol = ? ORDER BY id DESC LIMIT ?",
                  (symbol, limit))


def log_buy(symbol, timestamp, type_, usd_amount, price, multiplier=1.0, regime=None):
    return _insert("""INSERT INTO tjr_buys (timestamp, symbol, type, usd_amount, price, multiplier, regime)
                      VALUES (?, ?, ?, ?, ?, ?, ?)""",
                   (timestamp, symbol, type_, usd_amount, price, multiplier, regime))


def get_recent_buys(limit: int = 50) -> list[dict]:
    return _query("SELECT * FROM tjr_buys ORDER BY id DESC LIMIT ?", (limit,))


def get_setting(key: str, default=None):
    rows = _query("SELECT value FROM tjr_settings WHERE key = ?", (key,))
    return rows[0]["value"] if rows else default


def set_setting(key: str, value: str):
    if USE_PG:
        _exec("""INSERT INTO tjr_settings (key, value) VALUES (?, ?)
                 ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value""", (key, value))
    else:
        _exec("INSERT OR REPLACE INTO tjr_settings (key, value) VALUES (?, ?)", (key, value))


def get_sweeps_on(symbol: str, date: str) -> list[dict]:
    return _query("SELECT * FROM tjr_sweeps WHERE symbol = ? AND timestamp LIKE ? ORDER BY id",
                  (symbol, date + "%"))


def get_mss_on(symbol: str, date: str) -> list[dict]:
    return _query("SELECT * FROM tjr_mss WHERE symbol = ? AND timestamp LIKE ? ORDER BY id",
                  (symbol, date + "%"))


def fvg_exists(symbol: str, timestamp: str) -> bool:
    return bool(_query("SELECT id FROM tjr_fvg WHERE symbol = ? AND timestamp = ? LIMIT 1",
                       (symbol, timestamp)))


def get_recent_sweeps(limit: int = 50) -> list[dict]:
    return _query("SELECT * FROM tjr_sweeps ORDER BY id DESC LIMIT ?", (limit,))


def get_recent_mss(limit: int = 50) -> list[dict]:
    return _query("SELECT * FROM tjr_mss ORDER BY id DESC LIMIT ?", (limit,))


def get_recent_filled_fvgs(limit: int = 50) -> list[dict]:
    cond = "filled = TRUE" if USE_PG else "filled = 1"
    return _query(f"SELECT * FROM tjr_fvg WHERE {cond} ORDER BY id DESC LIMIT ?", (limit,))


def get_buys_since(date_prefix: str, symbol: str | None = None) -> list[dict]:
    """Buys whose timestamp starts with date_prefix (YYYY-MM-DD)."""
    if symbol:
        return _query("SELECT * FROM tjr_buys WHERE symbol = ? AND timestamp LIKE ? ORDER BY id DESC",
                      (symbol, date_prefix + "%"))
    return _query("SELECT * FROM tjr_buys WHERE timestamp LIKE ? ORDER BY id DESC",
                  (date_prefix + "%",))


# Create tables on import, like database.py does.
init_tjr_db()
