import sqlite3
from datetime import datetime
from typing import Any
import config


def _conn():
    return sqlite3.connect(config.DB_PATH, check_same_thread=False)


def init_db():
    with _conn() as con:
        con.executescript("""
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


def log_trade(trade: dict[str, Any]):
    with _conn() as con:
        con.execute(
            "INSERT INTO trades (timestamp, symbol, action, price, shares, pnl, balance)"
            " VALUES (?, ?, ?, ?, ?, ?, ?)",
            (trade["timestamp"], trade["symbol"], trade["action"],
             trade["price"], trade["shares"], trade["pnl"], trade["balance"]),
        )


def get_recent_trades(limit: int = 100) -> list[dict]:
    with _conn() as con:
        con.row_factory = sqlite3.Row
        rows = con.execute(
            "SELECT * FROM trades ORDER BY timestamp DESC LIMIT ?", (limit,)
        ).fetchall()
    return [dict(r) for r in rows]


def get_kill_switch() -> bool:
    with _conn() as con:
        row = con.execute(
            "SELECT value FROM bot_state WHERE key = 'kill_switch'"
        ).fetchone()
    return row is not None and row[0] == "1"


def set_kill_switch(active: bool):
    with _conn() as con:
        con.execute(
            "INSERT OR REPLACE INTO bot_state (key, value) VALUES ('kill_switch', ?)",
            ("1" if active else "0",),
        )
