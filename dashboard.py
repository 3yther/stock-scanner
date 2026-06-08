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


def configure(trader, scanner):
    global _trader, _scanner, _start_time
    _trader     = trader
    _scanner    = scanner
    _start_time = _time.time()


# ------------------------------------------------------------------ #
#  Routes                                                              #
# ------------------------------------------------------------------ #

@app.route("/")
def index():
    return render_template("index.html")


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
    # Annotate with whether a position is currently held
    if _trader is not None:
        st = _trader.status()
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
