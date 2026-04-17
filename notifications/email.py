"""Telegram notifications via Bot API."""
import json
import logging
import time
import urllib.request
import urllib.parse
from datetime import datetime

log = logging.getLogger("bot")


class EmailNotifier:
    """Telegram notifier (keeps class name for compatibility)."""

    def __init__(self, state_manager):
        self.state_manager = state_manager
        self._sent_cache = {}
        self._cooldown_seconds = 60
        self._sync_from_state()

    def _sync_from_state(self):
        """Sync Telegram config from state manager."""
        s = self.state_manager.state
        self.enabled = s.get("email_enabled", False)
        self.tg_chat_id = str(s.get("tg_chat_id", "")).strip()
        self.tg_bot_token = str(s.get("tg_bot_token", "")).strip()

        if self.enabled and not all([self.tg_chat_id, self.tg_bot_token]):
            self.enabled = False

    def send_alert(self, alert: dict) -> bool:
        """Send a single alert via Telegram. Returns True if sent."""
        self._sync_from_state()
        if not self.enabled:
            log.warning(f"Telegram disabled (enabled={self.enabled}, "
                        f"chat_id={'set' if self.tg_chat_id else 'empty'}, "
                        f"token={'set' if self.tg_bot_token else 'empty'}), "
                        f"skipping: {alert.get('type')} {alert.get('symbol')}")
            return False

        alert_key = f"{alert['type']}_{alert['symbol']}_{alert.get('exchange', '')}"
        now = time.time()
        if alert_key in self._sent_cache:
            if now - self._sent_cache[alert_key] < self._cooldown_seconds:
                remaining = self._cooldown_seconds - (now - self._sent_cache[alert_key])
                log.info(f"Telegram cooldown for {alert_key}, {remaining:.0f}s left")
                return False

        try:
            log.info(f"Sending Telegram: {alert.get('type')} {alert.get('symbol')}...")
            text = self._format_message(alert)
            self._send_telegram(text)
            self._sent_cache[alert_key] = now
            log.info(f"Telegram alert SENT OK: {alert.get('type')} {alert.get('symbol')}")
            return True
        except Exception as e:
            log.error(f"Telegram send FAILED: {e}")
            return False

    def send_alerts(self, alerts: list) -> int:
        """Send multiple alerts, returns count sent."""
        self._sync_from_state()
        if not self.enabled:
            log.warning("Telegram alerts skipped: notifications disabled")
            return 0
        if not alerts:
            return 0

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
        """Format alert as Markdown for Telegram."""
        severity = alert.get("severity", "INFO")
        symbol = alert.get("symbol", "???")
        exchange = alert.get("exchange", "")
        alert_type = alert.get("type", "UNKNOWN")
        message = alert.get("message", "")

        icon = "\U0001f6a8" if severity == "CRITICAL" else "\u26a0\ufe0f" if severity == "WARNING" else "\u2139\ufe0f"
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
            lines.append("\n\U0001f534 *CERRAR POSICION INMEDIATAMENTE*")
        elif alert_type == "RATE_DROP":
            lines.append("\n\U0001f7e1 Considerar cerrar posicion")
        elif alert_type == "PRE_PAYMENT_UNFAVORABLE":
            lines.append("\n\U0001f7e1 Tasa desfavorable antes del pago")
        elif alert_type == "SL_TP_REVIEW":
            lines.append("\n\U0001f535 *REVISAR STOP LOSS / TAKE PROFIT*")
        elif alert_type == "EXCEPTIONAL_OPPORTUNITY":
            lines.append("\n\u2b50 *OPORTUNIDAD EXCEPCIONAL — Revisar ahora*")
        elif alert_type == "SWITCH_OPPORTUNITY":
            lines.append("\n\U0001f504 *Alternativa superior disponible*")
        elif alert_type == "POSITION_CLOSED":
            lines.append("\n\U0001f7e2 Posicion cerrada")

        return "\n".join(lines)

    def _send_telegram(self, text: str):
        """Send message via Telegram Bot API (HTTPS POST)."""
        url = f"https://api.telegram.org/bot{self.tg_bot_token}/sendMessage"
        payload = json.dumps({
            "chat_id": self.tg_chat_id,
            "text": text,
            "parse_mode": "Markdown",
        }).encode("utf-8")
        log.debug(f"Telegram: chat_id={self.tg_chat_id}, token={self.tg_bot_token[:8]}...")
        req = urllib.request.Request(
            url, data=payload,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=15) as resp:
            status = resp.getcode()
            body = resp.read().decode("utf-8", errors="replace")
            log.debug(f"Telegram response: {status} — {body[:100]}")
            if status != 200:
                raise RuntimeError(f"Telegram API returned {status}: {body}")

    def test_connection(self) -> dict:
        """Test Telegram by sending a test message."""
        self._sync_from_state()
        if not all([self.tg_chat_id, self.tg_bot_token]):
            return {"ok": False, "error": "Configura Bot Token y Chat ID de Telegram"}
        try:
            self._send_telegram("\u2705 Funding Bot — Prueba de Telegram OK")
            return {"ok": True, "error": ""}
        except urllib.error.HTTPError as e:
            body = e.read().decode("utf-8", errors="replace")[:200]
            if e.code == 401:
                return {"ok": False, "error": "Bot Token invalido — verifica tu token de @BotFather"}
            if e.code == 400 and "chat not found" in body.lower():
                return {"ok": False, "error": "Chat ID no encontrado — envia /start al bot primero"}
            return {"ok": False, "error": f"Error HTTP {e.code}: {body}"}
        except urllib.error.URLError as e:
            return {"ok": False, "error": f"Sin conexion a Telegram: {str(e.reason)[:100]}"}
        except Exception as e:
            return {"ok": False, "error": f"Error: {str(e)[:200]}"}
