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


# ── Cierre de AGT #44 (2026-10-23): SL disparado + spot BSC sin vender ─────────
class _Klo:
    """kyber_limit_order simulado: una orden con estado y un cancel que puede fallar."""

    def __init__(self, status="open", cancel_fails=False, **order):
        self.order = {"id": 7, "status": status, "makingAmount": str(10 ** 21), **order}
        self.cancel_fails = cancel_fails
        self.cancels = 0

    def get_order(self, maker, oid):
        return dict(self.order)

    def cancel(self, wallet, ids):
        self.cancels += 1
        if self.cancel_fails:
            raise RuntimeError("Kyber LO 4001: order is not active")
        self.order["status"] = "cancelling"


@pytest.fixture
def klo(monkeypatch):
    from exchanges import kyber_limit_order as real

    def make(**kw):
        k = _Klo(**kw)
        monkeypatch.setattr(real, "get_order", k.get_order)
        monkeypatch.setattr(real, "cancel", k.cancel)
        return k
    monkeypatch.setattr(te.time, "sleep", lambda s: None)
    return make


WALLET_BSC = {"address": "0x" + "cd" * 20, "private_key": "0x" + "11" * 32}
SPOT_LO = {"venue": "bsc", "id": 7, "status": "open", "qty": 1000.0}


def test_bsc_limit_already_canceled_is_not_alive(klo):
    k = klo(status="cancelled")
    assert te._cancel_bsc_limit(WALLET_BSC, SPOT_LO) == (False, 0.0, 0.0)
    assert k.cancels == 0


def test_bsc_limit_cancel_error_rechecks_state(klo):
    k = klo(status="open", cancel_fails=True)
    assert te._cancel_bsc_limit(WALLET_BSC, SPOT_LO)[0] is True        # sigue viva → abortar
    k.order["status"] = "cancelled"                                    # otro job ya la canceló
    assert te._cancel_bsc_limit(WALLET_BSC, SPOT_LO)[0] is False


def test_bsc_limit_cancel_ok_and_partial_fill_counted(klo):
    k = klo(status="open", filledMakingAmount=str(25 * 10 ** 19), takingAmount=str(400 * 10 ** 18))
    alive, filled, cost = te._cancel_bsc_limit(WALLET_BSC, SPOT_LO)
    assert not alive and k.cancels == 1
    assert filled == pytest.approx(250.0) and cost == pytest.approx(100.0)


class _OracleAgg:
    """Agregador sobre test_onchain_bsc.Chain: impacto REAL configurable para montos
    > 100 unidades y un price_impact de oráculo arbitrario (lo que reporta Kyber)."""

    def __init__(self, chain, real_impact=0.0, oracle_impact=0.0986):
        self.c, self.real, self.oracle = chain, real_impact, oracle_impact

    def out(self, src, amt):
        o = self.c._out(src, amt)
        return int(o * (1 - self.real)) if amt > 10 ** 20 else o

    def quote(self, src, dst, amt, wallet):
        return {"to_amount": self.out(src, amt), "price_impact": self.oracle}

    def swap_tx(self, src, dst, amt, wallet, slippage, max_impact=0.01):
        q = self.c.swap_tx(src, dst, amt, wallet, slippage, max_impact)
        return {**q, "to_amount": self.out(src, amt), "price_impact": self.oracle}


@pytest.fixture
def bsc_env(monkeypatch):
    """Cadena + perp simulados para cerrar una posición XYZ on-chain."""
    from exchanges import onchain_bsc as ob
    from tests import test_onchain_bsc as t
    c = t.Chain()
    box = {}
    monkeypatch.setattr(ob, "erc20_balance", lambda tok, owner: c.bal.get(tok.lower(), 0))
    monkeypatch.setattr(ob, "erc20_allowance", lambda tok, o, sp: 10 ** 40)

    def send(wallet, tx, agg=None):
        src, dst, amt = tx["_swap"]
        c.bal[src.lower()] -= amt
        c.bal[dst.lower()] = c.bal.get(dst.lower(), 0) + box["agg"].out(src, amt)
        return "0xtx"
    monkeypatch.setattr(ob, "_sign_and_send", send)
    monkeypatch.setattr(ob, "wait_receipt", lambda h, timeout=0: {
        "status": "0x1", "gasUsed": hex(150000), "effectiveGasPrice": hex(50_000_000)})
    ex = FakeEx()
    ex.book["XYZ/USDT:USDT"] = (2.0, 2.002)
    ex.book["BNB/USDT:USDT"] = (600, 600)
    ex.markets["XYZ/USDT:USDT"] = {"id": "XYZUSDT", "base": "XYZ", "spot": False, "contract": True,
                                   "contractSize": 1, "precision": {"amount": 1},
                                   "limits": {"amount": {"min": 1}, "cost": {"min": 5}}}
    monkeypatch.setattr(te, "_build_client", lambda *a, **k: ex)
    monkeypatch.setattr(te, "_ensure_markets", lambda cl: None)
    monkeypatch.setattr(te.time, "sleep", lambda s: None)

    def close(real_impact, oracle_impact, short, qty=500.0, held=None):
        box["agg"] = agg = _OracleAgg(c, real_impact, oracle_impact)
        c.bal[t.TOKEN["address"].lower()] = ob.to_wei(qty if held is None else held, 18)
        ex.short = short
        pos = {"mode": "spot_perp", "symbol": "XYZ", "exchange": "Binance", "spot_venue": "bsc",
               "spot_qty": qty, "perp_qty": qty, "entry_price": 2.0}
        return te.execute_close(t.CREDS, pos, spot_leg_factory=lambda pc, ps, ctx: ob.BscSpotLeg(
            t.WALLET, t.TOKEN, ctx, pc, ps, aggregator=agg))
    return close, ex


def test_bsc_sell_ignores_oracle_impact_when_pool_is_fine(bsc_env):
    close, ex = bsc_env
    res = close(real_impact=0.0, oracle_impact=0.0986, short=500)   # 9.86 % "reportado"
    assert res["ok"] and res["spot_sold"] == pytest.approx(500), res
    assert ex.short == pytest.approx(0)


def test_naked_bsc_close_accepts_real_impact_but_hedged_close_does_not(bsc_env):
    close, ex = bsc_env
    res = close(real_impact=0.03, oracle_impact=None, short=500)    # short vivo: 3 % se bloquea
    assert res["spot_sold"] == 0 and "impacto" in (res.get("partial_note") or ""), res
    assert ex.short == pytest.approx(500)
    res = close(real_impact=0.03, oracle_impact=None, short=0)      # spot desnudo: se vende
    assert res["ok"] and res["spot_sold"] == pytest.approx(500), res


def test_failed_close_after_sl_fill_records_perp_leg(env, monkeypatch):
    ex = SafeEx()
    app, runner, uid = _env_with(monkeypatch, env, ex)
    pos_id = _open(runner, app, uid)
    from core.db_models import UserPosition
    # Lo que devolvió el job #10: el SL llenó el perp y la venta del spot falló.
    res10 = {"ok": True, "dry_run": False, "exit_fees_usd": 0.2,
             "protection_filled": {"perp_filled": 2.0, "perp_cost": 260.0, "spot_filled": 0.0,
                                   "spot_cost": 0.0, "fee_usd": 0.2},
             "spot_sold": 0.0, "perp_closed": 0.0, "complete": False, "order_ids": [], "legs": [],
             "price_pnl": -60.0, "partial_note": "Error vendiendo el spot: impacto de precio 9.86% > máx 1.0%"}
    monkeypatch.setattr(te, "execute_close", lambda *a, **k: dict(res10))
    monkeypatch.setattr(runner, "_alert", lambda *a, **k: None)
    status, _, _, err = _run_sync(runner, app, uid, "close", {"position_id": pos_id, "symbol": "BTC"})
    assert status == "failed" and "impacto" in err
    with app.app_context():
        p = UserPosition.query.get(pos_id)
        assert p.status == "active" and p.perp_qty == pytest.approx(0) and p.spot_qty > 0
        assert p.realized_price_pnl == pytest.approx(-60.0)
        assert p.protection_json["perp"]["status"] == "filled"
        assert p.protection_json["spot"]["status"] == "canceled"    # el reintento no la cancela


# ── Cierre de JCT #46 (2026-10-24): short recomprado fuera de basyo ─────────────
def _jct_state(ex, buyback_px=130.0):
    """Venta límite del spot llenada y short recomprado a mano en la web (sin el SL)."""
    ex.algo[ex.algo_of("STOP_MARKET")]["status"] = "canceled"   # el usuario movió el SL
    ex.fill_spot_limit(1.0)
    ex.book["BTC/USDT:USDT"] = (buyback_px - 0.1, buyback_px)
    ex.create_order("BTC/USDT:USDT", "market", "buy", ex.short, None, {"clientOrderId": "web_x"})
    assert ex.short == pytest.approx(0)


def test_close_books_perp_bought_back_outside_basyo(sex):
    sex.spot_bal["BTC"], sex.short, sex.liq = 2.0, 2.0, 148.0
    prot = te.place_protection(CREDS, POS)
    _jct_state(sex)
    res = te.execute_close(CREDS, {**POS, "protection": prot, "spot_entry_price": 100.0})
    assert res["ok"] and res["complete"] and not res.get("price_pnl_unknown"), res
    spot_px = prot["spot_price"]
    # Ambas piernas: (venta límite − entrada)·2 + (entrada perp − recompra web)·2.
    assert res["perp_external"]["gross"] == pytest.approx(2.0)
    assert res["price_pnl"] == pytest.approx((spot_px - 100.0) * 2 + (100.0 - 130.0) * 2)
    assert res["exit_fees_usd"] >= 130.0 * 2 * 0.0005 - 1e-9          # comisión de la recompra


def test_close_price_pnl_unknown_when_buyback_not_found(sex):
    sex.spot_bal["BTC"], sex.short, sex.liq = 2.0, 2.0, 148.0
    prot = te.place_protection(CREDS, POS)
    sex.algo[sex.algo_of("STOP_MARKET")]["status"] = "canceled"
    sex.fill_spot_limit(1.0)
    sex.short = 0.0                                       # desapareció sin trades legibles
    res = te.execute_close(CREDS, {**POS, "protection": prot, "spot_entry_price": 100.0})
    assert res["ok"] and res["price_pnl"] is None and res["price_pnl_unknown"], res


def test_bsc_dust_left_after_limit_fill_still_buys_back_short(bsc_env, monkeypatch):
    close, ex = bsc_env
    from exchanges import onchain_bsc as ob

    def revert(self, *a, **k):
        raise te.ExecError("RPC eth_estimateGas: execution reverted: Return amount is not enough")
    monkeypatch.setattr(ob.BscSpotLeg, "trade", revert)
    res = close(real_impact=0.0, oracle_impact=None, short=500, held=1e-11)
    assert res["ok"] and res["complete"] and not res.get("partial_note"), res
    assert ex.short == pytest.approx(0)                   # el short no queda abierto y sin SL


def test_job_close_jct_records_both_legs(env, monkeypatch):
    ex = SafeEx()
    ex.liq = 148.0
    app, runner, uid = _env_with(monkeypatch, env, ex)
    pos_id = _open(runner, app, uid)
    from core.db_models import UserHistory, UserPosition
    with app.app_context():
        p = UserPosition.query.get(pos_id)
        spot_entry, perp_entry, qs, qp = p.spot_entry_price, p.entry_price, p.spot_qty, p.perp_qty
        limit_px = p.protection_json["spot_price"]
    _jct_state(ex)
    monkeypatch.setattr(runner, "_alert", lambda *a, **k: None)
    status, _, _, err = _run_sync(runner, app, uid, "close", {"position_id": pos_id, "symbol": "BTC"})
    assert status == "done", err
    with app.app_context():
        h = UserHistory.query.filter_by(user_id=uid, symbol="BTC").order_by(UserHistory.id.desc()).first()
        buy = next(t for t in ex.trades if t["side"] == "buy" and t["symbol"] == "BTC/USDT:USDT"
                   and ex.orders[t["order"]].get("clientOrderId") == "web_x")
        expected = (limit_px - spot_entry) * qs + (perp_entry * qp - buy["cost"] * qp / buy["amount"])
        assert h.price_pnl == pytest.approx(expected, rel=1e-6)


def test_real_net_breakdown_unknown_external_buyback_is_none():
    from execution.jobs import real_net_breakdown

    class Row:
        realized_price_pnl = 438.44                        # solo la pierna spot (JCT #46)
        entry_slippage_usd = None
        entry_basis_pct = None
        payments_json = [{"kind": "auto_close_partial", "price_pnl_unknown": "no encontrada"}]
    assert real_net_breakdown(Row(), {"price_pnl": None})["price_pnl"] is None
    Row.payments_json = []
    assert real_net_breakdown(Row(), {"price_pnl": None, "price_pnl_unknown": "x"})["price_pnl"] is None
    assert real_net_breakdown(Row(), {"price_pnl": -380.9})["price_pnl"] == pytest.approx(57.54)
