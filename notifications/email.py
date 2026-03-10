"""Notification system — WhatsApp (CallMeBot) + Email SMTP."""
import smtplib
import logging
import time
import urllib.request
import urllib.parse
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from datetime import datetime

log = logging.getLogger("bot")


class EmailNotifier:
    def __init__(self, state_manager):
        self.state_manager = state_manager
        self._sent_cache = {}
        self._cooldown_seconds = 300
        self._sync_from_state()

    def _sync_from_state(self):
        """Sync notification config from state manager."""
        s = self.state_manager.state
        # General
        self.enabled = s.get("email_enabled", False)
        self.notify_method = s.get("notify_method", "email")  # "email", "whatsapp"

        # SMTP config
        self.smtp_host = s.get("smtp_host", "smtp.gmail.com")
        self.smtp_port = s.get("smtp_port", 587)
        self.smtp_user = s.get("smtp_user", "")
        self.smtp_password = s.get("smtp_password", "")
        self.email_to = s.get("email_to", "")
        self.email_from = s.get("smtp_user", "")

        # WhatsApp (CallMeBot) config
        self.wa_phone = s.get("wa_phone", "")
        self.wa_apikey = s.get("wa_apikey", "")

        # Validate
        if self.enabled:
            if self.notify_method == "email":
                if not all([self.smtp_user, self.smtp_password, self.email_to]):
                    self.enabled = False
            elif self.notify_method == "whatsapp":
                if not all([self.wa_phone, self.wa_apikey]):
                    self.enabled = False

    def send_alert(self, alert: dict) -> bool:
        """Send a single alert. Returns True if sent."""
        self._sync_from_state()
        if not self.enabled:
            return False

        alert_key = f"{alert['type']}_{alert['symbol']}_{alert.get('exchange', '')}"
        now = time.time()
        if alert_key in self._sent_cache:
            if now - self._sent_cache[alert_key] < self._cooldown_seconds:
                return False

        try:
            if self.notify_method == "whatsapp":
                text = self._format_whatsapp(alert)
                self._send_whatsapp(text)
            else:
                subject, body = self._format_email_alert(alert)
                self._send_email(subject, body)

            self._sent_cache[alert_key] = now
            log.info(f"Alert sent ({self.notify_method}): {alert.get('type')} {alert.get('symbol')}")
            return True
        except Exception as e:
            log.error(f"Notification send failed ({self.notify_method}): {e}")
            return False

    def send_alerts(self, alerts: list) -> int:
        """Send multiple alerts, returns count sent."""
        self._sync_from_state()
        if not self.enabled or not alerts:
            return 0

        critical = [a for a in alerts if a.get("severity") in ("CRITICAL", "WARNING")]
        if not critical:
            return 0

        sent = 0
        for alert in critical:
            if self.send_alert(alert):
                sent += 1
        return sent

    # ── WhatsApp (CallMeBot) ─────────────────────────────────

    def _format_whatsapp(self, alert: dict) -> str:
        """Format alert as plain text for WhatsApp."""
        severity = alert.get("severity", "INFO")
        symbol = alert.get("symbol", "???")
        exchange = alert.get("exchange", "")
        alert_type = alert.get("type", "UNKNOWN")
        message = alert.get("message", "")

        icon = "🚨" if severity == "CRITICAL" else "⚠️" if severity == "WARNING" else "ℹ️"
        now = datetime.now().strftime("%H:%M:%S")

        lines = [
            f"{icon} *FUNDING BOT*",
            f"*{alert_type}*",
            f"Token: *{symbol}*",
        ]
        if exchange:
            lines.append(f"Exchange: {exchange}")
        lines.append(f"Detalle: {message}")
        lines.append(f"Hora: {now}")

        if alert_type == "RATE_REVERSAL":
            lines.append("\n🔴 *CERRAR POSICION INMEDIATAMENTE*")
        elif alert_type == "RATE_DROP":
            lines.append("\n🟡 Considerar cerrar posicion")
        elif alert_type == "PRE_PAYMENT_UNFAVORABLE":
            lines.append("\n🟡 Tasa desfavorable antes del pago")
        elif alert_type == "POSITION_CLOSED":
            lines.append("\n🟢 Posicion cerrada")

        return "\n".join(lines)

    def _send_whatsapp(self, text: str):
        """Send WhatsApp message via CallMeBot API."""
        encoded = urllib.parse.quote_plus(text)
        url = (
            f"https://api.callmebot.com/whatsapp.php"
            f"?phone={self.wa_phone}"
            f"&text={encoded}"
            f"&apikey={self.wa_apikey}"
        )
        req = urllib.request.Request(url, method="GET")
        with urllib.request.urlopen(req, timeout=15) as resp:
            status = resp.getcode()
            if status != 200:
                body = resp.read().decode("utf-8", errors="replace")
                raise RuntimeError(f"CallMeBot returned {status}: {body}")

    def test_whatsapp(self) -> dict:
        """Test WhatsApp connection."""
        self._sync_from_state()
        if not all([self.wa_phone, self.wa_apikey]):
            return {"ok": False, "error": "Configura telefono y API key de CallMeBot"}
        try:
            self._send_whatsapp("✅ Funding Bot — Prueba de notificacion WhatsApp OK")
            return {"ok": True, "error": ""}
        except Exception as e:
            err = str(e)[:200]
            if "urlopen" in err or "getaddrinfo" in err:
                return {"ok": False, "error": "Sin acceso a internet desde el servidor"}
            return {"ok": False, "error": f"Error: {err}"}

    # ── Email (SMTP) ─────────────────────────────────────────

    def _format_email_alert(self, alert: dict) -> tuple:
        """Format alert into email subject and HTML body."""
        severity = alert.get("severity", "INFO")
        symbol = alert.get("symbol", "???")
        exchange = alert.get("exchange", "")
        alert_type = alert.get("type", "UNKNOWN")
        message = alert.get("message", "")

        emoji = "🚨" if severity == "CRITICAL" else "⚠️" if severity == "WARNING" else "ℹ️"
        subject = f"{emoji} {alert_type}: {symbol}" + (f" ({exchange})" if exchange else "")

        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

        action_text = ""
        if alert_type == "RATE_REVERSAL":
            action_text = (
                "<p style='color:#ef4444;font-weight:bold;font-size:18px'>"
                "ACCION: Cerrar posicion inmediatamente</p>"
            )
        elif alert_type == "PRE_PAYMENT_UNFAVORABLE":
            action_text = (
                "<p style='color:#fbbf24;font-weight:bold;font-size:18px'>"
                "REVISAR: Tasa desfavorable antes del pago</p>"
            )
        elif alert_type == "RATE_DROP":
            action_text = (
                "<p style='color:#fbbf24;font-weight:bold;font-size:18px'>"
                "Rate cayo significativamente — considerar cerrar</p>"
            )
        elif alert_type == "POSITION_CLOSED":
            action_text = (
                "<p style='color:#22c55e;font-weight:bold;font-size:18px'>"
                "Posicion cerrada — resumen incluido</p>"
            )

        body = f"""
        <html><body style="font-family:monospace;background:#0a0b0d;color:#c8ccd0;padding:20px">
        <div style="max-width:600px;margin:0 auto;background:#111318;border-radius:12px;
                    padding:24px;border:1px solid #1a1d23">
            <h2 style="color:#fff;margin-bottom:8px">
                {emoji} Funding Bot v8.0 — Alerta
            </h2>
            <hr style="border-color:#1a1d23">

            <table style="width:100%;margin:16px 0;font-size:14px">
                <tr><td style="color:#666;padding:4px 8px">Tipo</td>
                    <td style="color:#fff;font-weight:bold">{alert_type}</td></tr>
                <tr><td style="color:#666;padding:4px 8px">Severidad</td>
                    <td style="color:{'#ef4444' if severity=='CRITICAL' else '#fbbf24' if severity=='WARNING' else '#22c55e'};
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
                Funding Rate Arbitrage Bot v8.0
            </p>
        </div>
        </body></html>
        """
        return subject, body

    def _send_email(self, subject: str, html_body: str):
        """Send email via SMTP (STARTTLS on 587 or SSL on 465)."""
        msg = MIMEMultipart("alternative")
        msg["From"] = self.email_from
        msg["To"] = self.email_to
        msg["Subject"] = subject
        msg.attach(MIMEText(html_body, "html"))

        if self.smtp_port == 465:
            with smtplib.SMTP_SSL(self.smtp_host, self.smtp_port, timeout=10) as server:
                server.login(self.smtp_user, self.smtp_password)
                server.sendmail(self.email_from, self.email_to, msg.as_string())
        else:
            with smtplib.SMTP(self.smtp_host, self.smtp_port, timeout=10) as server:
                server.starttls()
                server.login(self.smtp_user, self.smtp_password)
                server.sendmail(self.email_from, self.email_to, msg.as_string())

    def test_connection(self) -> dict:
        """Test notification connection (email or whatsapp)."""
        self._sync_from_state()
        if self.notify_method == "whatsapp":
            return self.test_whatsapp()
        # SMTP test
        if not all([self.smtp_host, self.smtp_user, self.smtp_password]):
            return {"ok": False, "error": "Configuracion SMTP incompleta"}
        try:
            if self.smtp_port == 465:
                with smtplib.SMTP_SSL(self.smtp_host, self.smtp_port, timeout=10) as server:
                    server.login(self.smtp_user, self.smtp_password)
            else:
                with smtplib.SMTP(self.smtp_host, self.smtp_port, timeout=10) as server:
                    server.starttls()
                    server.login(self.smtp_user, self.smtp_password)
            return {"ok": True, "error": ""}
        except OSError as e:
            if e.errno == 101:
                return {"ok": False, "error": "Red no disponible — prueba WhatsApp como alternativa"}
            if "getaddrinfo" in str(e) or "Name or service not known" in str(e):
                return {"ok": False, "error": f"No se pudo resolver DNS para {self.smtp_host}"}
            return {"ok": False, "error": f"Error de red: {str(e)[:200]}"}
        except smtplib.SMTPAuthenticationError:
            return {"ok": False, "error": "Credenciales SMTP incorrectas. Para Gmail usa una App Password"}
        except Exception as e:
            return {"ok": False, "error": str(e)[:200]}
