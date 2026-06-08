import smtplib
import threading
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
import config


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

    def _send():
        try:
            msg = MIMEMultipart("alternative")
            msg["Subject"] = subject
            msg["From"]    = config.EMAIL_ADDRESS
            msg["To"]      = config.EMAIL_ADDRESS
            msg.attach(MIMEText(body, "html"))
            with smtplib.SMTP_SSL("smtp.gmail.com", 465) as srv:
                srv.login(config.EMAIL_ADDRESS, config.EMAIL_PASSWORD)
                srv.sendmail(config.EMAIL_ADDRESS, config.EMAIL_ADDRESS, msg.as_string())
        except Exception as e:
            print(f"  [EMAIL] Failed: {e}")

    threading.Thread(target=_send, daemon=True).start()
