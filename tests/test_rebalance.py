"""Rebalanceo spot → margen (execution/rebalance.py) — sin red, exchange simulado."""
import pytest

from core.database import db
from exchanges import binance_account as ba
from exchanges import trade_executor as te
from execution import rebalance as reb
from tests.test_exec_safety import SafeEx, _env_with, _open
from tests.test_jobs import _run_sync, env  # noqa: F401 (fixture)


class RebEx(SafeEx):
    """SafeEx + transferencia spot→futuros y margen aislado."""

    def __init__(self, **kw):
        super().__init__(**kw)
        self.transfers, self.margin = [], []
        self.transfer_fails = False

    def transfer(self, code, amount, src, dst):
        if self.transfer_fails:
            raise RuntimeError("binance -1002 not authorized (Universal Transfer)")
        self.transfers.append((code, amount, src, dst))
        return {"id": "t1"}

    def add_margin(self, sym, amount, params=None):
        self.margin.append((sym, amount, dict(params or {})))
        return {"type": "add", "amount": amount}


@pytest.fixture
def renv(env, monkeypatch):
    ex = RebEx()
    app, runner, uid = _env_with(monkeypatch, env, ex)
    monkeypatch.setattr(ba, "_build_client", lambda *a, **k: ex)
    monkeypatch.setattr(reb.time, "sleep", lambda s: None)
    alerts = []
    monkeypatch.setattr(runner, "_alert", lambda u, s, m, **k: alerts.append((k.get("typ"), m)))
    monkeypatch.setenv("REBALANCE_ENABLED", "1")
    ex.liq = 148.0
    pos_id = _open(runner, app, uid)
    return app, runner, uid, ex, pos_id, alerts


def _row(app, pos_id):
    from core.db_models import UserPosition
    with app.app_context():
        r = UserPosition.query.get(int(pos_id))
        db.session.refresh(r)
        return {"spot_qty": r.spot_qty, "perp_qty": r.perp_qty, "prot": dict(r.protection_json or {}),
                "pj": list(r.payments_json or []), "rpp": r.realized_price_pnl}


def _rally(ex, px=125.0):
    ex.book["BTC/USDT"] = (px, px + 0.1)
    ex.book["BTC/USDT:USDT"] = (px + 0.2, px + 0.3)
    ex.mark = px + 0.25


# ── Disparo ─────────────────────────────────────────────────────────────────
def test_trigger_math(monkeypatch):
    monkeypatch.setenv("REBALANCE_ENABLED", "1")
    pos, prot = {"spot_qty": 2.0, "perp_qty": 2.0, "entry_price": 100.0}, {"status": "active", "sl_price": 140.0}
    assert reb.trigger_level(pos, prot) == pytest.approx(124.0)          # 100 + 0.6·(140 − 100)
    assert reb.fraction(pos, prot, 125.0) == pytest.approx(0.2)          # 1 − 100/125
    assert not reb.due(pos, prot, 123.0) and reb.due(pos, prot, 125.0)
    assert reb.trigger_level(pos, {**prot, "anchor": 125.0, "sl_price": 165.0}) == pytest.approx(149.0)
    assert not reb.due(pos, {**prot, "rebalance_at": reb.time.time()}, 125.0)   # cooldown
    assert not reb.due({**pos, "spot_qty": 0.01}, prot, 125.0)                  # < MIN_USD
    monkeypatch.setenv("REBALANCE_ENABLED", "0")
    assert not reb.due(pos, prot, 125.0)


def test_watcher_submits_rebalance_only_when_enabled(renv, monkeypatch):
    app, runner, uid, ex, pos_id, _ = renv
    _rally(ex)
    from execution.protection import ProtectionWatcher
    submitted = []
    monkeypatch.setattr(runner, "submit", lambda u, kind, p: submitted.append((kind, p)) or 5)
    monkeypatch.setenv("REBALANCE_ENABLED", "0")
    ProtectionWatcher(runner).tick()
    assert not submitted
    monkeypatch.setenv("REBALANCE_ENABLED", "1")
    w = ProtectionWatcher(runner)
    w.tick()
    assert submitted and submitted[0][0] == "rebalance"
    p = submitted[0][1]
    assert p["position_id"] == int(pos_id) and p["fraction"] == pytest.approx(1 - 100.0 / ex.mark, rel=0.02)
    w.tick()
    assert len(submitted) == 1                                              # cooldown persistido


# ── Job ─────────────────────────────────────────────────────────────────────
def test_rebalance_job_sells_fraction_adds_margin_and_reanchors(renv):
    app, runner, uid, ex, pos_id, alerts = renv
    before = _row(app, pos_id)
    _rally(ex)
    ex.liq = 160.0                                         # tras la recompra + margen
    f = 0.2
    status, res, _, err = _run_sync(runner, app, uid, "rebalance",
                                    {"position_id": int(pos_id), "symbol": "BTC", "fraction": f, "mark": ex.mark})
    assert status == "done", err
    after = _row(app, pos_id)
    # cantidades: f de cada pierna, redondeado al paso del mercado (spot 0.001, perp 0.01)
    assert after["spot_qty"] == pytest.approx(before["spot_qty"] * (1 - f), abs=0.001)
    assert after["perp_qty"] == pytest.approx(before["perp_qty"] * (1 - f), abs=0.01)
    assert ex.short == pytest.approx(after["perp_qty"], abs=0.01)
    usd = res["rebalance"]["usd"]
    assert usd == pytest.approx(before["spot_qty"] * f * 125.0, rel=0.01)
    assert ex.transfers == [("USDT", int(usd * 100) / 100, "spot", "future")]
    assert ex.margin and ex.margin[0][1] == pytest.approx(int(usd * 100) / 100)
    prot = after["prot"]
    assert prot["status"] == "active" and prot["anchor"] == pytest.approx(125.3)
    assert prot["sl_price"] == pytest.approx(125.3 + 0.8 * (160 - 125.3), abs=0.01)
    assert prot["perp"]["status"] == "open" and prot["spot"]["status"] == "open"
    assert after["rpp"] is not None                        # PnL de precio de la fracción
    reb_rows = [p for p in after["pj"] if p.get("kind") == "rebalance"]
    assert reb_rows and reb_rows[-1]["ok"] and reb_rows[-1]["stage"] == "done"
    assert "REBALANCED" in [a[0] for a in alerts]


def test_margin_failure_keeps_position_safer_and_alerts(renv):
    app, runner, uid, ex, pos_id, alerts = renv
    before = _row(app, pos_id)
    _rally(ex)
    ex.transfer_fails = True
    status, res, _, err = _run_sync(runner, app, uid, "rebalance",
                                    {"position_id": int(pos_id), "symbol": "BTC", "fraction": 0.2, "mark": ex.mark})
    assert status == "failed" and "Universal Transfer" in err
    after = _row(app, pos_id)
    assert after["perp_qty"] < before["perp_qty"]          # short reducido: más seguro
    assert after["prot"]["status"] == "active" and after["prot"]["anchor"] == pytest.approx(125.3)
    assert not ex.margin
    assert ("REBALANCE_FAILED" in [a[0] for a in alerts])
    rows = [p for p in after["pj"] if p.get("kind") == "rebalance"]
    assert rows[-1]["stage"] == "margin" and not rows[-1]["ok"]


def test_rebalance_aborts_if_protection_cannot_be_canceled(renv):
    app, runner, uid, ex, pos_id, alerts = renv
    before = _row(app, pos_id)
    _rally(ex)
    ex.trigger_cancel_fails = True
    status, _, _, err = _run_sync(runner, app, uid, "rebalance",
                                  {"position_id": int(pos_id), "symbol": "BTC", "fraction": 0.2, "mark": ex.mark})
    assert status == "failed"
    after = _row(app, pos_id)
    assert after["spot_qty"] == pytest.approx(before["spot_qty"])   # nada operado
    assert ex.short == pytest.approx(before["perp_qty"])
    assert not ex.transfers and not ex.margin


def test_queued_rebalance_is_not_replayed_after_restart(renv):
    app, runner, uid, ex, pos_id, alerts = renv
    from core.db_models import ExecutionJob
    with app.app_context():
        j = ExecutionJob(user_id=uid, kind="rebalance", status="queued", position_id=int(pos_id),
                         params_json={"position_id": int(pos_id), "symbol": "BTC", "fraction": 0.2})
        db.session.add(j)
        db.session.commit()
        jid = j.id
    runner._recover()                                      # time.sleep ya es no-op en el fixture
    with app.app_context():
        assert ExecutionJob.query.get(jid).status == "cancelled"
    assert not ex.transfers and not ex.margin


# ── Camino BSC: depósito on-chain → crédito → margen ────────────────────────
@pytest.fixture
def bsc(renv, monkeypatch):
    """La posición pasa a spot on-chain; el swap se simula con la pierna CEX del fake y la
    wallet/transferencia/órdenes límite Kyber con stubs."""
    app, runner, uid, ex, pos_id, alerts = renv
    from core.db_models import UserPosition
    from exchanges import kyber_limit_order as klo
    from exchanges import onchain_bsc as ob
    with app.app_context():
        r = UserPosition.query.get(int(pos_id))
        r.spot_venue = "bsc"
        db.session.commit()
    wallet, token = {"address": "0xW", "private_key": "k"}, {"address": "0xT", "decimals": 18, "symbol": "BTC"}
    monkeypatch.setattr(runner, "_bsc_args", lambda *a, **k: (wallet, token))
    monkeypatch.setattr(runner, "_spot_leg_factory", lambda *a, **k: None)
    monkeypatch.setattr(ob, "erc20_balance", lambda t, o: ob.to_wei(10_000, 18))
    sent = []
    monkeypatch.setattr(ob, "transfer_erc20", lambda w, t, to, amt: sent.append((t, to, amt)) or "0xABC")
    monkeypatch.setattr(klo, "place_sell", lambda w, t, q, p: {"id": "lo1", "expires_at": 9e9})
    monkeypatch.setattr(te, "_cancel_bsc_limit", lambda w, s: (False, 0.0, 0.0))
    ex.deposits, ex.dep_polls = [], 0
    ex.fetch_deposit_address = lambda code, params=None: {"address": "0xDEP", "tag": None}

    def deposits(code=None, since=None, limit=None, params=None):
        ex.dep_polls += 1
        return list(ex.deposits)
    ex.fetch_deposits = deposits
    return app, runner, uid, ex, pos_id, alerts, sent


def test_bsc_rebalance_deposits_then_adds_margin(bsc):
    app, runner, uid, ex, pos_id, alerts, sent = bsc
    _rally(ex)
    ex.liq = 160.0
    ex.deposits = [{"txid": "0xabc", "info": {"status": 1}}]           # ya acreditado
    status, res, _, err = _run_sync(runner, app, uid, "rebalance",
                                    {"position_id": int(pos_id), "symbol": "BTC", "fraction": 0.2, "mark": ex.mark})
    assert status == "done", err
    from exchanges import onchain_bsc as ob
    amount = ex.margin[0][1]
    assert sent == [(ob.USDT, "0xDEP", ob.to_wei(amount, 18))]
    assert ex.transfers == [("USDT", amount, "spot", "future")]
    st = res["rebalance"]
    assert st["deposit_tx"] == "0xABC" and st["sent"] <= st["credited"] <= st["margin"]
    assert _row(app, pos_id)["prot"]["spot"]["venue"] == "bsc"


def test_bsc_deposit_timeout_alerts_without_margin(bsc, monkeypatch):
    app, runner, uid, ex, pos_id, alerts, sent = bsc
    _rally(ex)
    monkeypatch.setattr(reb, "DEPOSIT_TIMEOUT", 0)
    status, res, _, err = _run_sync(runner, app, uid, "rebalance",
                                    {"position_id": int(pos_id), "symbol": "BTC", "fraction": 0.2, "mark": ex.mark})
    assert status == "failed" and "TimeoutError" in err
    assert sent and not ex.transfers and not ex.margin
    typ, msg = next(a for a in alerts if a[0] == "REBALANCE_FAILED")
    assert "en tránsito" in msg
    assert _row(app, pos_id)["prot"]["status"] == "active"


def test_deposit_matching_and_credit_states():
    assert ba.BinanceAccount.deposit_credited({"info": {"status": 6}})
    assert not ba.BinanceAccount.deposit_credited({"info": {"status": 0}, "status": "pending"})

    class Cli:
        def fetch_deposits(self, code, since):
            return [{"txid": "ABC", "info": {"status": 1}}, {"txid": None, "info": {}}]
    acct = ba.BinanceAccount.__new__(ba.BinanceAccount)
    acct.spot = Cli()
    assert acct.find_deposit("0xabc", 0)["txid"] == "ABC"
    assert acct.find_deposit("0xdef", 0) is None


def test_transfer_erc20_encodes_call(monkeypatch):
    from exchanges import onchain_bsc as ob
    seen = {}
    monkeypatch.setattr(ob, "_sign_and_send", lambda w, tx, **k: seen.update(tx) or "0xh")
    monkeypatch.setattr(ob, "wait_receipt", lambda h: {"status": "0x1"})
    assert ob.transfer_erc20({}, ob.USDT, "0x" + "ab" * 20, 5 * 10 ** 18) == "0xh"
    assert seen["to"] == ob.USDT and seen["data"].startswith("0xa9059cbb" + "0" * 24 + "ab" * 20)
    assert seen["data"].endswith(hex(5 * 10 ** 18)[2:].rjust(64, "0"))
    monkeypatch.setattr(ob, "wait_receipt", lambda h: {"status": "0x0"})
    with pytest.raises(RuntimeError):
        ob.transfer_erc20({}, ob.USDT, "0x" + "ab" * 20, 1)
