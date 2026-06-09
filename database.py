"""
Database layer — PostgreSQL when DATABASE_URL is set (Railway), SQLite locally.

PostgreSQL connection is managed through a ThreadedConnectionPool so the
single shared pool handles Flask + scanner + trader thread concurrency safely.

SQLite is kept as a zero-config fallback for local development.
"""

import os
import sqlite3
from contextlib import contextmanager
from typing import Any

import config

# ─── Backend detection ─────────────────────────────────────────────────────

_DB_URL = os.getenv("DATABASE_URL", "")
# Railway Postgres gives "postgres://" but psycopg2 requires "postgresql://"
if _DB_URL.startswith("postgres://"):
    _DB_URL = _DB_URL.replace("postgres://", "postgresql://", 1)

USE_PG = bool(_DB_URL)

if USE_PG:
    import psycopg2
    import psycopg2.extras
    import psycopg2.pool
    _pool = psycopg2.pool.ThreadedConnectionPool(1, 10, _DB_URL)


# ─── Connection context manager ────────────────────────────────────────────

@contextmanager
def _conn():
    """Yield a connection, commit on success, rollback on error."""
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


# ─── Schema ────────────────────────────────────────────────────────────────

def init_db():
    """Create tables if they don't exist. Safe to call multiple times."""
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
            """)


init_db()


# ─── CRUD ──────────────────────────────────────────────────────────────────

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
