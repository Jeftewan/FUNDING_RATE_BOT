"""Email alert notifications via SMTP."""
import smtplib
import logging
import time
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from datetime import datetime

log = logging.getLogger("bot")


class EmailNotifier:
    def __init__(self, config):
        self.enabled = config.ALERT_EMAIL_ENABLED
        self.smtp_host = config.SMTP_HOST
        self.smtp_port = config.SMTP_PORT
        self.smtp_user = config.SMTP_USER
        self.smtp_password = config.SMTP_PASSWORD
        self.email_to = config.ALERT_EMAIL_TO
        self.email_from = config.ALERT_EMAIL_FROM or config.SMTP_USER
        # Track sent alerts to avoid duplicate emails
        self._sent_cache = {}  # {alert_key: timestamp}
        self._cooldown_seconds = 300  # 5 min between same alert

        if self.enabled:
            if not all([self.smtp_user, self.smtp_password, self.email_to]):
                log.warning("Email alerts enabled but SMTP config incomplete. Disabling.")
                self.enabled = False
            else:
                log.info(f"Email alerts enabled: {self.email_to}")

    def send_alert(self, alert: dict) -> bool:
        """Send a single alert via email. Returns True if sent."""
        if not self.enabled:
            return False

        # Dedup: don't send the same alert within cooldown
        alert_key = f"{alert['type']}_{alert['symbol']}_{alert['exchange']}"
        now = time.time()
        if alert_key in self._sent_cache:
            if now - self._sent_cache[alert_key] < self._cooldown_seconds:
                return False

        try:
            subject, body = self._format_alert(alert)
            self._send_email(subject, body)
            self._sent_cache[alert_key] = now
            log.info(f"Alert email sent: {subject}")
            return True
        except Exception as e:
            log.error(f"Email send failed: {e}")
            return False

    def send_alerts(self, alerts: list) -> int:
        """Send multiple alerts, returns count of emails sent."""
        if not self.enabled or not alerts:
            return 0

        # Only send CRITICAL alerts by email
        critical = [a for a in alerts if a.get("severity") == "CRITICAL"]
        if not critical:
            return 0

        sent = 0
        for alert in critical:
            if self.send_alert(alert):
                sent += 1

        return sent

    def _format_alert(self, alert: dict) -> tuple:
        """Format alert into email subject and HTML body."""
        severity = alert.get("severity", "INFO")
        symbol = alert.get("symbol", "???")
        exchange = alert.get("exchange", "")
        alert_type = alert.get("type", "UNKNOWN")
        message = alert.get("message", "")

        # Subject
        emoji = "🚨" if severity == "CRITICAL" else "⚠️"
        subject = f"{emoji} {alert_type}: {symbol} ({exchange})"

        # Body
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

        action_text = ""
        if alert_type == "RATE_REVERSAL":
            action_text = (
                "<p style='color:#ef4444;font-weight:bold;font-size:18px'>"
                "⛔ ACCION REQUERIDA: Cerrar posicion inmediatamente</p>"
                "<p>El funding rate cambio de direccion. La posicion ahora "
                "esta pagando en lugar de recibir funding.</p>"
            )
        elif alert_type == "STOP_LOSS":
            action_text = (
                "<p style='color:#ef4444;font-weight:bold;font-size:18px'>"
                "⛔ STOP LOSS ALCANZADO: Cerrar posicion</p>"
                "<p>El precio cayo por debajo del stop loss configurado.</p>"
            )
        elif alert_type == "RATE_DROP":
            action_text = (
                "<p style='color:#fbbf24;font-weight:bold;font-size:18px'>"
                "⚠️ Rate cayo significativamente</p>"
                "<p>Considerar cerrar si no se recupera en los proximos cobros.</p>"
            )

        body = f"""
        <html><body style="font-family:monospace;background:#0a0b0d;color:#c8ccd0;padding:20px">
        <div style="max-width:600px;margin:0 auto;background:#111318;border-radius:12px;
                    padding:24px;border:1px solid #1a1d23">
            <h2 style="color:#fff;margin-bottom:8px">
                {emoji} Funding Bot v7.0 — Alerta
            </h2>
            <hr style="border-color:#1a1d23">

            <table style="width:100%;margin:16px 0;font-size:14px">
                <tr><td style="color:#666;padding:4px 8px">Tipo</td>
                    <td style="color:#fff;font-weight:bold">{alert_type}</td></tr>
                <tr><td style="color:#666;padding:4px 8px">Severidad</td>
                    <td style="color:{'#ef4444' if severity=='CRITICAL' else '#fbbf24'};
                        font-weight:bold">{severity}</td></tr>
                <tr><td style="color:#666;padding:4px 8px">Symbol</td>
                    <td style="color:#fff;font-weight:bold;font-size:16px">{symbol}</td></tr>
                <tr><td style="color:#666;padding:4px 8px">Exchange</td>
                    <td style="color:#fff">{exchange}</td></tr>
                <tr><td style="color:#666;padding:4px 8px">Detalle</td>
                    <td style="color:#fff">{message}</td></tr>
                <tr><td style="color:#666;padding:4px 8px">Hora</td>
                    <td style="color:#555">{now}</td></tr>
            </table>

            {action_text}

            <hr style="border-color:#1a1d23;margin-top:16px">
            <p style="font-size:11px;color:#444;text-align:center">
                Funding Rate Arbitrage Bot v7.0 — Alerta automatica
            </p>
        </div>
        </body></html>
        """
        return subject, body

    def _send_email(self, subject: str, html_body: str):
        """Send email via SMTP."""
        msg = MIMEMultipart("alternative")
        msg["From"] = self.email_from
        msg["To"] = self.email_to
        msg["Subject"] = subject
        msg.attach(MIMEText(html_body, "html"))

        with smtplib.SMTP(self.smtp_host, self.smtp_port, timeout=10) as server:
            server.starttls()
            server.login(self.smtp_user, self.smtp_password)
            server.sendmail(self.email_from, self.email_to, msg.as_string())

    def test_connection(self) -> dict:
        """Test SMTP connection. Returns {ok, error}."""
        if not self.enabled:
            return {"ok": False, "error": "Email alerts not enabled"}
        try:
            with smtplib.SMTP(self.smtp_host, self.smtp_port, timeout=10) as server:
                server.starttls()
                server.login(self.smtp_user, self.smtp_password)
            return {"ok": True, "error": ""}
        except Exception as e:
            return {"ok": False, "error": str(e)[:200]}
