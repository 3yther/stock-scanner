import time as _time
import threading

from flask import Flask, jsonify, render_template, request

import config
import database as db

app = Flask(__name__)
app.config["TEMPLATES_AUTO_RELOAD"] = True

_trader  = None
_scanner = None
_start_time: float = 0.0

# Stats cache (computed on demand, cached for 60s)
_stats_cache: dict | None = None
_stats_cache_ts: float = 0.0
_STATS_TTL = 60.0


def configure(trader, scanner):
    global _trader, _scanner, _start_time
    _trader     = trader
    _scanner    = scanner
    _start_time = _time.time()


# ── Routes ────────────────────────────────────────────────────────────────

@app.route("/")
def index():
    return render_template("index.html")


@app.route("/stats")
def stats_page():
    return render_template("stats.html")


@app.route("/api/status")
def api_status():
    if _trader is None:
        return jsonify({"error": "not ready"}), 503
    data = _trader.status()
    data["uptime_seconds"] = int(_time.time() - _start_time) if _start_time else 0
    return jsonify(data)


@app.route("/api/scanner")
def api_scanner():
    if _scanner is None:
        return jsonify([])
    results = _scanner.get_results()
    if _trader is not None:
        st   = _trader.status()
        held = {p["symbol"] for p in st.get("positions", [])}
    else:
        held = set()
    for r in results:
        r["has_position"] = r["symbol"] in held
    return jsonify(results)


@app.route("/api/positions")
def api_positions():
    if _trader is None:
        return jsonify([])
    return jsonify(_trader.status()["positions"])


@app.route("/api/trades")
def api_trades():
    try:
        return jsonify(db.get_recent_trades(100))
    except Exception:
        return jsonify([])


@app.route("/api/kill-switch", methods=["GET", "POST"])
def api_kill_switch():
    if request.method == "POST":
        body   = request.get_json(force=True) or {}
        active = bool(body.get("active", False))
        db.set_kill_switch(active)
        if _trader is not None:
            _trader.kill_switch = active
        return jsonify({"active": active})
    return jsonify({"active": db.get_kill_switch()})


@app.route("/api/stats")
def api_stats():
    global _stats_cache, _stats_cache_ts
    now = _time.time()
    if _stats_cache and now - _stats_cache_ts < _STATS_TTL:
        return jsonify(_stats_cache)

    try:
        all_trades = db.get_all_trades(2000)
        snapshots  = db.get_equity_snapshots()
    except Exception:
        return jsonify({"error": "db unavailable"}), 503

    closed = [t for t in all_trades if t["action"] != "BUY"]
    wins   = [t for t in closed if t["pnl"] > 0]
    losses = [t for t in closed if t["pnl"] <= 0]

    gross_wins   = sum(t["pnl"] for t in wins)
    gross_losses = abs(sum(t["pnl"] for t in losses))
    avg_win      = gross_wins   / len(wins)   if wins   else 0.0
    avg_loss     = gross_losses / len(losses) if losses else 0.0
    profit_factor = round(gross_wins / gross_losses, 2) if gross_losses > 0 else 0.0
    win_rate     = round(len(wins) / len(closed) * 100, 1) if closed else 0.0

    best_trade  = max(closed, key=lambda t: t["pnl"],  default=None)
    worst_trade = min(closed, key=lambda t: t["pnl"],  default=None)

    # Per-symbol breakdown
    by_symbol: dict[str, dict] = {}
    for t in closed:
        sym = t["symbol"]
        if sym not in by_symbol:
            by_symbol[sym] = {"trades": 0, "wins": 0, "pnl": 0.0}
        by_symbol[sym]["trades"] += 1
        if t["pnl"] > 0:
            by_symbol[sym]["wins"] += 1
        by_symbol[sym]["pnl"] = round(by_symbol[sym]["pnl"] + t["pnl"], 2)
    by_symbol_list = sorted(
        [{"symbol": k, **v,
          "win_rate": round(v["wins"] / v["trades"] * 100, 1)}
         for k, v in by_symbol.items()],
        key=lambda x: x["pnl"], reverse=True,
    )

    # Daily P&L
    daily_pnl: dict[str, float] = {}
    for t in closed:
        day = t["timestamp"][:10]
        daily_pnl[day] = round(daily_pnl.get(day, 0.0) + t["pnl"], 2)
    daily_pnl_list = [{"date": k, "pnl": v} for k, v in sorted(daily_pnl.items())]

    result = {
        "total_trades":  len(closed),
        "win_rate":      win_rate,
        "avg_win":       round(avg_win, 2),
        "avg_loss":      round(avg_loss, 2),
        "profit_factor": profit_factor,
        "gross_wins":    round(gross_wins, 2),
        "gross_losses":  round(gross_losses, 2),
        "best_trade":    best_trade,
        "worst_trade":   worst_trade,
        "by_symbol":     by_symbol_list,
        "daily_pnl":     daily_pnl_list,
        "equity_curve":  snapshots,
        "initial_balance": config.INITIAL_BALANCE,
    }

    _stats_cache    = result
    _stats_cache_ts = now
    return jsonify(result)


def start_server(port: int = 5002):
    t = threading.Thread(
        target=lambda: app.run(
            host="0.0.0.0", port=port,
            debug=False, use_reloader=False, threaded=True,
        ),
        daemon=True,
    )
    t.start()
    return t
