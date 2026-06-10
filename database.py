"""
Database layer — PostgreSQL when DATABASE_URL is set (Railway), SQLite locally.
"""

import os
import sqlite3
from contextlib import contextmanager
from datetime import datetime
from typing import Any

import config

_DB_URL = os.getenv("DATABASE_URL", "")
if _DB_URL.startswith("postgres://"):
    _DB_URL = _DB_URL.replace("postgres://", "postgresql://", 1)

USE_PG = bool(_DB_URL)

if USE_PG:
    import psycopg2
    import psycopg2.extras
    import psycopg2.pool
    _pool = psycopg2.pool.ThreadedConnectionPool(1, 10, _DB_URL)


@contextmanager
def _conn():
    if USE_PG:
        conn = _pool.getconn()
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            _pool.putconn(conn)
    else:
        conn = sqlite3.connect(config.DB_PATH, check_same_thread=False)
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()


def init_db():
    with _conn() as conn:
        if USE_PG:
            cur = conn.cursor()
            cur.execute("""
                CREATE TABLE IF NOT EXISTS trades (
                    id        SERIAL PRIMARY KEY,
                    timestamp TEXT             NOT NULL,
                    symbol    TEXT             NOT NULL,
                    action    TEXT             NOT NULL,
                    price     DOUBLE PRECISION NOT NULL,
                    shares    DOUBLE PRECISION NOT NULL,
                    pnl       DOUBLE PRECISION NOT NULL DEFAULT 0.0,
                    balance   DOUBLE PRECISION NOT NULL
                )
            """)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS bot_state (
                    key   TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                )
            """)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS equity_snapshots (
                    date             TEXT PRIMARY KEY,
                    equity           DOUBLE PRECISION NOT NULL,
                    balance          DOUBLE PRECISION NOT NULL,
                    positions_value  DOUBLE PRECISION NOT NULL DEFAULT 0.0
                )
            """)
            cur.close()
        else:
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS trades (
                    id        INTEGER PRIMARY KEY AUTOINCREMENT,
                    timestamp TEXT    NOT NULL,
                    symbol    TEXT    NOT NULL,
                    action    TEXT    NOT NULL,
                    price     REAL    NOT NULL,
                    shares    REAL    NOT NULL,
                    pnl       REAL    NOT NULL DEFAULT 0.0,
                    balance   REAL    NOT NULL
                );
                CREATE TABLE IF NOT EXISTS bot_state (
                    key   TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS equity_snapshots (
                    date             TEXT PRIMARY KEY,
                    equity           REAL NOT NULL,
                    balance          REAL NOT NULL,
                    positions_value  REAL NOT NULL DEFAULT 0.0
                );
            """)


init_db()


# ── CRUD ──────────────────────────────────────────────────────────────────

def log_trade(trade: dict[str, Any]):
    with _conn() as conn:
        if USE_PG:
            cur = conn.cursor()
            cur.execute(
                """INSERT INTO trades (timestamp, symbol, action, price, shares, pnl, balance)
                   VALUES (%s, %s, %s, %s, %s, %s, %s)""",
                (trade["timestamp"], trade["symbol"], trade["action"],
                 trade["price"], trade["shares"], trade["pnl"], trade["balance"]),
            )
            cur.close()
        else:
            conn.execute(
                """INSERT INTO trades (timestamp, symbol, action, price, shares, pnl, balance)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (trade["timestamp"], trade["symbol"], trade["action"],
                 trade["price"], trade["shares"], trade["pnl"], trade["balance"]),
            )


def get_recent_trades(limit: int = 100) -> list[dict]:
    with _conn() as conn:
        if USE_PG:
            cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
            cur.execute(
                "SELECT * FROM trades ORDER BY timestamp DESC LIMIT %s", (limit,)
            )
            rows = [dict(r) for r in cur.fetchall()]
            cur.close()
            return rows
        else:
            conn.row_factory = sqlite3.Row
            return [dict(r) for r in conn.execute(
                "SELECT * FROM trades ORDER BY timestamp DESC LIMIT ?", (limit,)
            ).fetchall()]


def get_all_trades(limit: int = 2000) -> list[dict]:
    """For stats page — larger limit, chronological order."""
    with _conn() as conn:
        if USE_PG:
            cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
            cur.execute(
                "SELECT * FROM trades ORDER BY timestamp ASC LIMIT %s", (limit,)
            )
            rows = [dict(r) for r in cur.fetchall()]
            cur.close()
            return rows
        else:
            conn.row_factory = sqlite3.Row
            return [dict(r) for r in conn.execute(
                "SELECT * FROM trades ORDER BY timestamp ASC LIMIT ?", (limit,)
            ).fetchall()]


def get_kill_switch() -> bool:
    with _conn() as conn:
        if USE_PG:
            cur = conn.cursor()
            cur.execute("SELECT value FROM bot_state WHERE key = 'kill_switch'")
            row = cur.fetchone()
            cur.close()
        else:
            row = conn.execute(
                "SELECT value FROM bot_state WHERE key = 'kill_switch'"
            ).fetchone()
    return row is not None and row[0] == "1"


def set_kill_switch(active: bool):
    val = "1" if active else "0"
    with _conn() as conn:
        if USE_PG:
            cur = conn.cursor()
            cur.execute(
                """INSERT INTO bot_state (key, value) VALUES ('kill_switch', %s)
                   ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value""",
                (val,),
            )
            cur.close()
        else:
            conn.execute(
                "INSERT OR REPLACE INTO bot_state (key, value) VALUES ('kill_switch', ?)",
                (val,),
            )


# ── Equity snapshots ──────────────────────────────────────────────────────

def save_equity_snapshot(equity: float, balance: float, pos_value: float):
    day = datetime.utcnow().date().isoformat()
    with _conn() as conn:
        if USE_PG:
            cur = conn.cursor()
            cur.execute(
                """INSERT INTO equity_snapshots (date, equity, balance, positions_value)
                   VALUES (%s, %s, %s, %s)
                   ON CONFLICT (date) DO UPDATE
                   SET equity = EXCLUDED.equity,
                       balance = EXCLUDED.balance,
                       positions_value = EXCLUDED.positions_value""",
                (day, equity, balance, pos_value),
            )
            cur.close()
        else:
            conn.execute(
                """INSERT OR REPLACE INTO equity_snapshots
                   (date, equity, balance, positions_value) VALUES (?, ?, ?, ?)""",
                (day, equity, balance, pos_value),
            )


def get_equity_snapshots() -> list[dict]:
    with _conn() as conn:
        if USE_PG:
            cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
            cur.execute("SELECT * FROM equity_snapshots ORDER BY date")
            rows = [dict(r) for r in cur.fetchall()]
            cur.close()
            return rows
        else:
            conn.row_factory = sqlite3.Row
            return [dict(r) for r in conn.execute(
                "SELECT * FROM equity_snapshots ORDER BY date"
            ).fetchall()]
