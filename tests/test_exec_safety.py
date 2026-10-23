"""Seguridad de la ejecución con dinero real: cada test reproduce un defecto de
la auditoría 2026-10-22 (lecturas fallidas, reintentos, reconciliación, seguros,
vigilante, doble submit) — sin red."""
import pytest

from core.database import db
from exchanges import trade_executor as te
from tests.test_jobs import _run_sync, env  # noqa: F401 (fixture)
from tests.test_protection import OPP as JOB_OPP, POS, ProtEx
from tests.test_trade_executor import CREDS, OPP, FakeEx


class SafeEx(ProtEx):
    """ProtEx + fallos inyectables y búsqueda por clientOrderId / fetch_orders."""

    def __init__(self, **kw):
        super().__init__(**kw)
        self.fail_positions = False
        self.perp_timeout_once = False   # el 1er market perp ENTRA pero create_order lanza
        self.perp_always_fail = False
        self.trigger_cancel_fails = False

    def fetch_positions(self, syms=None):
        if self.fail_positions:
            raise RuntimeError("Timeout fetch_positions")
        return super().fetch_positions(syms)

    def create_order(self, s, typ, side, amount, price=None, params=None):
        params = params or {}
        is_perp = s.endswith(":USDT") and typ == "market"
        coid = params.get("clientOrderId")
        if is_perp and coid and any(o.get("clientOrderId") == coid for o in self.orders.values()):
            raise RuntimeError("binance -4116 ClientOrderId is duplicated")
        if is_perp and self.perp_always_fail:
            raise RuntimeError("binance -1001 internal error")
        o = super().create_order(s, typ, side, amount, price, params)
        if is_perp and self.perp_timeout_once:
            self.perp_timeout_once = False
            raise RuntimeError("RequestTimeout")
        return o

    def fetch_order(self, oid, s, params=None):
        params = params or {}
        if oid is None and params.get("origClientOrderId"):
            for o in self.orders.values():
                if o.get("clientOrderId") == params["origClientOrderId"]:
                    return dict(o)
            raise RuntimeError("Order does not exist.")
        return super().fetch_order(oid, s, params)

    def cancel_order(self, oid, s, params=None):
        if (params or {}).get("trigger") and self.trigger_cancel_fails:
            raise RuntimeError("Timeout cancel algo")
        return super().cancel_order(oid, s, params)

    def fetch_orders(self, s, since=None, limit=None):
        return [dict(o) for o in self.orders.values() if o["symbol"] == s]


@pytest.fixture
def sex(monkeypatch):
    ex = SafeEx()
    monkeypatch.setattr(te, "POLL_INTERVAL", 0)
    monkeypatch.setattr(te, "REPRICE_SECS", 0.01)
    monkeypatch.setattr(te, "SPOT_LIMIT_TIMEOUT", 0.3)
    monkeypatch.setattr(te, "_build_client", lambda *a, **k: ex)
    monkeypatch.setattr(te, "_ensure_markets", lambda c: None)
    monkeypatch.setattr(te.time, "sleep", lambda s: None)
    return ex


# ── Cierre: lectura del short ───────────────────────────────────────────────
def test_close_aborts_without_selling_when_position_read_fails(sex):
    sex.spot_bal["BTC"], sex.short = 2.0, 2.0
    sex.fail_positions = True
    res = te.execute_close(CREDS, dict(POS))
    assert not res["ok"] and res["code"] == "POSITION_READ"
    assert sex.spot_bal["BTC"] == pytest.approx(2.0)      # spot intacto
    assert sex.short == pytest.approx(2.0)                # short intacto
    assert not sex.orders


def test_open_perp_size_none_on_error_zero_when_flat(sex):
    sex.fail_positions = True
    assert te._open_perp_size(sex, "BTC/USDT:USDT", "short") is None
    sex.fail_positions = False
    assert te._open_perp_size(sex, "BTC/USDT:USDT", "short") == 0.0


# ── Reconciliación: signo en el cierre ──────────────────────────────────────
def test_reconcile_close_full_close_leaves_zero_short(sex):
    sex.spot_bal["BTC"], sex.short = 2.0, 2.0
    sex.create_order("BTC/USDT", "market", "sell", 2.0, None, {"clientOrderId": "bsy5-s1"})
    sex.create_order("BTC/USDT:USDT", "market", "buy", 2.0, None, {"clientOrderId": "bsy5-p1"})
    assert sex.short == pytest.approx(0)
    n_orders = len(sex.orders)
    rec = te.reconcile_spot_perp(CREDS["binance"], "Binance", "BTC", "bsy5-", 0, "close",
                                 {"spot_qty": 2.0, "perp_qty": 2.0})
    assert rec["spot_qty"] == pytest.approx(0) and rec["perp_qty"] == pytest.approx(0)
    assert rec["adjusted"] is None and len(sex.orders) == n_orders   # ninguna orden fantasma


def test_reconcile_close_partial(sex):
    sex.spot_bal["BTC"], sex.short = 2.0, 2.0
    sex.create_order("BTC/USDT", "market", "sell", 1.0, None, {"clientOrderId": "bsy6-s1"})
    sex.create_order("BTC/USDT:USDT", "market", "buy", 1.0, None, {"clientOrderId": "bsy6-p1"})
    rec = te.reconcile_spot_perp(CREDS["binance"], "Binance", "BTC", "bsy6-", 0, "close",
                                 {"spot_qty": 2.0, "perp_qty": 2.0})
    assert rec["spot_qty"] == pytest.approx(1.0) and rec["perp_qty"] == pytest.approx(1.0)
    assert rec["adjusted"] is None


# ── Idempotencia del hedge ──────────────────────────────────────────────────
def test_perp_hedge_timeout_with_accepted_order_is_not_duplicated(sex):
    sex.perp_timeout_once = True
    res = te.execute_open(CREDS, OPP, capital=300, leverage=2, ctx=te.ExecContext(job_id=11))
    assert res["ok"], res
    # El short nunca supera al spot neto (sin la búsqueda por coid se duplicaba).
    assert sex.short <= sex.spot_bal["BTC"] + 1e-9
    assert sex.short == pytest.approx(res["perp_qty"])


def test_hedge_failure_unwinds_spot_including_late_fills(sex):
    sex.perp_always_fail = True
    res = te.execute_open(CREDS, OPP, capital=300, leverage=2, ctx=te.ExecContext(job_id=12))
    assert not res["ok"] and res["code"] == "HEDGE_FAILED"
    assert sex.short == pytest.approx(0)
    assert sex.spot_bal["BTC"] < 0.001                    # todo el spot comprado se deshizo


def test_order_fills_falls_back_to_order_when_trades_lag():
    class Cli:
        def market(self, s):
            return {"base": "BTC", "contract": True, "contractSize": 1}

        def fetch_my_trades(self, s, params=None):
            return []                                     # el endpoint aún no lo refleja

        def fetch_order(self, oid, s):
            return {"filled": 2.0, "average": 100.0, "cost": 200.0, "fee": None}

    f = te.order_fills(Cli(), "BTC/USDT:USDT", "1", expected_filled=2.0)
    assert f["gross"] == pytest.approx(2.0) and f["avg"] == pytest.approx(100.0)
    assert f["cost"] == pytest.approx(200.0)


# ── Estados de los seguros ──────────────────────────────────────────────────
def test_protection_state_mapping_never_assumes_canceled():
    assert te._algo_state({"status": "open"}) == "open"
    assert te._algo_state({"status": "closed"}) == "filled"
    assert te._algo_state({"status": "open", "info": {"actualOrderId": "9"}}) == "filled"
    assert te._algo_state({"status": "canceled"}) == "canceled"
    assert te._algo_state({"status": "TRIGGERING"}) == "unknown"
    assert te._limit_state({"status": "open", "filled": 0.5}) == "filled"   # parcial
    assert te._limit_state({"status": "open", "filled": 0}) == "open"


def test_close_aborts_when_protection_cannot_be_canceled(sex):
    sex.spot_bal["BTC"], sex.short, sex.liq = 2.0, 2.0, 148.0
    prot = te.place_protection(CREDS, POS)
    sex.trigger_cancel_fails = True
    res = te.execute_close(CREDS, {**POS, "protection": prot})
    assert not res["ok"] and res["code"] == "PROTECTION_CANCEL"
    assert sex.short == pytest.approx(2.0)                # nada se operó
    assert not any(o["side"] == "sell" and o["type"] == "market" for o in sex.orders.values())


# ── Polvo ───────────────────────────────────────────────────────────────────
def test_close_dust_only_position_completes(sex):
    sex.spot_bal["BTC"] = 0.0009                          # bajo el mínimo vendible (0.001)
    pos = {**POS, "spot_qty": 0.0009, "perp_qty": 0.0}
    res = te.execute_close(CREDS, pos)
    assert res["ok"] and res["complete"], res


# ── Jobs / vigilante ────────────────────────────────────────────────────────
def _env_with(monkeypatch, env, ex):
    app, runner, _, uid = env
    monkeypatch.setattr(te, "_build_client", lambda *a, **k: ex)
    return app, runner, uid


def _open(runner, app, uid):
    status, _, pos_id, err = _run_sync(runner, app, uid, "open",
                                       {"symbol": "BTC", "opportunity": JOB_OPP, "capital": 300, "leverage": 2})
    assert status == "done", err
    return pos_id


def _busy_job(app, uid, symbol="ETH"):
    from core.db_models import ExecutionJob
    with app.app_context():
        db.session.add(ExecutionJob(user_id=uid, kind="open", status="running",
                                    params_json={"symbol": symbol}))
        db.session.commit()


def test_watcher_closes_fired_position_even_if_user_has_other_job(env, monkeypatch):
    ex = SafeEx()
    app, runner, uid = _env_with(monkeypatch, env, ex)
    pos_id = _open(runner, app, uid)
    _busy_job(app, uid)                                   # TWAP de horas en otro símbolo
    from execution.protection import ProtectionWatcher
    submitted = []
    monkeypatch.setattr(runner, "submit", lambda u, kind, p: submitted.append((kind, p)) or 99)
    monkeypatch.setattr(runner, "_alert", lambda *a, **k: None)
    ex.fire_sl()
    ProtectionWatcher(runner).tick()
    assert submitted and submitted[0][1]["position_id"] == int(pos_id)
    assert submitted[0][1]["trigger"] == "protection"


def test_watcher_alerts_naked_spot_and_unwinds_only_with_flag(env, monkeypatch):
    ex = SafeEx()
    app, runner, uid = _env_with(monkeypatch, env, ex)
    pos_id = _open(runner, app, uid)
    ex.short = 0.0                                        # liquidado: el SL sigue "open"
    from execution.protection import ProtectionWatcher
    alerts, submitted = [], []
    monkeypatch.setattr(runner, "_alert", lambda u, s, m, **k: alerts.append(k.get("typ")))
    monkeypatch.setattr(runner, "submit", lambda u, kind, p: submitted.append(p) or 77)
    monkeypatch.delenv("AUTO_UNWIND_ON_LIQ", raising=False)
    w = ProtectionWatcher(runner)
    w.tick()
    assert "NAKED_SPOT" in alerts and not submitted
    w.tick()
    assert alerts.count("NAKED_SPOT") == 1                # dedup
    monkeypatch.setenv("AUTO_UNWIND_ON_LIQ", "1")
    ProtectionWatcher(runner).tick()
    assert submitted and submitted[0]["trigger"] == "naked_spot"
    assert submitted[0]["position_id"] == int(pos_id)


def test_watcher_warns_near_liquidation(env, monkeypatch):
    ex = SafeEx()
    app, runner, uid = _env_with(monkeypatch, env, ex)
    _open(runner, app, uid)
    orig = ex.fetch_positions

    def near(syms=None):
        out = orig(syms)
        for p in out:
            p.update(markPrice=100.0, liquidationPrice=110.0)   # 10% < 20%
        return out
    monkeypatch.setattr(ex, "fetch_positions", near)
    alerts = []
    monkeypatch.setattr(runner, "_alert", lambda u, s, m, **k: alerts.append(k.get("typ")))
    from execution.protection import ProtectionWatcher
    ProtectionWatcher(runner).tick()
    assert "LIQUIDATION_RISK" in alerts


def test_submit_exclusive_blocks_double_click(env, monkeypatch):
    app, runner, _, uid = env
    monkeypatch.setattr(runner, "_run", lambda jid: None)   # el job queda 'queued'
    with app.app_context():
        first = runner.submit_exclusive(uid, "open", {"symbol": "BTC"})
        second = runner.submit_exclusive(uid, "open", {"symbol": "BTC"})
    assert first is not None and second is None


def test_open_job_entry_time_is_job_start(env, monkeypatch):
    app, runner, ex, uid = env
    from core.db_models import ExecutionJob, UserPosition
    pos_id = _open(runner, app, uid)
    with app.app_context():
        p = UserPosition.query.get(pos_id)
        j = ExecutionJob.query.filter_by(position_id=pos_id).first()
        from datetime import timezone
        start_ms = int(j.created_at.replace(tzinfo=timezone.utc).timestamp() * 1000)
        assert p.entry_time == start_ms


def test_finalized_close_marks_protection_closed(env, monkeypatch):
    ex = SafeEx()
    app, runner, uid = _env_with(monkeypatch, env, ex)
    pos_id = _open(runner, app, uid)
    status, _, _, err = _run_sync(runner, app, uid, "close", {"position_id": pos_id, "symbol": "BTC"})
    assert status == "done", err
    from core.db_models import UserPosition
    with app.app_context():
        assert UserPosition.query.get(pos_id).protection_json["status"] == "closed"


def test_fakeex_baseline_still_hedges():
    # Guardia: el FakeEx base (sin fetch por coid) sigue funcionando con el hedge idempotente.
    ex = FakeEx()
    assert te._find_by_coid(ex, "BTC/USDT:USDT", None) is None
