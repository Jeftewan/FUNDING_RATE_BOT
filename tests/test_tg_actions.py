"""Botones de Telegram (notifications/tg_actions.py) — SQLite en memoria, Bot API simulada."""
from datetime import datetime, timedelta, timezone

import pytest
from flask import Flask

from core.database import db
from notifications import tg_actions as tga


@pytest.fixture
def tenv(monkeypatch):
    app = Flask(__name__)
    app.config["SQLALCHEMY_DATABASE_URI"] = "sqlite://"
    app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False
    db.init_app(app)
    calls = []
    monkeypatch.setattr(tga, "tg_call", lambda token, method, payload, timeout=15: calls.append((method, payload)) or {})
    import core.encryption as enc
    monkeypatch.setattr(enc, "decrypt_value", lambda v: v)
    with app.app_context():
        from core import db_models as m
        db.create_all()
        u = m.User(email="t@t.co")
        db.session.add(u)
        db.session.commit()
        db.session.add(m.UserConfig(user_id=u.id, tg_chat_id="123456", tg_bot_token_encrypted="1:" + "x" * 30,
                                    tg_actions_enabled=True, tg_webhook_secret_hash=tga.secret_hash("s3cret")))
        db.session.commit()
        uid = u.id
    return app, uid, calls


def _signal(app, uid, ttl=timedelta(hours=1)):
    alert = {"type": "CLOSE_SUGGESTED", "symbol": "ABC", "user_id": uid,
             "action": {"kind": "CLOSE_SUGGESTED", "position_id": 7}}
    with app.app_context():
        tga.attach(uid, alert)
        from core.db_models import UserSignal
        row = UserSignal.query.get(alert["_signal_id"])
        row.expires_at = (datetime.now(timezone.utc) + ttl).replace(tzinfo=None)
        db.session.commit()
    return alert


def _update(sid, act="a", chat=123456):
    return {"callback_query": {"id": "cb1", "data": f"s:{sid}:{act}",
                               "message": {"message_id": 99, "text": "CLOSE_SUGGESTED", "chat": {"id": chat}}}}


def _status(app, sid):
    from core.db_models import UserSignal
    with app.app_context():
        r = UserSignal.query.get(sid)
        return r.status, r.job_id


def test_attach_adds_buttons_and_record_sent(tenv):
    app, uid, _ = tenv
    alert = _signal(app, uid)
    kb = alert["reply_markup"]["inline_keyboard"][0]
    assert kb[0]["callback_data"] == f"s:{alert['_signal_id']}:a" and kb[1]["callback_data"].endswith(":i")
    alert["_tg_message_id"] = 55
    with app.app_context():
        tga.record_sent(alert)
        from core.db_models import UserSignal
        assert UserSignal.query.get(alert["_signal_id"]).tg_message_id == 55


def test_unsent_signal_is_deleted(tenv):
    app, uid, _ = tenv
    alert = _signal(app, uid)                       # sin _tg_message_id: dedup o error de envío
    with app.app_context():
        tga.record_sent(alert)
        from core.db_models import UserSignal
        assert UserSignal.query.get(alert["_signal_id"]) is None


def test_bad_secret_or_foreign_chat_rejected(tenv):
    app, uid, _ = tenv
    sid = _signal(app, uid)["_signal_id"]
    started = []
    with app.app_context():
        assert tga.handle_update(uid, _update(sid), "wrong", lambda *a: started.append(a)) == 403
        assert tga.handle_update(uid, _update(sid, chat=999), "s3cret", lambda *a: started.append(a)) == 403
    assert not started and _status(app, sid)[0] == "pending"


def test_accept_launches_close_once(tenv):
    app, uid, calls = tenv
    sid = _signal(app, uid)["_signal_id"]
    started = []

    def start_close(u, pos_id):
        started.append((u, pos_id))
        return True, "", 42

    with app.app_context():
        assert tga.handle_update(uid, _update(sid), "s3cret", start_close) == 200
        assert tga.handle_update(uid, _update(sid), "s3cret", start_close) == 200    # doble clic
    assert started == [(uid, 7)]
    assert _status(app, sid) == ("accepted", 42)
    answers = [p["text"] for m, p in calls if m == "answerCallbackQuery"]
    assert "job #42" in answers[0] and answers[1] == "Ya se aceptó"


def test_close_failure_marks_failed(tenv):
    app, uid, _ = tenv
    sid = _signal(app, uid)["_signal_id"]
    with app.app_context():
        tga.handle_update(uid, _update(sid), "s3cret", lambda u, p: (False, "Sin API keys", None))
    assert _status(app, sid)[0] == "failed"


def test_expired_and_ignored(tenv):
    app, uid, calls = tenv
    old = _signal(app, uid, ttl=timedelta(minutes=-1))["_signal_id"]
    new = _signal(app, uid)["_signal_id"]
    started = []
    with app.app_context():
        tga.handle_update(uid, _update(old), "s3cret", lambda *a: started.append(a))
        tga.handle_update(uid, _update(new, "i"), "s3cret", lambda *a: started.append(a))
    assert not started
    assert _status(app, old)[0] == "pending" and _status(app, new)[0] == "ignored"
    answers = [p["text"] for m, p in calls if m == "answerCallbackQuery"]
    assert answers == ["La señal venció", "Ignorada"]


def test_rebalance_pause_button(tenv):
    app, uid, _ = tenv
    alert = {"type": "REBALANCED", "symbol": "ABC", "user_id": uid}
    with app.app_context():
        tga.attach(uid, alert)
        assert alert["reply_markup"]["inline_keyboard"][0][0]["callback_data"] == "r:off"
        upd = {"callback_query": {"id": "c", "data": "r:off", "message": {"message_id": 1, "chat": {"id": 123456}}}}
        assert tga.handle_update(uid, upd, "s3cret", None) == 200
        from core.db_models import UserConfig
        assert UserConfig.query.filter_by(user_id=uid).first().rebalance_paused is True


def test_actions_disabled_rejects(tenv):
    app, uid, _ = tenv
    sid = _signal(app, uid)["_signal_id"]
    with app.app_context():
        from core.db_models import UserConfig
        UserConfig.query.filter_by(user_id=uid).first().tg_actions_enabled = False
        db.session.commit()
        assert tga.handle_update(uid, _update(sid), "s3cret", lambda *a: None) == 403


def test_send_telegram_passes_markup_and_returns_message_id(monkeypatch):
    import json
    from notifications import email as em

    class Resp:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def getcode(self):
            return 200

        def read(self):
            return json.dumps({"ok": True, "result": {"message_id": 77}}).encode()

    sent = {}
    monkeypatch.setattr(em.urllib.request, "urlopen", lambda req, timeout=15: sent.update(body=req.data) or Resp())
    n = em.EmailNotifier.__new__(em.EmailNotifier)
    mid = n._send_telegram("hola", "123456", "1:" + "x" * 30, reply_markup={"inline_keyboard": []})
    assert mid == 77 and json.loads(sent["body"])["reply_markup"] == {"inline_keyboard": []}
