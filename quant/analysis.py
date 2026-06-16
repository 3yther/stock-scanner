"""
analysis.py — historical signal analysis over the tjr_buys ledger.

Read-only. Each DCA buy is a lot; since the bot only buys, P&L is unrealised
(mark-to-market at the current price): pnl = (price_now - buy_price) × qty. Breaks
performance down by signal type, symbol, regime, and multiplier bucket, plus a
signal-type × regime grid and a one-line auto READOUT of the standout finding.
"""

from quant import crypto_data, crypto_db, tjr_strategy

_PRETTY = {"tjr_mss": "MSS ×", "tjr_fvg": "FVG ×", "dca_interval": "Interval"}


def _agg(rows, keyfn):
    groups = {}
    for r in rows:
        g = groups.setdefault(keyfn(r), {"count": 0, "usd": 0.0, "pnl": 0.0, "wins": 0})
        g["count"] += 1; g["usd"] += r["usd_amount"]; g["pnl"] += r["pnl"]; g["wins"] += 1 if r["win"] else 0
    out = []
    for k, g in groups.items():
        out.append({"key": k, "count": g["count"], "usd": round(g["usd"], 2),
                    "total_pnl": round(g["pnl"], 2),
                    "avg_pnl": round(g["pnl"] / g["count"], 4) if g["count"] else 0.0,
                    "win_rate": round(g["wins"] / g["count"] * 100, 1) if g["count"] else 0.0})
    return sorted(out, key=lambda x: x["total_pnl"], reverse=True)


def _readout(grid, by_mult):
    """One-line standout finding from the type×regime grid + multiplier buckets."""
    sig = [c for c in grid if c["type"] in ("tjr_mss", "tjr_fvg") and c["count"] >= 3]
    bits = []
    if sig:
        best = max(sig, key=lambda c: c["avg_pnl"])
        worst = min(sig, key=lambda c: c["avg_pnl"])
        name = lambda t: "FVG fills" if t == "tjr_fvg" else "MSS buys"
        bits.append(f"{name(best['type'])} in {best['regime']} are the top performer "
                    f"(avg ${best['avg_pnl']:.2f}/buy)")
        if worst is not best:
            bits.append(f"{name(worst['type'])} in {worst['regime']} lag "
                        f"(avg ${worst['avg_pnl']:.2f}/buy)")
    # do the big multipliers earn their size?
    hi = [m for m in by_mult if m["key"] in ("×2.5", "×3.0") and m["count"] >= 3]
    lo = [m for m in by_mult if m["key"] in ("×1.0", "×1.25", "×1.5") and m["count"] >= 3]
    if hi and lo:
        ah = sum(m["avg_pnl"] for m in hi) / len(hi)
        al = sum(m["avg_pnl"] for m in lo) / len(lo)
        bits.append(f"high-conviction buys {'out-earn' if ah > al else 'do NOT beat'} "
                    f"smaller ones per buy")
    return "; ".join(bits) + "." if bits else "Not enough buys yet to call an edge — let the bot run."


def compute() -> dict:
    px = {}
    for s in tjr_strategy.SYMBOLS:
        try:
            c = crypto_data.get_candles(s, "5m", 2)
            px[s] = float(c[-1]["close"]) if c else 0.0
        except Exception:
            px[s] = 0.0

    rows = []
    for b in crypto_db.get_recent_buys(100000):
        price = float(b["price"]); usd = float(b["usd_amount"])
        qty = usd / price if price > 0 else 0.0
        pnl = (px.get(b["symbol"], 0.0) - price) * qty
        rows.append({"symbol": b["symbol"], "type": b["type"], "regime": b.get("regime") or "UNKNOWN",
                     "multiplier": b.get("multiplier") or 1.0, "usd_amount": usd,
                     "pnl": pnl, "win": pnl > 0})

    by_type   = _agg(rows, lambda r: r["type"])
    by_symbol = _agg(rows, lambda r: r["symbol"])
    by_regime = _agg(rows, lambda r: r["regime"])
    by_mult   = _agg(rows, lambda r: f"×{round(float(r['multiplier']), 2):g}")

    grid_map = {}
    for r in rows:
        cell = grid_map.setdefault((r["type"], r["regime"]), {"pnl": 0.0, "count": 0})
        cell["pnl"] += r["pnl"]; cell["count"] += 1
    grid = [{"type": t, "regime": reg, "count": c["count"],
             "avg_pnl": round(c["pnl"] / c["count"], 4) if c["count"] else 0.0,
             "total_pnl": round(c["pnl"], 2)} for (t, reg), c in grid_map.items()]

    return {
        "total_buys": len(rows), "prices": px,
        "by_type": by_type, "by_symbol": by_symbol, "by_regime": by_regime,
        "by_multiplier": by_mult, "grid": grid,
        "readout": _readout(grid, by_mult),
        "note": "P&L is unrealised (mark-to-market at current price); the DCA bot only buys.",
    }
