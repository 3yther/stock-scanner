import json
import os
import time as _time
import threading
from datetime import datetime, timezone, timedelta

import requests as _http
from flask import Flask, jsonify, render_template, request

import backtest
import config
import database as db
import market_data

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
def landing():
    return render_template("landing.html")


@app.route("/scanner")
def index():
    # The existing stock dashboard — unchanged, just moved off "/".
    return render_template("index.html")


@app.route("/crypto")
def crypto_page():
    return render_template("crypto.html")


@app.route("/stats")
def stats_page():
    return render_template("stats.html")


@app.route("/backtest")
def backtest_page():
    return render_template("backtest.html")


@app.route("/api/backtest_run", methods=["POST"])
def api_backtest_run():
    """Kick off a read-only historical backtest in a background thread.

    Never writes to the live trades DB — results live in memory only.
    """
    body = request.get_json(force=True) or {}

    today = datetime.now(timezone.utc).date()
    default_start = (today - timedelta(days=730)).isoformat()
    start_date = (body.get("start") or default_start)[:10]
    end_date   = (body.get("end")   or today.isoformat())[:10]
    mode       = "hourly" if body.get("mode") == "hourly" else "daily"
    split      = body.get("split") if body.get("split") in ("full", "train", "validation") else "full"
    split_date = (body.get("split_date") or "")[:10] or None
    strategy   = body.get("strategy") or "macd_mtf"
    sizing     = body.get("sizing") if body.get("sizing") in ("flat", "vol_conviction") else "flat"
    exit_mode  = body.get("exit_mode") if body.get("exit_mode") in ("fixed_tp", "trail_only") else "fixed_tp"
    try:
        max_positions = int(body.get("max_positions", config.MAX_POSITIONS))
    except (TypeError, ValueError):
        max_positions = config.MAX_POSITIONS
    try:
        capital = float(body.get("capital", config.INITIAL_BALANCE))
    except (TypeError, ValueError):
        capital = config.INITIAL_BALANCE
    if capital <= 0:
        capital = config.INITIAL_BALANCE

    if backtest.is_running():
        return jsonify({"started": False, "reason": "A backtest is already running"}), 409

    started = backtest.start(start_date, end_date, capital, mode, split, split_date,
                             strategy, sizing, exit_mode, max_positions)
    print(f"[BACKTEST] run requested: {start_date}→{end_date} ${capital} mode={mode} "
          f"strategy={strategy} sizing={sizing} exit={exit_mode} "
          f"max_positions={max_positions} split={split} "
          f"split_date={split_date} started={started}", flush=True)
    return jsonify({"started": started, "start": start_date, "end": end_date,
                    "capital": capital, "mode": mode, "split": split,
                    "split_date": split_date, "strategy": strategy,
                    "sizing": sizing, "exit_mode": exit_mode,
                    "max_positions": max_positions})


@app.route("/api/backtest_status")
def api_backtest_status():
    """Progress + results for the current/last backtest (polled by /backtest)."""
    return jsonify(backtest.get_status())


@app.route("/api/strategies")
def api_strategies():
    """Available backtest strategies for the selector."""
    import strategies
    return jsonify({"strategies": strategies.strategy_list(),
                    "default": strategies.DEFAULT_STRATEGY})


@app.route("/api/status")
def api_status():
    if _trader is None:
        return jsonify({"error": "not ready"}), 503
    data = _trader.status()
    data["uptime_seconds"] = int(_time.time() - _start_time) if _start_time else 0
    return jsonify(data)


@app.route("/api/crypto/datatest")
def api_crypto_datatest():
    """TEMPORARY (Phase 0): probe whether Binance is reachable from this server's
    IP. Railway/US IPs usually get a restricted-location error — in which case
    NoisyEdge uses Coinbase (the default in quant/crypto_data.py). Remove once
    the data source is confirmed."""
    out = {"server_can_reach_binance": None}
    try:
        r = _http.get(
            "https://api.binance.com/api/v3/klines",
            params={"symbol": "BTCUSDT", "interval": "5m", "limit": 5}, timeout=10)
        out["binance_status"] = r.status_code
        try:
            out["binance_body"] = r.json()
        except Exception:
            out["binance_body"] = r.text[:300]
        out["server_can_reach_binance"] = (r.status_code == 200 and isinstance(out["binance_body"], list))
    except Exception as exc:
        out["binance_error"] = f"{type(exc).__name__}: {exc}"
        out["server_can_reach_binance"] = False
    # Also show the Coinbase fallback so you can compare in one call.
    try:
        from quant import crypto_data
        c = crypto_data.get_candles("BTC", "5m", 3)
        out["active_source"] = crypto_data.source()
        out["coinbase_sample"] = c[-1] if c else None
    except Exception as exc:
        out["crypto_data_error"] = f"{type(exc).__name__}: {exc}"
    return jsonify(out)


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
        trades = db.get_recent_trades(50)
        print(f"[TRADES] Reading from {db.db_location()} — returned {len(trades)} rows", flush=True)
        return jsonify(trades)
    except Exception as exc:
        print(f"[TRADES] DB query failed ({db.db_location()}): {exc}", flush=True)
        return jsonify([])


@app.route("/api/debug_db")
def api_debug_db():
    """Diagnostic endpoint — hit in browser to confirm which DB is in use."""
    try:
        total        = db.count_trades()
        recent       = db.get_recent_trades(10)
        latest_ts    = recent[0]["timestamp"] if recent else None
        result = {
            "backend":               "postgresql" if db.USE_PG else "sqlite",
            "location":              db.db_location(),
            "db_module_id":          id(db),
            "trade_count":           total,
            "latest_trade_timestamp": latest_ts,
            "recent_trades":         recent,
        }
        print(f"[DEBUG_DB] {result['backend']} | {result['location']} | trades={total}", flush=True)
        return jsonify(result)
    except Exception as exc:
        err = {"error": str(exc), "backend": "postgresql" if db.USE_PG else "sqlite",
               "location": db.db_location()}
        print(f"[DEBUG_DB] ERROR: {exc}", flush=True)
        return jsonify(err), 500


@app.route("/api/poly_status")
def api_poly_status():
    """Rate-limiter and cache snapshot for Polygon.io — useful for debugging 429s."""
    return jsonify(market_data.poly_status())


@app.route("/api/cache_status")
def api_cache_status():
    """Disk-cache snapshot — file path/size, per-symbol ages, calls today, hit rate."""
    return jsonify(market_data.cache_status())


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
        print(f"[STATS] Reading from {db.db_location()} — {len(all_trades)} trades, {len(snapshots)} snapshots", flush=True)
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


# ── News cache ─────────────────────────────────────────────────────────────
_news_cache: dict | None = None
_news_cache_ts: float    = 0.0
_NEWS_TTL = 300.0  # 5 minutes

# Keyword map for symbol detection in headlines
_SYM_KEYWORDS: dict[str, list[str]] = {
    "AAPL":  ["Apple", "iPhone", "AAPL"],
    "MSFT":  ["Microsoft", "Azure", "MSFT"],
    "GOOGL": ["Google", "Alphabet", "GOOGL"],
    "AMZN":  ["Amazon", "AWS", "AMZN"],
    "NVDA":  ["Nvidia", "NVDA", "GPU"],
    "META":  ["Meta", "Facebook", "Instagram", "META"],
    "TSLA":  ["Tesla", "TSLA", "Elon Musk"],
    "JPM":   ["JPMorgan", "JPM", "Jamie Dimon"],
    "JNJ":   ["Johnson & Johnson", "J&J", "JNJ"],
    "V":     ["Visa"],
    "PG":    ["Procter", "Gamble", "P&G"],
    "UNH":   ["UnitedHealth", "UNH"],
    "HD":    ["Home Depot", "HD"],
    "MA":    ["Mastercard", "MA"],
    "BAC":   ["Bank of America", "BAC"],
    "XOM":   ["Exxon", "ExxonMobil", "XOM"],
    "PFE":   ["Pfizer", "PFE"],
    "ABBV":  ["AbbVie", "ABBV"],
    "COST":  ["Costco", "COST"],
    "BRK-B": ["Berkshire", "Buffett"],
    "SPCX":  ["SPCX"],
    "SPY":   ["S&P 500", "S&P500", "SPY", "index fund"],
    "QQQ":   ["Nasdaq", "QQQ", "tech index"],
    "AMD":   ["AMD", "Advanced Micro"],
}


@app.route("/api/market_status")
def api_market_status():
    now_utc = datetime.now(timezone.utc)
    month   = now_utc.month
    et_off  = timedelta(hours=-4 if 3 <= month <= 11 else -5)
    et_now  = now_utc + et_off
    open_t  = et_now.replace(hour=9,  minute=30, second=0, microsecond=0)
    close_t = et_now.replace(hour=16, minute=0,  second=0, microsecond=0)
    is_open = et_now.weekday() < 5 and open_t <= et_now <= close_t

    if is_open:
        delta = close_t - et_now
        label = "CLOSES IN"
    else:
        next_open = open_t
        if et_now >= close_t or et_now.weekday() >= 5:
            next_open += timedelta(days=1)
        while next_open.weekday() >= 5:
            next_open += timedelta(days=1)
        delta = next_open - et_now
        label = "OPENS IN"

    s = int(delta.total_seconds())
    s = max(s, 0)
    cd = f"{s//3600:02d}:{(s%3600)//60:02d}:{s%60:02d}"
    return jsonify({"is_open": is_open, "label": label, "countdown": cd})


@app.route("/api/sparklines")
def api_sparklines():
    result = {}
    for sym in config.SYMBOLS:
        try:
            df_d, _ = market_data.get_ohlc(sym)
            if df_d is not None and len(df_d) > 1:
                result[sym] = [round(float(p), 2) for p in df_d["close"].tail(30)]
            else:
                result[sym] = []
        except Exception:
            result[sym] = []
    return jsonify(result)


@app.route("/api/ohlc/<symbol>")
def api_ohlc(symbol):
    symbol = symbol.upper()
    try:
        df_d, _ = market_data.get_ohlc(symbol)
        if df_d is None or df_d.empty:
            return jsonify([])
        rows = []
        for _, row in df_d.tail(90).iterrows():
            rows.append({
                "date":   str(row.get("datetime", ""))[:10],
                "open":   round(float(row["open"]),   2),
                "high":   round(float(row["high"]),   2),
                "low":    round(float(row["low"]),    2),
                "close":  round(float(row["close"]),  2),
                "volume": int(row["volume"]),
            })
        return jsonify(rows)
    except Exception as e:
        return jsonify([])


def _ingest_article(a: dict, articles: list) -> None:
    """Parse one NewsAPI article dict and append to articles if valid and non-duplicate."""
    title = (a.get("title") or "").strip()
    if not title or title == "[Removed]":
        return
    if any(x["title"] == title for x in articles):
        return
    text = title + " " + (a.get("description") or "")
    syms = [s for s, kws in _SYM_KEYWORDS.items()
            if any(k.lower() in text.lower() for k in kws)]
    articles.append({
        "id":          len(articles),
        "title":       title,
        "source":      (a.get("source") or {}).get("name", ""),
        "url":         a.get("url", "#"),
        "publishedAt": a.get("publishedAt", ""),
        "symbols":     syms,
        "sentiment":   "NEUTRAL",
    })


@app.route("/api/news")
def api_news():
    global _news_cache, _news_cache_ts
    now = _time.time()
    if _news_cache and now - _news_cache_ts < _NEWS_TTL:
        print(f"[NEWS] Cache hit — {len(_news_cache.get('articles', []))} articles", flush=True)
        return jsonify(_news_cache)

    news_key      = os.getenv("NEWS_API_KEY", "")
    anthropic_key = os.getenv("ANTHROPIC_API_KEY", "")
    articles: list[dict] = []

    print(f"[NEWS] NEWS_API_KEY present={bool(news_key)}  ANTHROPIC_KEY present={bool(anthropic_key)}", flush=True)

    if not news_key:
        print("[NEWS] NEWS_API_KEY not set — returning empty feed. Set it in Railway env vars.", flush=True)
        result = {"articles": [], "per_symbol": {s: "NEUTRAL" for s in config.SYMBOLS}}
        _news_cache = result
        _news_cache_ts = now
        return jsonify(result)

    # ── Fetch 1: top-headlines from finance/news sources ──────────────────
    try:
        r1 = _http.get(
            "https://newsapi.org/v2/top-headlines",
            params={
                "sources":  "bbc-news,bloomberg,financial-post,the-wall-street-journal",
                "apiKey":   news_key,
                "pageSize": 20,
                "language": "en",
            },
            timeout=8,
        )
        data1 = r1.json()
        print(
            f"[NEWS] top-headlines status={r1.status_code} "
            f"raw={len(data1.get('articles', []))} "
            f"msg={data1.get('message', 'ok')}",
            flush=True,
        )
        if not r1.ok:
            print(f"[NEWS] top-headlines error body: {data1}", flush=True)
        for a in data1.get("articles", []):
            _ingest_article(a, articles)
    except Exception as exc:
        print(f"[NEWS] top-headlines exception: {exc}", flush=True)

    # ── Fetch 2: everything with per-stock OR query ───────────────────────
    try:
        # Build "NVDA OR Nvidia OR AAPL OR Apple OR ..." query from keyword map
        parts: list[str] = [" OR ".join(kws[:2]) for kws in _SYM_KEYWORDS.values()]
        stock_query = " OR ".join(parts[:14])   # cap at 14 symbols to stay under URL limits
        print(f"[NEWS] everything query (first 120 chars): {stock_query[:120]}", flush=True)
        r2 = _http.get(
            "https://newsapi.org/v2/everything",
            params={
                "q":        stock_query,
                "sortBy":   "publishedAt",
                "language": "en",
                "pageSize": 20,
                "apiKey":   news_key,
            },
            timeout=8,
        )
        data2 = r2.json()
        print(
            f"[NEWS] everything status={r2.status_code} "
            f"raw={len(data2.get('articles', []))} "
            f"msg={data2.get('message', 'ok')}",
            flush=True,
        )
        if not r2.ok:
            print(f"[NEWS] everything error body: {data2}", flush=True)
        for a in data2.get("articles", []):
            _ingest_article(a, articles)
    except Exception as exc:
        print(f"[NEWS] everything exception: {exc}", flush=True)

    # Sort by publishedAt descending, keep 10 most recent
    articles.sort(key=lambda x: x.get("publishedAt", ""), reverse=True)
    articles = articles[:10]
    for i, a in enumerate(articles):
        a["id"] = i  # re-sequence IDs after sort

    print(f"[NEWS] Articles after merge+dedup+sort: {len(articles)}", flush=True)
    for a in articles:
        print(f"  [{a['publishedAt'][:10]}] {a['source']}: {a['title'][:70]}", flush=True)

    # ── Sentiment scoring via Anthropic ───────────────────────────────────
    if anthropic_key and articles:
        try:
            import anthropic as _ant
            client = _ant.Anthropic(api_key=anthropic_key)
            txt  = "\n".join(f"{a['id']}. {a['title']}" for a in articles)
            resp = client.messages.create(
                model="claude-sonnet-4-6",
                max_tokens=256,
                messages=[{"role": "user", "content": (
                    "Score each headline as BULLISH, BEARISH, or NEUTRAL for stock market sentiment. "
                    "Return ONLY a JSON array: [{\"id\":0,\"sentiment\":\"BULLISH\"},…]\n\n" + txt
                )}],
            )
            raw = resp.content[0].text
            s, e = raw.find("["), raw.rfind("]") + 1
            if s >= 0 and e > s:
                for item in json.loads(raw[s:e]):
                    idx  = item.get("id")
                    sent = item.get("sentiment", "NEUTRAL").upper()
                    if isinstance(idx, int) and 0 <= idx < len(articles) and sent in ("BULLISH", "BEARISH", "NEUTRAL"):
                        articles[idx]["sentiment"] = sent
            print(f"[NEWS] Anthropic scored {len(articles)} articles", flush=True)
        except Exception as exc:
            print(f"[NEWS] Anthropic error: {exc}", flush=True)

    # ── Aggregate per-symbol sentiment ────────────────────────────────────
    per_symbol: dict[str, str] = {s: "NEUTRAL" for s in config.SYMBOLS}
    for sym in config.SYMBOLS:
        sa = [a for a in articles if sym in a.get("symbols", [])]
        if sa:
            counts = {"BULLISH": 0, "BEARISH": 0, "NEUTRAL": 0}
            for a in sa:
                counts[a.get("sentiment", "NEUTRAL")] += 1
            per_symbol[sym] = max(counts, key=counts.get)

    result = {"articles": articles, "per_symbol": per_symbol}
    _news_cache    = result
    _news_cache_ts = now
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
