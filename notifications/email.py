"""WhatsApp notifications via CallMeBot API."""
import logging
import time
import urllib.request
import urllib.parse
from datetime import datetime

log = logging.getLogger("bot")


class EmailNotifier:
    """WhatsApp notifier via CallMeBot (keeps class name for compatibility)."""

    def __init__(self, state_manager):
        self.state_manager = state_manager
        self._sent_cache = {}
        self._cooldown_seconds = 300
        self._sync_from_state()

    def _sync_from_state(self):
        """Sync WhatsApp config from state manager."""
        s = self.state_manager.state
        self.enabled = s.get("email_enabled", False)
        # Clean phone: remove +, spaces, dashes
        raw_phone = s.get("wa_phone", "")
        self.wa_phone = raw_phone.replace("+", "").replace(" ", "").replace("-", "")
        self.wa_apikey = s.get("wa_apikey", "").strip()

        if self.enabled and not all([self.wa_phone, self.wa_apikey]):
            self.enabled = False

    def send_alert(self, alert: dict) -> bool:
        """Send a single alert via WhatsApp. Returns True if sent."""
        self._sync_from_state()
        if not self.enabled:
            log.warning(f"WhatsApp disabled (email_enabled={self.enabled}, "
                        f"phone={'set' if self.wa_phone else 'empty'}, "
                        f"apikey={'set' if self.wa_apikey else 'empty'}), "
                        f"skipping: {alert.get('type')} {alert.get('symbol')}")
            return False

        alert_key = f"{alert['type']}_{alert['symbol']}_{alert.get('exchange', '')}"
        now = time.time()
        if alert_key in self._sent_cache:
            if now - self._sent_cache[alert_key] < self._cooldown_seconds:
                remaining = self._cooldown_seconds - (now - self._sent_cache[alert_key])
                log.info(f"WhatsApp cooldown for {alert_key}, {remaining:.0f}s left")
                return False

        try:
            log.info(f"Sending WhatsApp: {alert.get('type')} {alert.get('symbol')}...")
            text = self._format_message(alert)
            self._send_whatsapp(text)
            self._sent_cache[alert_key] = now
            log.info(f"WhatsApp alert SENT OK: {alert.get('type')} {alert.get('symbol')}")
            return True
        except Exception as e:
            log.error(f"WhatsApp send FAILED: {e}")
            return False

    def send_alerts(self, alerts: list) -> int:
        """Send multiple alerts, returns count sent."""
        self._sync_from_state()
        if not self.enabled:
            log.warning("WhatsApp alerts skipped: notifications disabled")
            return 0
        if not alerts:
            return 0

        # Send CRITICAL, WARNING, and INFO (for POSITION_CLOSED)
        actionable = [a for a in alerts
                      if a.get("severity") in ("CRITICAL", "WARNING", "INFO")]
        if not actionable:
            return 0

        sent = 0
        for alert in actionable:
            if self.send_alert(alert):
                sent += 1
        return sent

    def _format_message(self, alert: dict) -> str:
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
        """Send WhatsApp message via CallMeBot API (HTTPS GET)."""
        encoded_text = urllib.parse.quote(text)
        url = (
            f"https://api.callmebot.com/whatsapp.php"
            f"?phone={self.wa_phone}"
            f"&text={encoded_text}"
            f"&apikey={self.wa_apikey}"
        )
        log.debug(f"CallMeBot URL: phone={self.wa_phone}, apikey={self.wa_apikey[:3]}...")
        req = urllib.request.Request(url, method="GET")
        with urllib.request.urlopen(req, timeout=15) as resp:
            status = resp.getcode()
            body = resp.read().decode("utf-8", errors="replace")
            log.debug(f"CallMeBot response: {status} — {body[:100]}")
            if status != 200:
                raise RuntimeError(f"CallMeBot returned {status}: {body}")

    def test_connection(self) -> dict:
        """Test WhatsApp by sending a test message."""
        self._sync_from_state()
        if not all([self.wa_phone, self.wa_apikey]):
            return {"ok": False, "error": "Configura telefono y API key de CallMeBot"}
        try:
            self._send_whatsapp("✅ Funding Bot — Prueba de WhatsApp OK")
            return {"ok": True, "error": ""}
        except urllib.error.HTTPError as e:
            body = e.read().decode("utf-8", errors="replace")[:200]
            if e.code == 401 or "apikey" in body.lower():
                return {"ok": False, "error": "API key invalida — verifica tu key de CallMeBot"}
            return {"ok": False, "error": f"Error HTTP {e.code}: {body}"}
        except urllib.error.URLError as e:
            return {"ok": False, "error": f"Sin conexion a CallMeBot: {str(e.reason)[:100]}"}
        except Exception as e:
            return {"ok": False, "error": f"Error: {str(e)[:200]}"}
