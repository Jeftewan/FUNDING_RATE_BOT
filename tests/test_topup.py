"""«Completar apertura» (BULLA #60: la apertura quedó en 1 de 3 tramos): el job suma las
piernas nuevas a la posición existente y re-coloca los seguros sobre el total."""
import pytest

from core.database import db
from tests.test_exec_safety import SafeEx, _env_with, _open
from tests.test_jobs import _run_sync, env  # noqa: F401 (fixture)
from tests.test_protection import OPP as JOB_OPP


def _topup_params(pos_id, capital=150):
    return {"symbol": "BTC", "opportunity": JOB_OPP, "capital": capital, "leverage": 2,
            "topup_of": pos_id, "position_id": pos_id}


def test_topup_merges_into_position_and_replaces_protection(env, monkeypatch):
    ex = SafeEx()
    ex.liq = 148.0
    app, runner, uid = _env_with(monkeypatch, env, ex)
    pos_id = _open(runner, app, uid)
    from core.db_models import UserPosition
    with app.app_context():
        p0 = UserPosition.query.get(pos_id)
        sq0, pq0, cap0 = p0.spot_qty, p0.perp_qty, p0.capital_used
        fees0, spe0 = p0.entry_fees_real, p0.spot_entry_price
        old_sl = p0.protection_json["perp"]["id"]

    status, res, got_pos, err = _run_sync(runner, app, uid, "open", _topup_params(pos_id))
    assert status == "done", err
    assert got_pos == pos_id
    with app.app_context():
        assert UserPosition.query.filter_by(user_id=uid, status="active").count() == 1
        p = UserPosition.query.get(pos_id)
        assert p.spot_qty == pytest.approx(sq0 + res["spot_qty"])
        assert p.perp_qty == pytest.approx(pq0 + res["perp_qty"])
        assert p.perp_qty == pytest.approx(ex.short)
        assert p.capital_used == pytest.approx(cap0 + res["capital_used"], rel=1e-6)
        assert p.entry_fees_real == pytest.approx(fees0 + res["entry_fees_usd"])
        assert p.spot_entry_price == pytest.approx(spe0, rel=0.01)   # mismo precio → promedio igual
        assert p.payments_json[-1]["kind"] == "topup"
        prot = p.protection_json
        assert prot["status"] == "active"
        assert prot["perp"]["id"] != old_sl                     # SL nuevo sobre el total
        assert prot["spot"]["qty"] == pytest.approx(p.spot_qty)  # venta límite cubre todo
    assert ex.algo[old_sl]["status"] in ("canceled", "cancelled")


def test_topup_skipped_when_position_already_closed(env, monkeypatch):
    ex = SafeEx()
    ex.liq = 148.0
    app, runner, uid = _env_with(monkeypatch, env, ex)
    pos_id = _open(runner, app, uid)
    from core.db_models import UserPosition
    with app.app_context():
        UserPosition.query.get(pos_id).status = "closed"
        db.session.commit()
    short0 = ex.short
    status, _, _, err = _run_sync(runner, app, uid, "open", _topup_params(pos_id))
    assert status == "cancelled", err
    assert ex.short == short0                                   # no operó


def test_pos_dict_exposes_job_id(env, monkeypatch):
    # position_token() cae al token del job de apertura: necesita job_id en el dict.
    ex = SafeEx()
    ex.liq = 148.0
    app, runner, uid = _env_with(monkeypatch, env, ex)
    pos_id = _open(runner, app, uid)
    assert runner._load_pos(pos_id)["job_id"]
