"""Telegram notifications via Bot API."""
import json
import logging
import time
import urllib.request
import urllib.parse
from datetime import datetime

log = logging.getLogger("bot")


def build_alert_dedup_key(alert: dict) -> str:
    """Single source of truth for alert dedup keys.

    Convention: f"{type}:{user_id}:{symbol}:{exchange}:{bucket}" where bucket
    is funding_ts (int ms or s) for window-bound alerts, or _exc_bucket
    (YYYYMMDD) for daily broadcasts, or empty string when neither applies.
    Always returns a string; never None.
    """
    if alert.get("dedup_key"):
        return str(alert["dedup_key"])
    bucket = alert.get("funding_ts") or alert.get("_exc_bucket") or ""
    return (
        f"{alert.get('type','')}:{alert.get('user_id','')}:"
        f"{alert.get('symbol','')}:{alert.get('exchange','')}:{bucket}"
    )


def valid_telegram_creds(chat_id: str, token: str) -> bool:
    """Cheap format check before hitting api.telegram.org."""
    chat = (chat_id or "").strip()
    tok = (token or "").strip()
    return (
        chat.lstrip("-").isdigit() and len(chat) >= 5
        and ":" in tok and len(tok) >= 20
    )


class EmailNotifier:
    """Telegram notifier (keeps class name for compatibility)."""

    def __init__(self, state_manager, dedup_check=None, dedup_record=None):
        self.state_manager = state_manager
        # In-memory short-window cooldown (per-process). Survives only until
        # restart; the persistent dedup is delegated to dedup_check/record.
        self._sent_cache = {}
        self._cooldown_seconds = 60
        # dedup_check(user_id, dedup_key) -> bool: True if already sent recently.
        # dedup_record(user_id, dedup_key) -> None: persist that it was sent.
        # Both must run inside a Flask app context (caller's responsibility).
        self._dedup_check = dedup_check
        self._dedup_record = dedup_record
        # Per-process log throttle for "invalid creds" warnings, keyed by user_id.
        self._invalid_creds_warned: set = set()
        self._sync_from_state()

    def _sync_from_state(self):
        """Sync Telegram config from state manager (fallback path)."""
        s = self.state_manager.state
        self.enabled = s.get("email_enabled", False)
        self.tg_chat_id = str(s.get("tg_chat_id", "")).strip()
        self.tg_bot_token = str(s.get("tg_bot_token", "")).strip()

        if self.enabled and not all([self.tg_chat_id, self.tg_bot_token]):
            self.enabled = False

    def send_alert(self, alert: dict, chat_id: str = None, token: str = None) -> bool:
        """Send a single alert via Telegram. Returns True if sent.

        If chat_id/token are provided, they override the state-derived
        credentials for this call only (no state mutation, no races across
        users). Otherwise falls back to state.
        """
        if chat_id is None or token is None:
            self._sync_from_state()
            chat_id = self.tg_chat_id
            token = self.tg_bot_token
            enabled = self.enabled
        else:
            chat_id = str(chat_id).strip()
            token = str(token).strip()
            enabled = bool(chat_id and token)

        if not enabled:
            log.warning(f"Telegram disabled (chat_id={'set' if chat_id else 'empty'}, "
                        f"token={'set' if token else 'empty'}), "
                        f"skipping: {alert.get('type')} {alert.get('symbol')}")
            return False

        if not valid_telegram_creds(chat_id, token):
            uid = alert.get("user_id", "")
            if uid not in self._invalid_creds_warned:
                log.warning(f"Telegram: invalid credentials format for user {uid}, "
                            f"chat_id/token rejected without hitting API")
                self._invalid_creds_warned.add(uid)
            return False

        user_id = alert.get("user_id", "")
        alert_key = build_alert_dedup_key(alert)
        now = time.time()

        # Layer 1: persistent dedup across process restarts (DB).
        if self._dedup_check and user_id:
            try:
                if self._dedup_check(user_id, alert_key):
                    log.info(
                        f"Telegram dedup hit (already sent recently) "
                        f"{alert_key}, skipping"
                    )
                    return False
            except Exception as e:
                log.debug(f"dedup_check failed (allowing send): {e}")

        # Layer 2: in-memory cooldown (catches rapid back-to-back ticks
        # before the DB write commits, and works in DB-less mode).
        expiry = self._cooldown_seconds * 10
        expired = [k for k, ts in self._sent_cache.items() if now - ts > expiry]
        for k in expired:
            del self._sent_cache[k]
        if alert_key in self._sent_cache:
            if now - self._sent_cache[alert_key] < self._cooldown_seconds:
                remaining = self._cooldown_seconds - (now - self._sent_cache[alert_key])
                log.info(f"Telegram cooldown for {alert_key}, {remaining:.0f}s left")
                return False

        try:
            log.info(f"Sending Telegram: {alert.get('type')} {alert.get('symbol')}...")
            text = self._format_message(alert)
            self._send_telegram(text, chat_id, token)
            self._sent_cache[alert_key] = now
            if self._dedup_record and user_id:
                try:
                    self._dedup_record(user_id, alert_key)
                except Exception as e:
                    log.debug(f"dedup_record failed: {e}")
            log.info(f"Telegram alert SENT OK: {alert.get('type')} {alert.get('symbol')}")
            return True
        except Exception as e:
            log.error(f"Telegram send FAILED: {e}")
            return False

    def send_alerts(self, alerts: list, chat_id: str = None, token: str = None) -> int:
        """Send multiple alerts, returns count sent.

        If chat_id/token are provided, all alerts are dispatched using those
        credentials directly — no state mutation, safe across concurrent users.
        """
        if chat_id is None or token is None:
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
            if self.send_alert(alert, chat_id=chat_id, token=token):
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
        elif alert_type == "LIQUIDATION_PROXIMITY":
            lines.append("\n\U0001f6a8 *SHORT CERCA DE LIQUIDACION — revisar margen*")
        elif alert_type == "EXCEPTIONAL_OPPORTUNITY":
            lines.append("\n\u2b50 *OPORTUNIDAD EXCEPCIONAL — Revisar ahora*")
        elif alert_type == "SWITCH_OPPORTUNITY":
            lines.append("\n\U0001f504 *Alternativa superior disponible*")
        elif alert_type == "POSITION_CLOSED":
            lines.append("\n\U0001f7e2 Posicion cerrada")

        return "\n".join(lines)

    def _send_telegram(self, text: str, chat_id: str = None, token: str = None):
        """Send message via Telegram Bot API (HTTPS POST).

        Uses provided chat_id/token if given, otherwise falls back to the
        instance attributes synced from state.
        """
        chat_id = chat_id if chat_id is not None else self.tg_chat_id
        token = token if token is not None else self.tg_bot_token
        url = f"https://api.telegram.org/bot{token}/sendMessage"
        payload = json.dumps({
            "chat_id": chat_id,
            "text": text,
            "parse_mode": "Markdown",
        }).encode("utf-8")
        log.debug(f"Telegram: chat_id={chat_id}, token={token[:8]}...")
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
