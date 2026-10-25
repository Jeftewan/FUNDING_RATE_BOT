"""Seguros spot_perp (SL del short + venta límite spot), vigilante, fallback de
agregador DEX y estado de jobs de cierre — sin red."""
import time

import pytest

from exchanges import dex_aggregator as dexa
from exchanges import trade_executor as te
from tests.test_jobs import _run_sync, env  # noqa: F401 (fixture)
from tests.test_trade_executor import CREDS, FakeEx


class ProtEx(FakeEx):
    """FakeEx + órdenes trigger (algo) y límites lejanas que no se llenan solas."""

    def __init__(self, **kw):
        super().__init__(**kw)
        self.liq = None
        self.mark = None
        self.algo = {}
        self.calls = []

    def create_order(self, s, typ, side, amount, price=None, params=None):
        params = params or {}
        self.calls.append((s, typ, side, amount, price, dict(params)))
        if typ in ("STOP_MARKET", "TAKE_PROFIT_MARKET"):
            self.n += 1
            oid = f"algo{self.n}"
            self.algo[oid] = {"id": oid, "status": "open", "info": {}, "params": dict(params), "type": typ}
            return dict(self.algo[oid])
        o = super().create_order(s, typ, side, amount, price, params)
        b, a = self.book[s]
        if typ == "limit" and side == "sell" and price and price > a * 1.1:
            self.orders[o["id"]]["hold"] = True   # take-profit lejano: queda en reposo
            self.spot_bal["BTC"] -= float(amount)  # bloquea la base (free)
            self.orders[o["id"]]["locked"] = float(amount)
        return o

    def fetch_order(self, oid, s, params=None):
        if (params or {}).get("trigger"):
            return dict(self.algo[oid])
        o = self.orders[oid]
        if o.get("hold"):
            return dict(o)
        return super().fetch_order(oid, s)

    def cancel_order(self, oid, s, params=None):
        if (params or {}).get("trigger"):
            self.algo[oid]["status"] = "canceled"
            return
        o = self.orders[oid]
        if o.get("locked") and o["status"] == "open":
            self.spot_bal["BTC"] += o["locked"] - o["filled"]
        super().cancel_order(oid, s)

    def fetch_positions(self, syms=None):
        out = super().fetch_positions(syms)
        for p in out:
            p["liquidationPrice"] = self.liq
            p["markPrice"] = self.mark
        return out

    # helpers de test
    def fire_sl(self, oid=None):
        oid = oid or next(iter(self.algo))
        self.n += 1
        fill_id = str(self.n)
        self.orders[fill_id] = {"id": fill_id, "symbol": "BTC/USDT:USDT", "type": "market", "side": "buy",
                                "amount": self.short, "price": None, "filled": 0.0, "status": "open"}
        self._fill(self.orders[fill_id], self.short)
        self.algo[oid].update(status="closed", info={"actualOrderId": fill_id})

    def fill_spot_limit(self, frac=1.0):
        """Llena la venta límite spot en reposo (la base ya estaba bloqueada)."""
        o = next(o for o in self.orders.values() if o.get("hold"))
        qty = o["amount"] * frac
        self.spot_bal["BTC"] += qty
        o["locked"] -= qty
        self._fill(o, qty)

    def algo_of(self, typ):
        return next(oid for oid, a in self.algo.items() if a["type"] == typ)


@pytest.fixture
def pex(monkeypatch):
    ex = ProtEx()
    monkeypatch.setattr(te, "POLL_INTERVAL", 0)
    monkeypatch.setattr(te, "REPRICE_SECS", 0.01)
    monkeypatch.setattr(te, "SPOT_LIMIT_TIMEOUT", 0.3)
    monkeypatch.setattr(te, "_build_client", lambda *a, **k: ex)
    monkeypatch.setattr(te, "_ensure_markets", lambda c: None)
    monkeypatch.setattr(te.time, "sleep", lambda s: None)
    return ex


POS = {"id": 1, "mode": "spot_perp", "symbol": "BTC", "exchange": "Binance", "entry_price": 100.0,
       "leverage": 2, "spot_qty": 2.0, "perp_qty": 2.0, "spot_venue": "binance"}


# ── Seguros: colocación ─────────────────────────────────────────────────────
def test_protection_price_is_80pct_towards_liquidation():
    assert te.protection_price(100, 150) == pytest.approx(140)
    assert te.protection_spot_price(140) == pytest.approx(140 * (1 - te.PROTECTION_SPOT_OFFSET))


def test_place_protection_uses_real_liq_and_places_both_legs(pex):
    pex.spot_bal["BTC"], pex.short, pex.liq = 2.0, 2.0, 148.0
    prot = te.place_protection(CREDS, POS)
    assert prot["ok"] and not prot["errors"]
    assert prot["liq_source"] == "exchange"
    assert prot["sl_price"] == pytest.approx(100 + 0.80 * 48)
    sl = next(c for c in pex.calls if c[1] == "STOP_MARKET")
    assert sl[2] == "buy" and sl[3] is None
    assert sl[5]["closePosition"] is True and sl[5]["workingType"] == "MARK_PRICE"
    assert sl[5]["triggerPrice"] == prot["sl_price"] and "reduceOnly" not in sl[5]
    tp = next(c for c in pex.calls if c[1] == "limit")
    # la venta spot va un poco por debajo del SL: en una subida sale primero el spot
    assert tp[2] == "sell" and tp[3] == pytest.approx(2.0) and tp[4] == prot["spot_price"]
    assert prot["spot_price"] == pytest.approx(prot["sl_price"] * (1 - te.PROTECTION_SPOT_OFFSET), abs=0.01)
    assert prot["spot_price"] < prot["sl_price"]
    assert prot["perp"]["status"] == "open" and prot["spot"]["venue"] == "binance"


def test_place_protection_without_spot_only_places_sl(pex):
    pex.short, pex.liq = 2.0, 148.0
    prot = te.place_protection(CREDS, {**POS, "spot_qty": 0.0})
    assert prot["ok"] and not prot["errors"] and prot["spot"] is None
    assert prot["perp"]["status"] == "open"


def test_place_protection_falls_back_to_estimate(pex):
    pex.spot_bal["BTC"], pex.short, pex.liq = 2.0, 2.0, None
    prot = te.place_protection(CREDS, POS)
    assert prot["ok"] and prot["liq_source"] == "estimate"
    liq = te.liquidation_estimate(100, 2, 0.004)["liq_price"]
    assert prot["sl_price"] == pytest.approx(round(te.protection_price(100, liq), 2))


def test_place_protection_unsupported_exchange(pex):
    prot = te.place_protection({"bybit": {"api_key": "k"}}, {**POS, "exchange": "Bybit"})
    assert not prot["ok"] and "no soportados" in prot["msg"]


def test_close_cancels_protection_before_selling(pex):
    pex.spot_bal["BTC"], pex.short, pex.liq = 2.0, 2.0, 148.0
    prot = te.place_protection(CREDS, POS)
    assert pex.spot_bal["BTC"] == pytest.approx(0)       # base bloqueada por la límite
    res = te.execute_close(CREDS, {**POS, "protection": prot})
    assert res["ok"] and res["complete"], res
    assert res["spot_sold"] == pytest.approx(2.0, abs=0.001)
    assert pex.short == pytest.approx(0, abs=1e-9)
    assert all(a["status"] == "canceled" for a in pex.algo.values())


def test_close_after_sl_fired_only_sells_spot(pex):
    pex.spot_bal["BTC"], pex.short, pex.liq = 2.0, 2.0, 148.0
    prot = te.place_protection(CREDS, POS)
    pex.fire_sl()
    assert pex.short == pytest.approx(0)
    n_calls = len(pex.calls)
    res = te.execute_close(CREDS, {**POS, "protection": prot})
    assert res["ok"] and res["complete"], res
    assert res["perp_closed"] == 0 and res["spot_sold"] == pytest.approx(2.0, abs=0.001)
    assert res["protection_filled"]["perp_filled"] == pytest.approx(2.0)
    assert res["exit_fees_usd"] > 0
    # spot desnudo: taker directo, sin ventana maker
    assert [c[1] for c in pex.calls[n_calls:]] == ["market"]


# ── Agregador DEX: fallback por región ──────────────────────────────────────
class _Agg:
    def __init__(self, fail_code=None):
        self.fail_code, self.n = fail_code, 0

    def quote(self, *a, **k):
        self.n += 1
        if self.fail_code:
            raise dexa.AggregatorError("blocked", code=self.fail_code)
        return {"to_amount": 1, "price_impact": None}

    swap_tx = quote


def test_fallback_on_compliance_40304_and_remembers():
    primary, kyber = _Agg(fail_code=40304), _Agg()
    agg = dexa.FallbackAggregator([primary, kyber])
    assert agg.quote("a", "b", 1, "w")["to_amount"] == 1
    agg.quote("a", "b", 1, "w")
    assert primary.n == 1 and kyber.n == 2          # no reintenta el bloqueado
    with pytest.raises(dexa.AggregatorError):
        agg.broadcast("0x", "w")                     # Kyber sin broadcast → RPC


def test_fallback_does_not_hide_other_errors():
    agg = dexa.FallbackAggregator([_Agg(fail_code=50000), _Agg()])
    with pytest.raises(dexa.AggregatorError):
        agg.quote("a", "b", 1, "w")


def test_build_aggregator_wraps_with_kyber(monkeypatch):
    monkeypatch.setenv("DEX_AGGREGATOR", "binance")
    monkeypatch.delenv("BINANCE_WEB3_API_KEY", raising=False)
    assert isinstance(dexa.build_aggregator(), dexa.KyberSwapAggregator)   # sin keys → Kyber
    monkeypatch.setenv("BINANCE_WEB3_API_KEY", "k")
    monkeypatch.setenv("BINANCE_WEB3_API_SECRET", "s")
    agg = dexa.build_aggregator()
    assert isinstance(agg, dexa.FallbackAggregator)
    assert isinstance(agg.chain[-1], dexa.KyberSwapAggregator)
    monkeypatch.setenv("DEX_AGGREGATOR", "kyber")
    assert isinstance(dexa.build_aggregator(), dexa.KyberSwapAggregator)


# ── Jobs ────────────────────────────────────────────────────────────────────
OPP = {"mode": "spot_perp", "symbol": "BTC", "exchange": "Binance", "price": 100.0,
       "funding_rate": 0.0001, "interval_hours": 8, "payments_per_day": 3}


def _with_prot_ex(monkeypatch, env):
    app, runner, _, uid = env
    ex = ProtEx()
    monkeypatch.setattr(te, "_build_client", lambda *a, **k: ex)
    return app, runner, ex, uid


def test_open_job_places_protection(env, monkeypatch):
    app, runner, ex, uid = _with_prot_ex(monkeypatch, env)
    status, res, pos_id, err = _run_sync(runner, app, uid, "open",
                                         {"symbol": "BTC", "opportunity": OPP, "capital": 300, "leverage": 2})
    assert status == "done", err
    from core.db_models import UserPosition
    with app.app_context():
        prot = UserPosition.query.get(pos_id).protection_json
    assert prot["status"] == "active" and prot["perp"]["status"] == "open" and prot["spot"]["status"] == "open"
    # cierre normal: cancela seguros y cierra todo
    status, res, _, err = _run_sync(runner, app, uid, "close", {"position_id": pos_id, "symbol": "BTC"})
    assert status == "done", err
    assert ex.short == pytest.approx(0, abs=1e-9)
    assert all(a["status"] == "canceled" for a in ex.algo.values())


def test_close_job_with_nothing_executed_is_failed(env, monkeypatch):
    app, runner, ex, uid = _with_prot_ex(monkeypatch, env)
    status, _, pos_id, err = _run_sync(runner, app, uid, "open",
                                       {"symbol": "BTC", "opportunity": OPP, "capital": 300, "leverage": 2})
    assert status == "done", err
    alerts = []
    monkeypatch.setattr(runner, "_alert", lambda uid, sym, msg, **k: alerts.append(msg))
    monkeypatch.setattr(te, "execute_close", lambda *a, **k: {
        "ok": True, "spot_sold": 0.0, "perp_closed": 0.0, "complete": False, "legs": [],
        "partial_note": "Error vendiendo el spot: Web3 API 40304: Service not available"})
    restored = []
    monkeypatch.setattr(runner, "_restore_protection", lambda *a, **k: restored.append(a))
    status, _, _, err = _run_sync(runner, app, uid, "close", {"position_id": pos_id, "symbol": "BTC"})
    assert status == "failed" and "40304" in err
    assert alerts and "sigue abierta" in alerts[-1]
    assert restored
    from core.db_models import UserPosition
    with app.app_context():
        assert UserPosition.query.get(pos_id).status == "active"


# ── Vigilante ───────────────────────────────────────────────────────────────
def test_watcher_submits_close_when_sl_fires(env, monkeypatch):
    app, runner, ex, uid = _with_prot_ex(monkeypatch, env)
    status, _, pos_id, err = _run_sync(runner, app, uid, "open",
                                       {"symbol": "BTC", "opportunity": OPP, "capital": 300, "leverage": 2})
    assert status == "done", err
    from execution.protection import ProtectionWatcher
    w = ProtectionWatcher(runner)
    submitted = []
    monkeypatch.setattr(runner, "submit", lambda uid, kind, p: submitted.append((kind, p)) or 99)
    monkeypatch.setattr(runner, "_alert", lambda *a, **k: None)

    w.tick()
    assert not submitted                         # nada llenado todavía
    ex.fire_sl()
    w.tick()
    assert submitted and submitted[0][0] == "close"
    assert submitted[0][1]["position_id"] == int(pos_id) and submitted[0][1]["trigger"] == "protection"


def test_watcher_marks_external_cancel(env, monkeypatch):
    app, runner, ex, uid = _with_prot_ex(monkeypatch, env)
    _, _, pos_id, _ = _run_sync(runner, app, uid, "open",
                                {"symbol": "BTC", "opportunity": OPP, "capital": 300, "leverage": 2})
    from execution.protection import ProtectionWatcher
    alerts = []
    monkeypatch.setattr(runner, "_alert", lambda uid, sym, msg, **k: alerts.append(k.get("typ")))
    for a in ex.algo.values():
        a["status"] = "canceled"
    ProtectionWatcher(runner).tick()
    from core.db_models import UserPosition
    with app.app_context():
        prot = UserPosition.query.get(pos_id).protection_json
    assert prot["perp"]["status"] == "canceled" and "PROTECTION_CANCELED" in alerts


def test_watcher_renews_bsc_limit_before_expiry(env, monkeypatch):
    app, runner, ex, uid = env
    from execution import protection as pw
    from exchanges import kyber_limit_order as klo
    from exchanges import onchain_bsc as bsc
    pos = {"id": 7, "symbol": "AGT", "exchange": "Binance", "spot_venue": "bsc",
           "protection": {"status": "active", "sl_price": 0.15,
                          "perp": {"id": "a1", "status": "open"},
                          "spot": {"venue": "bsc", "id": 11, "status": "open", "qty": 170.0,
                                   "expires_at": time.time() + 3600}}}
    wallet, token = {"address": "0xW", "private_key": "0x1"}, {"address": "0xT", "decimals": 18}
    monkeypatch.setattr(runner, "_creds", lambda *a: CREDS)
    monkeypatch.setattr(runner, "_bsc_args", lambda *a, **k: (wallet, token))
    monkeypatch.setattr(te, "protection_status", lambda *a: {"perp": "open", "spot": "open"})
    canceled, saved, alerts = [], [], []
    monkeypatch.setattr(klo, "cancel", lambda w, ids: canceled.extend(ids))
    monkeypatch.setattr(klo, "place_sell", lambda w, t, q, p: {"id": 12, "expires_at": time.time() + klo.ORDER_TTL})
    monkeypatch.setattr(bsc, "erc20_balance", lambda *a: 170 * 10 ** 18)
    monkeypatch.setattr(runner, "_save_protection", lambda pid, p: saved.append(p))
    monkeypatch.setattr(runner, "_alert", lambda uid, sym, msg, **k: alerts.append(k.get("typ")))
    pw.ProtectionWatcher(runner).check(uid, pos)
    assert canceled == [11]
    assert saved[0]["spot"]["id"] == 12 and saved[0]["spot"]["status"] == "open"
    assert "PROTECTION_RENEWED" in alerts


# ── Vigilante: caso B (short suelto) y modo caliente ────────────────────────
def _opened_watcher(env, monkeypatch):
    app, runner, ex, uid = _with_prot_ex(monkeypatch, env)
    status, _, pos_id, err = _run_sync(runner, app, uid, "open",
                                       {"symbol": "BTC", "opportunity": OPP, "capital": 300, "leverage": 2})
    assert status == "done", err
    from execution.protection import ProtectionWatcher
    submitted, alerts = [], []
    monkeypatch.setattr(runner, "submit", lambda uid, kind, p: submitted.append((kind, p)) or 99)
    monkeypatch.setattr(runner, "_alert", lambda uid, sym, msg, **k: alerts.append(k.get("typ")))
    return app, runner, ex, uid, pos_id, ProtectionWatcher(runner), submitted, alerts


def _prot(app, pos_id):
    from core.db_models import UserPosition
    with app.app_context():
        row = UserPosition.query.get(pos_id)
        return dict(row.protection_json), row.spot_qty, row.perp_qty, row.realized_price_pnl


def test_watcher_full_spot_fill_leaves_short_running_with_tp(env, monkeypatch):
    app, runner, ex, uid, pos_id, w, submitted, alerts = _opened_watcher(env, monkeypatch)
    prot0, spot0, perp0, _ = _prot(app, pos_id)
    ex.fill_spot_limit()
    w.tick()
    assert not submitted                              # no recompra al instante
    prot, spot_qty, perp_qty, pnl = _prot(app, pos_id)
    assert prot["status"] == "runner" and "SPOT_TP_RUNNER" in alerts
    assert prot["perp"]["status"] == "open"           # el SL duro sigue vivo
    assert prot["spot"]["status"] == "filled" and prot["spot"]["fill_qty"] == pytest.approx(spot0)
    assert spot_qty == pytest.approx(0) and perp_qty == pytest.approx(perp0)
    assert pnl is not None
    tp = ex.algo[ex.algo_of("TAKE_PROFIT_MARKET")]
    assert tp["params"]["closePosition"] is True
    assert tp["params"]["triggerPrice"] == pytest.approx(prot0["spot_price"] * (1 - te.RUNNER_TP_PCT), abs=0.01)

    w.tick()
    assert not submitted                              # nada nuevo: sigue suelto
    ex.fire_sl(ex.algo_of("TAKE_PROFIT_MARKET"))      # tocó el take profit
    w.tick()
    assert submitted and submitted[0][1]["trigger"] == "protection"
    assert _prot(app, pos_id)[0]["triggered_by"] == "tp"


def test_runner_close_job_cancels_sl_and_closes(env, monkeypatch):
    app, runner, ex, uid, pos_id, w, submitted, alerts = _opened_watcher(env, monkeypatch)
    ex.fill_spot_limit()
    w.tick()
    monkeypatch.undo()                                # submit real para el cierre
    monkeypatch.setattr(te, "_build_client", lambda *a, **k: ex)
    ex.fire_sl(ex.algo_of("TAKE_PROFIT_MARKET"))
    status, res, _, err = _run_sync(runner, app, uid, "close", {"position_id": pos_id, "symbol": "BTC"})
    assert status == "done", err
    assert ex.short == pytest.approx(0, abs=1e-9)
    assert ex.algo[ex.algo_of("STOP_MARKET")]["status"] == "canceled"
    from core.db_models import UserPosition
    with app.app_context():
        assert UserPosition.query.get(pos_id).status != "active"


def test_runner_times_out_and_buys_back(env, monkeypatch):
    app, runner, ex, uid, pos_id, w, submitted, alerts = _opened_watcher(env, monkeypatch)
    ex.fill_spot_limit()
    w.tick()
    monkeypatch.setattr(te, "RUNNER_MAX_SECS", 0)
    w.tick()
    assert submitted and _prot(app, pos_id)[0]["triggered_by"] == "runner_timeout"


def test_partial_spot_fill_still_closes(env, monkeypatch):
    app, runner, ex, uid, pos_id, w, submitted, alerts = _opened_watcher(env, monkeypatch)
    ex.fill_spot_limit(0.5)
    w.tick()
    assert submitted and _prot(app, pos_id)[0]["triggered_by"] == "spot"


def test_runner_falls_back_to_close_if_tp_fails(env, monkeypatch):
    app, runner, ex, uid, pos_id, w, submitted, alerts = _opened_watcher(env, monkeypatch)
    ex.fill_spot_limit()

    def boom(*a, **k):
        raise RuntimeError("rechazada")
    monkeypatch.setattr(te, "place_runner_tp", boom)
    w.tick()
    assert submitted and _prot(app, pos_id)[0]["triggered_by"] == "spot"


def test_watcher_goes_hot_near_sl(env, monkeypatch):
    app, runner, ex, uid, pos_id, w, submitted, alerts = _opened_watcher(env, monkeypatch)
    sl = _prot(app, pos_id)[0]["sl_price"]
    ex.mark = 100.0
    w.tick()
    assert w._hot is False
    ex.mark = sl * 0.99
    w.tick()
    assert w._hot is True and not submitted
