"""Acciones por Telegram: botones inline en alertas accionables y su webhook.

Cada usuario tiene su propio bot (token en user_configs). Con `tg_actions_enabled` se registra
un webhook del bot del usuario en PUBLIC_BASE_URL/api/tg/hook/<uid> con un secret_token
aleatorio (se guarda solo su sha256). Al pulsar un botón, Telegram envía un callback_query:
se valida el secreto y el chat, la señal pasa pending → accepted de forma atómica (doble clic =
una sola acción) y se lanza el mismo job que el botón del dashboard (con la espera de basis y
la guarda de disparo existentes).

Señales accionables (tabla user_signals): CLOSE_SUGGESTED → "Cerrar (espera basis ≥ 0)".
Las reglas EXIT_NOW/ENTER_NOW del piloto LLM no pasaron el backtest
(reports/signal_rules_study_20261118.md). Botón "Pausar rebalanceo" en REBALANCED/REBALANCE_FAILED.
"""
import hashlib
import hmac
import json
import logging
import os
import secrets
import urllib.request
from datetime import datetime, timedelta, timezone

log = logging.getLogger("bot.tg_actions")

SIGNAL_TTL = {"CLOSE_SUGGESTED": timedelta(hours=8)}
DEFAULT_TTL = timedelta(minutes=30)
ACCEPT_LABEL = {"CLOSE_SUGGESTED": "Cerrar (espera basis ≥ 0)"}
REBALANCE_TYPES = ("REBALANCED", "REBALANCE_FAILED")


def public_base_url() -> str:
    return os.environ.get("PUBLIC_BASE_URL", "https://basyo.app").rstrip("/")


def _now():
    return datetime.now(timezone.utc)


def _aware(dt):
    """expires_at se guarda en UTC sin zona (comparable igual en Postgres y SQLite)."""
    return dt if dt is None or dt.tzinfo else dt.replace(tzinfo=timezone.utc)


# ── Bot API ────────────────────────────────────────────────────────────
def tg_call(token: str, method: str, payload: dict, timeout=15) -> dict:
    """POST JSON a la Bot API. Devuelve `result` o lanza RuntimeError."""
    req = urllib.request.Request(f"https://api.telegram.org/bot{token}/{method}",
                                 data=json.dumps(payload).encode("utf-8"),
                                 headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        body = json.loads(resp.read().decode("utf-8", errors="replace") or "{}")
    if not body.get("ok"):
        raise RuntimeError(f"Telegram {method}: {body.get('description', 'error')}")
    return body.get("result")


def secret_hash(secret: str) -> str:
    return hashlib.sha256((secret or "").encode("utf-8")).hexdigest()


def register_webhook(token: str, user_id: int) -> str:
    """setWebhook del bot del usuario → devuelve el hash del secret_token para guardar."""
    secret = secrets.token_urlsafe(32)
    tg_call(token, "setWebhook", {"url": f"{public_base_url()}/api/tg/hook/{int(user_id)}",
                                  "secret_token": secret, "allowed_updates": ["callback_query"],
                                  "drop_pending_updates": True})
    return secret_hash(secret)


def unregister_webhook(token: str):
    try:
        tg_call(token, "deleteWebhook", {"drop_pending_updates": True})
    except Exception as e:
        log.warning(f"deleteWebhook: {e}")


def sync_webhook(user_id: int) -> dict:
    """Registra o quita el webhook según user_configs (llamar en app context)."""
    from core.database import db
    from core.db_models import UserConfig
    from core.encryption import decrypt_value
    cfg = UserConfig.query.filter_by(user_id=user_id).first()
    if not cfg:
        return {"ok": False, "msg": "Sin configuración"}
    token = decrypt_value(cfg.tg_bot_token_encrypted) if cfg.tg_bot_token_encrypted else ""
    if not token:
        return {"ok": False, "msg": "Configura el Bot Token de Telegram"}
    if not cfg.tg_actions_enabled:
        if cfg.tg_webhook_secret_hash:
            unregister_webhook(token)
            cfg.tg_webhook_secret_hash = ""
            db.session.commit()
        return {"ok": True, "msg": "Botones desactivados"}
    try:
        cfg.tg_webhook_secret_hash = register_webhook(token, user_id)
    except Exception as e:
        return {"ok": False, "msg": f"No se pudo registrar el webhook: {e}"}
    db.session.commit()
    return {"ok": True, "msg": "Botones de Telegram activos"}


# ── Señales con botones ────────────────────────────────────────────────
def signal_markup(signal_id: int, kind: str) -> dict:
    return {"inline_keyboard": [[
        {"text": ACCEPT_LABEL.get(kind, "Ejecutar"), "callback_data": f"s:{signal_id}:a"},
        {"text": "Ignorar", "callback_data": f"s:{signal_id}:i"}]]}


def rebalance_markup(paused: bool) -> dict:
    return {"inline_keyboard": [[
        {"text": "Reanudar rebalanceo" if paused else "Pausar rebalanceo",
         "callback_data": "r:on" if paused else "r:off"}]]}


def attach(user_id: int, alert: dict) -> None:
    """Antes de enviar: crea la fila user_signals y pone los botones en la alerta (app context).
    alert["action"] = {"kind", "position_id"?, "payload"?}."""
    from core.database import db
    from core.db_models import UserSignal
    if alert.get("type") in REBALANCE_TYPES:
        alert["reply_markup"] = rebalance_markup(False)
        return
    act = alert.get("action") or {}
    kind = act.get("kind")
    if not kind:
        return
    row = UserSignal(user_id=user_id, kind=kind, symbol=alert.get("symbol", "") or "",
                     position_id=act.get("position_id"), payload_json=act.get("payload") or {},
                     status="pending", expires_at=(_now() + SIGNAL_TTL.get(kind, DEFAULT_TTL)).replace(tzinfo=None))
    db.session.add(row)
    db.session.commit()
    alert["_signal_id"] = row.id
    alert["reply_markup"] = signal_markup(row.id, kind)


def record_sent(alert: dict) -> None:
    """Después de enviar: guarda el message_id; si no salió (dedup, error) borra la señal (app context)."""
    sid, mid = alert.get("_signal_id"), alert.get("_tg_message_id")
    if not sid:
        return
    from core.database import db
    from core.db_models import UserSignal
    row = UserSignal.query.get(sid)
    if row:
        if mid:
            row.tg_message_id = int(mid)
        else:
            db.session.delete(row)
        db.session.commit()


# ── Webhook ────────────────────────────────────────────────────────────
def verify(cfg, header_secret: str, chat_id) -> bool:
    """Secreto del webhook y chat del usuario."""
    if not cfg or not cfg.tg_actions_enabled or not cfg.tg_webhook_secret_hash:
        return False
    if not hmac.compare_digest(secret_hash(header_secret or ""), cfg.tg_webhook_secret_hash):
        return False
    return str(chat_id or "").strip() == str(cfg.tg_chat_id or "").strip()


def _claim(signal_id: int, user_id: int, new_status: str) -> bool:
    """pending → new_status solo si sigue pendiente y no venció (atómico)."""
    from core.database import db
    from core.db_models import UserSignal
    n = (UserSignal.query
         .filter(UserSignal.id == signal_id, UserSignal.user_id == user_id,
                 UserSignal.status == "pending", UserSignal.expires_at > _now().replace(tzinfo=None))
         .update({"status": new_status, "decided_at": _now()}, synchronize_session=False))
    db.session.commit()
    return n == 1


def _finish(signal_id, **fields):
    from core.database import db
    from core.db_models import UserSignal
    row = UserSignal.query.get(signal_id)
    if row:
        for k, v in fields.items():
            setattr(row, k, v)
        db.session.commit()


def handle_update(user_id: int, update: dict, header_secret: str, start_close) -> int:
    """Procesa un update del webhook (app context). start_close(uid, pos_id) → (ok, msg, job_id).
    Devuelve el código HTTP (403 si el secreto no corresponde; 200 en el resto)."""
    from core.database import db
    from core.db_models import UserConfig, UserSignal
    from core.encryption import decrypt_value
    cfg = UserConfig.query.filter_by(user_id=user_id).first()
    cb = (update or {}).get("callback_query") or {}
    msg = cb.get("message") or {}
    chat_id = (msg.get("chat") or {}).get("id")
    if not verify(cfg, header_secret, chat_id):
        log.warning(f"tg hook user={user_id}: secreto o chat inválido")
        return 403
    token = decrypt_value(cfg.tg_bot_token_encrypted) if cfg.tg_bot_token_encrypted else ""
    data = str(cb.get("data") or "")
    reply = "Acción no reconocida"
    note = None
    try:
        if data in ("r:off", "r:on"):
            paused = data == "r:off"
            cfg.rebalance_paused = paused
            db.session.commit()
            reply = "Rebalanceo pausado" if paused else "Rebalanceo reanudado"
            _safe(token, "editMessageReplyMarkup", {"chat_id": chat_id, "message_id": msg.get("message_id"),
                                                    "reply_markup": rebalance_markup(paused)})
        elif data.startswith("s:"):
            _, sid, act = (data.split(":") + ["", ""])[:3]
            sid = int(sid)
            row = UserSignal.query.filter_by(id=sid, user_id=user_id).first()
            if not row:
                reply = "Señal no encontrada"
            elif act == "i":
                reply = "Ignorada" if _claim(sid, user_id, "ignored") else _stale_text(row)
                note = "⏸ Ignorada" if reply == "Ignorada" else None
            elif act == "a":
                if not _claim(sid, user_id, "accepted"):
                    reply = _stale_text(row)
                elif row.kind == "CLOSE_SUGGESTED" and row.position_id:
                    ok, text, jid = start_close(user_id, int(row.position_id))
                    if ok:
                        _finish(sid, job_id=jid)
                        reply = f"Cierre en curso (job #{jid})"
                        note = f"✅ Cierre lanzado: job #{jid} (espera basis de venta ≥ 0)"
                    else:
                        _finish(sid, status="failed", error=text)
                        reply = f"No se pudo cerrar: {text}"[:190]
                        note = f"❌ {text}"
                else:
                    _finish(sid, status="failed", error="tipo de señal sin acción")
                    reply = "Esta señal no tiene acción"
    except Exception as e:
        log.error(f"tg hook user={user_id}: {e}")
        reply = "Error procesando la acción"
    _safe(token, "answerCallbackQuery", {"callback_query_id": cb.get("id"), "text": reply[:190]})
    if note and msg.get("message_id"):
        _safe(token, "editMessageText", {"chat_id": chat_id, "message_id": msg["message_id"],
                                         "text": f"{msg.get('text', '')}\n\n{note}"[:4000]})
    return 200


def _stale_text(row) -> str:
    if row.status == "pending" and _aware(row.expires_at) and _aware(row.expires_at) <= _now():
        return "La señal venció"
    return {"accepted": "Ya se aceptó", "ignored": "Ya se ignoró", "failed": "Ya falló"}.get(row.status, "Ya decidida")


def _safe(token, method, payload):
    if not token:
        return None
    try:
        return tg_call(token, method, payload)
    except Exception as e:
        log.warning(f"tg {method}: {e}")
        return None
