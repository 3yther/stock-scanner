"""
alerts.py — multi-channel alert dispatcher for the Crypto Bot.

Channels (all optional, configured via env vars on Railway — missing ones are
skipped silently):
  - email   : existing Gmail (EMAIL_ADDRESS / EMAIL_PASSWORD)
  - discord : DISCORD_WEBHOOK_URL
  - slack   : SLACK_WEBHOOK_URL
  - sms     : Twilio (TWILIO_SID / TWILIO_TOKEN / TWILIO_FROM / ALERT_PHONE)

Events (each toggleable via the settings panel): mss_bull, mss_bear, fvg,
regime, drawdown.

Anti-spam:
  - MSS / regime / drawdown alert immediately.
  - FVG alerts are BATCHED into a summary (default every 30 min) AND the bot only
    dispatches an FVG alert when an entry actually triggered a buy.
  - Per-channel cooldown + short dedupe window prevent floods.

Settings persist in crypto_db (tjr_settings) so toggles survive restarts.
"""

import json
import os
import threading
import time
from datetime import datetime, timezone

import requests as _http

import config
import notifier
from quant import crypto_db

UTC = timezone.utc

DEFAULT_SETTINGS = {
    "channels": {"email": True, "discord": True, "slack": True, "sms": False},
    "events":   {"mss_bull": True, "mss_bear": True, "fvg": True, "regime": True, "drawdown": True},
    "fvg_batch_minutes": 30,
    "drawdown_pct": 10.0,
}

_COOLDOWN   = 4.0     # seconds between sends to the same channel
_DEDUPE_TTL = 90.0    # seconds an (event,symbol,minute) key is suppressed

_lock = threading.Lock()
_last_sent: dict[str, float] = {}
_dedupe: dict[str, float] = {}
_fvg_buffer: list[dict] = []
_fvg_last_flush = time.time()   # start the first batch window now (don't flush the first FVG instantly)


# ── Settings (persisted) ───────────────────────────────────────────────────

def _merge(base, over):
    out = json.loads(json.dumps(base))
    for k, v in (over or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k].update(v)
        else:
            out[k] = v
    return out


def get_settings() -> dict:
    raw = crypto_db.get_setting("alert_settings")
    if raw:
        try:
            return _merge(DEFAULT_SETTINGS, json.loads(raw))
        except Exception:
            pass
    return json.loads(json.dumps(DEFAULT_SETTINGS))


def set_settings(patch: dict) -> dict:
    merged = _merge(get_settings(), patch)
    crypto_db.set_setting("alert_settings", json.dumps(merged))
    return merged


def channels_configured() -> dict:
    return {
        "email":   bool(config.EMAIL_ADDRESS and config.EMAIL_PASSWORD),
        "discord": bool(os.getenv("DISCORD_WEBHOOK_URL")),
        "slack":   bool(os.getenv("SLACK_WEBHOOK_URL")),
        "sms":     bool(os.getenv("TWILIO_SID") and os.getenv("TWILIO_TOKEN")
                        and os.getenv("TWILIO_FROM") and os.getenv("ALERT_PHONE")),
    }


# ── Channel senders (each best-effort, non-blocking) ───────────────────────

def _post_json(url, payload):
    try:
        _http.post(url, json=payload, timeout=8)
    except Exception as exc:
        print(f"[ALERT] webhook failed: {exc}", flush=True)


def _send_channel(ch, subject, text):
    try:
        if ch == "email":
            notifier._send_email(subject, text.replace("\n", "<br>"))
        elif ch == "discord":
            url = os.getenv("DISCORD_WEBHOOK_URL")
            if url:
                _post_json(url, {"content": f"**{subject}**\n{text}"[:1900]})
        elif ch == "slack":
            url = os.getenv("SLACK_WEBHOOK_URL")
            if url:
                _post_json(url, {"text": f"*{subject}*\n{text}"})
        elif ch == "sms":
            sid, tok = os.getenv("TWILIO_SID"), os.getenv("TWILIO_TOKEN")
            frm, to = os.getenv("TWILIO_FROM"), os.getenv("ALERT_PHONE")
            if all([sid, tok, frm, to]):
                _http.post(f"https://api.twilio.com/2010-04-01/Accounts/{sid}/Messages.json",
                           data={"From": frm, "To": to, "Body": subject[:300]}, auth=(sid, tok), timeout=8)
    except Exception as exc:
        print(f"[ALERT] {ch} failed: {exc}", flush=True)


def _emit(subject, text, dedupe_key=None):
    """Fan out to every enabled+configured channel, honouring cooldown/dedupe."""
    now = time.time()
    if dedupe_key:
        with _lock:
            for k, v in list(_dedupe.items()):
                if now - v > _DEDUPE_TTL:
                    del _dedupe[k]
            if dedupe_key in _dedupe:
                return
            _dedupe[dedupe_key] = now
    s = get_settings(); conf = channels_configured()
    print(f"[ALERT] {subject}", flush=True)
    for ch in ("email", "discord", "slack", "sms"):
        if not s["channels"].get(ch) or not conf.get(ch):
            continue
        with _lock:
            if now - _last_sent.get(ch, 0) < _COOLDOWN:
                continue
            _last_sent[ch] = now
        threading.Thread(target=_send_channel, args=(ch, subject, text), daemon=True).start()


def _body(headline, **kv):
    now = datetime.now(UTC).strftime("%Y-%m-%d %H:%M:%S UTC")
    lines = [headline, ""]
    if kv.get("symbol"):      lines.append(f"Symbol    : {kv['symbol']}")
    if kv.get("signal"):      lines.append(f"Signal    : {kv['signal']}")
    if kv.get("direction"):   lines.append(f"Direction : {kv['direction']}")
    if kv.get("price") is not None: lines.append(f"Price     : ${kv['price']:,.2f}")
    lines.append(f"Time      : {now}")
    if kv.get("regime"):      lines.append(f"Regime    : {kv['regime']}")
    if kv.get("buy_amount"):  lines.append(f"Buy       : ${kv['buy_amount']:,.2f}")
    return "\n".join(lines)


# ── Public dispatch ────────────────────────────────────────────────────────

def dispatch(event_type, headline, *, symbol=None, signal=None, direction=None,
             price=None, regime=None, buy_amount=None):
    """event_type ∈ {mss_bull, mss_bear, fvg, regime, drawdown}."""
    s = get_settings()
    if not s["events"].get(event_type, True):
        return
    text = _body(headline, symbol=symbol, signal=signal, direction=direction,
                 price=price, regime=regime, buy_amount=buy_amount)
    if event_type == "fvg":
        with _lock:
            _fvg_buffer.append({"symbol": symbol, "price": price, "regime": regime,
                                "buy": buy_amount or 0.0})
        flush_due()
        return
    minute = int(time.time() // 60)
    _emit(headline, text, dedupe_key=f"{event_type}:{symbol}:{minute}")


def flush_due(force=False):
    """Send the batched FVG summary if the batch window has elapsed. Called each
    bot cycle so a quiet buffer still flushes on time."""
    global _fvg_last_flush
    s = get_settings()
    batch_s = max(60, int(s.get("fvg_batch_minutes", 30)) * 60)
    with _lock:
        if not _fvg_buffer:
            return
        if not force and time.time() - _fvg_last_flush < batch_s:
            return
        items = list(_fvg_buffer); _fvg_buffer.clear(); _fvg_last_flush = time.time()
    n = len(items); tot = sum(i.get("buy", 0) for i in items)
    head = (f"Crypto Bot TJR: {n} Bullish FVG buy{'s' if n != 1 else ''} "
            f"(last {batch_s // 60}m) — ${tot:,.2f} deployed")
    lines = [head, ""] + [f"{i['symbol']} ${(i['price'] or 0):,.2f} · {i.get('regime') or '—'} · "
                          f"buy ${i.get('buy', 0):,.2f}" for i in items[-20:]]
    _emit(head, "\n".join(lines))
