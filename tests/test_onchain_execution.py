"""Ejecución on-chain: anti-sándwich, troceo, espera de basis y orden límite (sin red)."""
import threading
import time
from unittest.mock import MagicMock

import pytest

from core.database import db
from exchanges import dex_aggregator as da
from exchanges import onchain_bsc as ob
from exchanges import trade_executor as te
from execution import jobs as J
from tests.test_jobs import env  # noqa: F401  (fixture)
from tests.test_onchain_bsc import CREDS, OPP, TOKEN, WALLET, chain, perp  # noqa: F401  (fixtures)

TX = {"to": "0x5dbde81fce337ff4bcaaee4ca3466c00aecae274", "data": "0x", "gasPrice": 50_000_000}
TOK = TOKEN["address"].lower()
POS = {"mode": "spot_perp", "symbol": "XYZ", "exchange": "Binance", "spot_venue": "bsc",
       "spot_qty": 100.0, "perp_qty": 100.0, "entry_price": 2.0, "spot_entry_price": 2.0}


# ── Fase 1: canal protegido ─────────────────────────────────────────────
@pytest.fixture
def signer(monkeypatch):
    monkeypatch.setattr(ob, "_nonce", lambda a: 0)
    monkeypatch.setattr(ob, "_gas_limit", lambda s, tx, hint=None: 60_000)
    monkeypatch.setattr(ob.time, "sleep", lambda s: None)


def test_no_public_fallback_when_private_rpc_fails(signer, monkeypatch):
    calls = []

    def rpc(method, params, url=None):
        calls.append((method, url))
        if method == "eth_sendRawTransaction":
            raise RuntimeError("private down")
        return None
    monkeypatch.setattr(ob, "_rpc", rpc)
    monkeypatch.setenv("BSC_PRIVATE_RPC_URL", "https://private.example")
    with pytest.raises(ob.BroadcastError):
        ob._sign_and_send(WALLET, TX, None, allow_public=False)
    assert ("eth_sendRawTransaction", None) not in calls


def test_naked_exit_may_fall_back_to_public(signer, monkeypatch):
    urls = []

    def rpc(method, params, url=None):
        urls.append(url)
        if url:
            raise RuntimeError("private down")
        return "0xpublic"
    monkeypatch.setattr(ob, "_rpc", rpc)
    monkeypatch.setenv("BSC_PRIVATE_RPC_URL", "https://private.example")
    meta = {}
    assert ob._sign_and_send(WALLET, TX, None, allow_public=True, meta=meta) == "0xpublic"
    assert meta["via"] == "public_rpc"


def test_abort_without_any_protected_channel(signer, monkeypatch):
    monkeypatch.setattr(ob, "_rpc", MagicMock(side_effect=AssertionError("no debe transmitir")))
    monkeypatch.delenv("BSC_PRIVATE_RPC_URL", raising=False)
    with pytest.raises(ob.BroadcastError):
        ob._sign_and_send(WALLET, TX, None, allow_public=False)


def test_private_rpc_error_but_mined_counts_as_sent(signer, monkeypatch):
    def rpc(method, params, url=None):
        if method == "eth_sendRawTransaction":
            raise RuntimeError("timeout")
        return {"status": "0x1"}
    monkeypatch.setattr(ob, "_rpc", rpc)
    monkeypatch.setenv("BSC_PRIVATE_RPC_URL", "https://private.example")
    assert ob._sign_and_send(WALLET, TX, None, allow_public=False).startswith("0x")


def _leg(chain, perp, cls=ob.BscSpotLeg, ctx=None, **kw):
    return cls(WALLET, TOKEN, ctx or te.ExecContext(), perp, "XYZ/USDT:USDT", aggregator=chain, **kw)


def test_swap_recotizes_after_revert_then_aborts(chain, perp, monkeypatch):
    monkeypatch.setattr(ob, "wait_receipt", lambda h, timeout=0: {"status": "0x0", "gasUsed": "0x0"})
    monkeypatch.setattr(ob.time, "sleep", lambda s: None)
    chain.allow[ob.USDT.lower()] = 10 ** 40
    spy = MagicMock(side_effect=chain.swap_tx)
    chain.swap_tx = spy
    leg = _leg(chain, perp)
    with pytest.raises(ob.SwapReverted):
        leg._swap_retry(ob.USDT, 18, TOK, 18, 10.0)
    assert spy.call_count == ob.SWAP_RETRIES + 1
    assert leg.swaps == []


def test_default_tolerance_is_tight():
    assert ob.DEFAULT_SLIPPAGE == 0.002


def test_kyber_retries_forbidden_then_succeeds(monkeypatch):
    bad = MagicMock(status_code=403, headers={}, json=lambda: {"message": "Forbidden"})
    good = MagicMock(status_code=200, headers={}, json=lambda: {"code": 0, "data": {"x": 1}})
    req = MagicMock(side_effect=[bad, good])
    monkeypatch.setattr(da.requests, "request", req)
    monkeypatch.setattr(da.time, "sleep", lambda s: None)
    assert da.KyberSwapAggregator()._req("GET", "/routes") == {"x": 1}
    assert req.call_count == 2


# ── Fase 2: costo previo y troceo ───────────────────────────────────────
def test_suggest_slices_from_measured_impact(chain, perp):
    s = _leg(chain, perp).suggest_slices(100, "sell")       # el fake cobra 0.3 % vs mid
    assert s["slices"] == 3 and s["duration_s"] == 3 * ob.SLICE_GAP and s["suggest"]


def test_suggest_slices_without_measure_does_not_split(chain, perp):
    chain.quote = MagicMock(side_effect=RuntimeError("sin ruta"))
    assert _leg(chain, perp).suggest_slices(100, "sell")["slices"] == 1


def test_dry_run_reports_round_trip_cost(chain, perp):
    fac = lambda pc, ps, ctx: _leg(chain, pc, ctx=ctx)      # noqa: E731
    opp = {**OPP, "model_prediction": 36.5}
    res = te.execute_open(CREDS, opp, 200, 1, dry_run=True, spot_leg_factory=fac)
    cost = res["execution_cost"]
    assert cost["rt_cost_pct"] == pytest.approx(0.7, abs=0.02)
    assert cost["payback_days"] == pytest.approx(7.0, abs=0.3)
    assert res["twap"]["slices"] == 3


def test_open_splits_automatically_and_logs_swaps(chain, perp, monkeypatch):
    monkeypatch.setattr(te.time, "sleep", lambda s: None)
    fac = lambda pc, ps, ctx: _leg(chain, pc, ctx=ctx)      # noqa: E731
    res = te.execute_open(CREDS, OPP, capital=200, leverage=1, spot_leg_factory=fac)
    assert res["ok"], res
    assert len(res["swaps"]) == 3 and res["entry_mode"] == "onchain_swap"
    assert all(s["perp_ref"] for s in res["swaps"])
    assert perp.short == pytest.approx(res["perp_qty"])


def test_payback_uses_model_then_current_rate():
    assert te.payback_days(1.0, {"model_prediction": 36.5}) == pytest.approx(10.0)
    assert te.payback_days(1.0, {"daily_income_per_1000": 1.0}) == pytest.approx(10.0)
    assert te.payback_days(None, {"model_prediction": 36.5}) is None
    assert te.payback_days(1.0, {}) is None


def test_basis_probe_measures_executable_basis(chain, perp):
    leg = _leg(chain, perp)
    buy = te.BasisProbe(perp, "XYZ/USDT:USDT", leg, "buy", 100, 2).read()
    assert buy["basis_pct"] == pytest.approx((2.0 - 2 / 0.997) / (2 / 0.997) * 100, abs=1e-3)
    sell = te.BasisProbe(perp, "XYZ/USDT:USDT", leg, "sell", 100, 1).read()
    assert sell["basis_pct"] == pytest.approx((2 * 0.997 - 2.002) / 2.002 * 100, abs=1e-3)


# ── Fase 3: espera de basis ─────────────────────────────────────────────
class Probe:
    def __init__(self, *values):
        self.values = list(values)

    def read(self):
        v = self.values.pop(0) if len(self.values) > 1 else self.values[0]
        return {"basis_pct": v, "px": 2.0, "perp": 2.0}


def _armed(app, uid, kind="open", position_id=None, **wait):
    from core.db_models import ExecutionJob
    w = {"side": "buy", "theta": 0.25, "deadline": time.time() + 3600, **wait}
    with app.app_context():
        job = ExecutionJob(user_id=uid, kind=kind, status="armed", position_id=position_id,
                           params_json={"symbol": "XYZ", "wait": w, "position_id": position_id,
                                        "token": TOKEN, "capital": 100, "leverage": 1,
                                        "opportunity": OPP})
        db.session.add(job)
        db.session.commit()
        return job.id


def _status(app, jid):
    from core.db_models import ExecutionJob
    with app.app_context():
        job = ExecutionJob.query.get(jid)
        return job.status, job.params_json, job.error


def _pos(app, uid, **fields):
    from core.db_models import UserPosition
    with app.app_context():
        row = UserPosition(user_id=uid, symbol="XYZ", exchange="Binance", mode="spot_perp", status="active",
                           source="auto", spot_venue="bsc", spot_qty=100.0, perp_qty=100.0, entry_time=1,
                           **fields)
        db.session.add(row)
        db.session.commit()
        return row.id


def test_armed_fires_only_when_basis_reaches_threshold(env, monkeypatch):  # noqa: F811
    app, runner, _, uid = env
    jid = _armed(app, uid)
    monkeypatch.setattr(runner, "_make_probe", lambda job: Probe(-0.5, 0.1, 0.3))
    ran = MagicMock()
    monkeypatch.setattr(runner, "_run", ran)
    runner._watch_armed(jid)
    status, params, _ = _status(app, jid)
    assert status == "queued" and params.get("fired_at")
    assert params["fire_read"]["basis_pct"] == 0.3


def test_armed_expires_without_entering(env, monkeypatch):  # noqa: F811
    app, runner, _, uid = env
    jid = _armed(app, uid, deadline=time.time() - 1)
    runner._watch_armed(jid)
    status, _, error = _status(app, jid)
    assert status == "expired" and "no se abrió" in error


def test_armed_cancelled_by_user(env, monkeypatch):  # noqa: F811
    from core.db_models import ExecutionJob
    app, runner, _, uid = env
    jid = _armed(app, uid)
    with app.app_context():
        ExecutionJob.query.get(jid).cancel_requested = True
        db.session.commit()
    runner._watch_armed(jid)
    assert _status(app, jid)[0] == "cancelled"


def test_armed_close_stops_if_position_gone(env):  # noqa: F811
    app, runner, _, uid = env
    jid = _armed(app, uid, kind="close", position_id=999999)
    runner._watch_armed(jid)
    assert _status(app, jid)[0] == "cancelled"


def test_force_fires_without_probe_and_uses_swap(env, monkeypatch):  # noqa: F811
    app, runner, _, uid = env
    jid = _armed(app, uid, force=True)
    monkeypatch.setattr(runner, "_make_probe", MagicMock(side_effect=AssertionError))
    monkeypatch.setattr(runner, "_run", MagicMock())
    runner._watch_armed(jid)
    status, params, _ = _status(app, jid)
    assert status == "queued" and params.get("fired_at")


def test_fire_waits_while_user_has_running_job(env):  # noqa: F811
    from core.db_models import ExecutionJob
    app, runner, _, uid = env
    jid = _armed(app, uid)
    with app.app_context():
        db.session.add(ExecutionJob(user_id=uid, kind="open", status="running", params_json={}))
        db.session.commit()
    assert runner._fire(runner._load(jid)) is False
    assert _status(app, jid)[0] == "armed"


def test_armed_does_not_block_new_jobs_but_blocks_double_close(env):  # noqa: F811
    app, runner, _, uid = env
    pid = _pos(app, uid)
    _armed(app, uid, kind="close", position_id=pid)
    with app.app_context():
        assert runner.active_job(uid) is None
        assert runner.active_job_for_position(uid, pid) is None
        assert runner.active_job_for_position(uid, pid, include_armed=True) is not None


def test_watchdog_close_cancels_armed(env):  # noqa: F811
    app, runner, _, uid = env
    pid = _pos(app, uid)
    jid = _armed(app, uid, kind="close", position_id=pid)
    runner.cancel_armed_for_position(uid, pid, "Seguro disparado")
    status, _, error = _status(app, jid)
    assert status == "cancelled" and "Seguro" in error


def test_recover_resumes_armed_jobs(env, monkeypatch):  # noqa: F811
    app, runner, _, uid = env
    jid = _armed(app, uid)
    seen = threading.Event()
    monkeypatch.setattr(runner, "_watch_armed", lambda j: seen.set() if j == jid else None)
    runner._recover()
    assert seen.wait(2)


def test_entry_time_is_fire_time(env, monkeypatch):  # noqa: F811
    from core.db_models import UserPosition
    app, runner, ex, uid = env
    jid = _armed(app, uid)
    opp = {"mode": "spot_perp", "symbol": "BTC", "exchange": "Binance", "price": 100.0,
           "funding_rate": 0.0001, "interval_hours": 8, "payments_per_day": 3}
    from core.db_models import ExecutionJob
    with app.app_context():
        job = ExecutionJob.query.get(jid)
        job.params_json = {"symbol": "BTC", "opportunity": opp, "capital": 300, "leverage": 2, "fired_at": 1234.5}
        job.status = "queued"
        db.session.commit()
    runner._run(jid)
    with app.app_context():
        row = UserPosition.query.filter_by(symbol="BTC").first()
        assert row.entry_time == 1234500


# ── ARIA #56: token Alpha que pasa a offline con la posición abierta ────
ARIA = {"symbol": "ARIA", "chainId": "56", "contractAddress": "0x5d3a12c42e5372b2cc3264ab3cdcf660a1555238",
        "decimals": 18, "liquidity": "2054366.5", "offline": True, "fullyDelisted": False,
        "alphaId": "ALPHA_332", "name": "AriaAI"}


def _alpha_list(monkeypatch, rows):
    class R:
        def raise_for_status(self):
            pass

        def json(self):
            return {"data": rows}
    monkeypatch.setattr(ob, "_alpha_cache", {"ts": 0.0, "by_symbol": {}, "any_by_symbol": {}})
    monkeypatch.setattr(ob.requests, "get", lambda *a, **k: R())


def test_offline_alpha_token_only_for_exit(monkeypatch):
    _alpha_list(monkeypatch, [ARIA])
    assert ob.alpha_token("ARIA") is None                     # abrir: no
    t = ob.alpha_token("ARIA", for_exit=True)                 # cerrar/vigilar: sí
    assert t["address"] == ARIA["contractAddress"] and t["decimals"] == 18


def test_position_token_falls_back_to_open_job(env, monkeypatch):  # noqa: F811
    from core.db_models import ExecutionJob
    app, runner, ex, uid = env
    _alpha_list(monkeypatch, [])                              # ya ni sale en la lista
    with app.app_context():
        job = ExecutionJob(user_id=uid, kind="open", status="done", params_json={"token": TOKEN})
        db.session.add(job)
        db.session.commit()
        assert J.position_token({"symbol": "XYZ", "job_id": job.id})["address"] == TOKEN["address"]
        assert J.position_token({"symbol": "XYZ"}) is None


def test_close_job_after_sl_resolves_offline_token(env, monkeypatch):  # noqa: F811
    """El vigilante lanza el cierre con token {}: antes abortaba con 'token Alpha BSC no disponible'."""
    from tests.test_jobs import _run_sync
    app, runner, ex, uid = env
    _alpha_list(monkeypatch, [{**ARIA, "symbol": "XYZ", "contractAddress": TOKEN["address"]}])
    pos_id = _pos(app, uid)
    seen = {}

    def factory(uid_, token):
        seen["token"] = token
        return lambda *a: None
    monkeypatch.setattr(runner, "_swap_leg_factory", factory)
    monkeypatch.setattr(runner, "_alert", lambda *a, **k: None)
    monkeypatch.setattr(runner, "_restore_protection", lambda *a, **k: None)
    monkeypatch.setattr(te, "execute_close", lambda *a, **k: {"ok": False, "msg": "stop"})
    _run_sync(runner, app, uid, "close", {"position_id": pos_id, "symbol": "XYZ", "token": {},
                                          "twap": {"slices": 1, "duration_s": 0}, "trigger": "protection"})
    assert seen["token"]["address"] == TOKEN["address"]
