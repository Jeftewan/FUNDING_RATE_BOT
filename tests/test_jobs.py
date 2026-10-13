"""Integración del runner de jobs: SQLite en memoria + exchange simulado."""
import pytest
from flask import Flask

from core.database import db
from exchanges import trade_executor as te
from tests.test_trade_executor import FakeEx


class _Worker:
    email_notifier = None
    _notified_alerts = {}


@pytest.fixture
def env(monkeypatch):
    app = Flask(__name__)
    app.config["SQLALCHEMY_DATABASE_URI"] = "sqlite://"
    app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False
    db.init_app(app)
    with app.app_context():
        from core import db_models as m
        db.create_all()
        u = m.User(email="t@t.co")
        db.session.add(u)
        db.session.commit()
        db.session.add(m.UserExchangeKey(user_id=u.id, exchange_name="Binance",
                                         api_key_encrypted="k", api_secret_encrypted="s"))
        db.session.commit()
        uid = u.id

    ex = FakeEx()
    monkeypatch.setattr(te, "POLL_INTERVAL", 0)
    monkeypatch.setattr(te, "REPRICE_SECS", 0.01)
    monkeypatch.setattr(te, "SPOT_LIMIT_TIMEOUT", 0.3)
    monkeypatch.setattr(te, "_build_client", lambda *a, **k: ex)
    monkeypatch.setattr(te, "_ensure_markets", lambda c: None)
    monkeypatch.setattr(te.time, "sleep", lambda s: None)
    # Estimación de fees del camino bookkeeping sin red.
    import portfolio.manager as pm
    fees = {"entry_fees": 0.2, "entry_slip": 0.0, "exit_fees": 0.2, "exit_slip": 0.0, "total_cost": 0.4}
    monkeypatch.setattr(pm, "calculate_spot_perp_fees", lambda *a, **k: fees)

    from execution.jobs import JobRunner
    runner = JobRunner(app, _Worker())
    return app, runner, ex, uid


def _run_sync(runner, app, uid, kind, params):
    from core.db_models import ExecutionJob
    with app.app_context():
        job = ExecutionJob(user_id=uid, kind=kind, status="queued", params_json=params)
        db.session.add(job)
        db.session.commit()
        jid = job.id
    runner._run(jid)
    with app.app_context():
        j = ExecutionJob.query.get(jid)
        return j.status, j.result_json, j.position_id, j.error


def test_open_then_close_job(env):
    app, runner, ex, uid = env
    opp = {"mode": "spot_perp", "symbol": "BTC", "exchange": "Binance", "price": 100.0,
           "funding_rate": 0.0001, "interval_hours": 8, "payments_per_day": 3}
    status, res, pos_id, err = _run_sync(runner, app, uid, "open", {
        "symbol": "BTC", "opportunity": opp, "capital": 300, "leverage": 2})
    assert status == "done", err
    from core.db_models import UserPosition, UserHistory
    with app.app_context():
        p = UserPosition.query.get(pos_id)
        assert p.source == "auto" and p.auto_executed
        assert p.spot_qty == pytest.approx(ex.spot_bal["BTC"])
        assert p.perp_qty == pytest.approx(ex.short)
        assert p.earnings_source == "real"
        assert p.entry_fees_real and p.entry_fees_real > 0
        assert p.payments_json[0]["kind"] == "auto_open"

    status, res, _, err = _run_sync(runner, app, uid, "close", {"position_id": pos_id, "symbol": "BTC"})
    assert status == "done", err
    assert ex.short == pytest.approx(0, abs=1e-9)
    with app.app_context():
        p = UserPosition.query.get(pos_id)
        assert p.status == "closed"
        h = UserHistory.query.filter_by(user_id=uid).first()
        assert h and "Auto-cierre job" in (h.notes or "")


def test_failed_open_saves_nothing(env):
    app, runner, ex, uid = env
    ex._multi = True
    opp = {"mode": "spot_perp", "symbol": "BTC", "exchange": "Binance", "price": 100.0}
    status, res, pos_id, err = _run_sync(runner, app, uid, "open", {
        "symbol": "BTC", "opportunity": opp, "capital": 300, "leverage": 2})
    assert status == "failed" and pos_id is None
    assert res.get("code") == "MULTI_ASSETS"


def test_real_capital_from_snapshot(env):
    app, runner, ex, uid = env
    from core.db_persistence import DBPersistence
    with app.app_context():
        dbp = DBPersistence()
        assert dbp.load_user_state(uid)["capital_source"] == "manual"
        dbp.save_account_snapshot(uid, "binance", {"balances": {
            "spot_usdt_free": 120.5, "futures_available": 80.0}})
        st = dbp.load_user_state(uid)
        assert st["capital_source"] == "real"
        assert st["total_capital"] == pytest.approx(200.5)
