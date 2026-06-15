import smtplib
import threading
from datetime import datetime, timezone
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
import config


def _send_email(subject: str, body_html: str):
    """Send one HTML email via Gmail SMTP in a background thread. No-op if the
    EMAIL_ADDRESS/EMAIL_PASSWORD env vars aren't set. Shared by the stock trade
    alerts and the Crypto Bot TJR alerts."""
    if not config.EMAIL_ADDRESS or not config.EMAIL_PASSWORD:
        return

    def _send():
        try:
            msg = MIMEMultipart("alternative")
            msg["Subject"] = subject
            msg["From"]    = config.EMAIL_ADDRESS
            msg["To"]      = config.EMAIL_ADDRESS
            msg.attach(MIMEText(body_html, "html"))
            with smtplib.SMTP_SSL("smtp.gmail.com", 465) as srv:
                srv.login(config.EMAIL_ADDRESS, config.EMAIL_PASSWORD)
                srv.sendmail(config.EMAIL_ADDRESS, config.EMAIL_ADDRESS, msg.as_string())
        except Exception as e:
            print(f"  [EMAIL] Failed: {e}")

    threading.Thread(target=_send, daemon=True).start()


def crypto_alert(headline: str, symbol: str, signal_price: float, prices: dict | None = None):
    """Crypto Bot TJR email alert. `headline` is the full subject line, e.g.
    'Crypto Bot TJR: Bullish MSS on BTC — buying 2.5×'. Includes the signal
    price, UTC time, and the current BTC/ETH prices."""
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    live = prices or {}
    px_line = " · ".join(f"{s} ${live[s]:,.2f}" for s in live) or "—"
    body = (
        f"<b>📡 {headline}</b><br><br>"
        f"Symbol       : {symbol}<br>"
        f"Signal price : ${signal_price:,.2f}<br>"
        f"Time (UTC)   : {now}<br>"
        f"Live prices  : {px_line}<br>"
    )
    _send_email(headline, body)


def trade_alert(
    action: str, symbol: str, price: float, shares: float,
    pnl: float, balance: float, sl_price: float = 0.0, tp_price: float = 0.0,
):
    if not config.EMAIL_ADDRESS or not config.EMAIL_PASSWORD:
        return

    icons = {"BUY": "📈", "SELL": "💰", "TRAILING STOP": "🛑", "TAKE PROFIT": "🎯"}
    icon  = icons.get(action, "📊")
    sign  = "+" if pnl >= 0 else ""

    subject = f"{icon} Stock Scanner: {action} {symbol} @ ${price:,.2f}"
    body = (
        f"<b>{icon} {action}</b><br><br>"
        f"Symbol   : {symbol}<br>"
        f"Price    : ${price:,.2f}<br>"
        f"Shares   : {shares:.4f}<br>"
        f"P&L      : {sign}${abs(pnl):,.2f}<br>"
        f"Balance  : ${balance:,.2f}<br>"
    )
    if sl_price:
        body += f"Trail SL : ${sl_price:,.2f}<br>"
    if tp_price:
        body += f"Take Profit: ${tp_price:,.2f}<br>"

    _send_email(subject, body)
