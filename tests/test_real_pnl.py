"""PnL neto real: funding + PnL de precio (fills reales de ambas piernas) − fees."""
import pytest

from exchanges import trade_executor as te
from tests.test_jobs import _run_sync, env  # noqa: F401 (fixture)
from tests.test_protection import POS, ProtEx, pex  # noqa: F401 (fixture)
from tests.test_trade_executor import CREDS, OPP, fake  # noqa: F401 (fixture)


def test_open_reports_gross_spot_price_and_slippage(fake):
    res = te.execute_open(CREDS, OPP, capital=300, leverage=2)
    assert res["ok"], res
    spot_buys = [t for t in fake.trades if fake.orders[t["order"]]["symbol"] == "BTC/USDT"]
    gross = sum(t["amount"] for t in spot_buys)
    cost = sum(t["cost"] for t in spot_buys)
    # Bruto: la comisión cobrada en BTC va en fees, no en el precio.
    assert res["spot_avg_price_gross"] == pytest.approx(cost / gross)
    assert res["spot_avg_price"] > res["spot_avg_price_gross"]
    slip = res["entry_slippage"]
    mid_spot, mid_perp = res["ref_mids"]["spot_mid"], res["ref_mids"]["perp_mid"]
    assert mid_spot == pytest.approx(100.05) and mid_perp == pytest.approx(100.25)
    # spot maker al bid (100.0 < mid) → slippage negativo (mejor que el mid); perp taker al bid → costo.
    assert slip["spot"] < 0 and slip["perp"] > 0
    assert res["basis_pct"] == pytest.approx((res["entry_price"] - cost / gross) / (cost / gross) * 100, abs=1e-4)


def test_close_price_pnl_from_real_fills(fake):
    fake.spot_bal["BTC"], fake.short = 2.0, 2.0
    pos = {"mode": "spot_perp", "symbol": "BTC", "exchange": "Binance", "spot_qty": 2.0, "perp_qty": 2.0,
           "entry_price": 101.0, "spot_entry_price": 99.0}
    res = te.execute_close(CREDS, pos)
    assert res["ok"] and res["complete"], res
    ex = res["exec"]
    expected = (ex["spot_exit"] - 99.0) * ex["spot_qty_out"] + (101.0 - ex["perp_exit"]) * ex["perp_qty_out"]
    assert res["price_pnl"] == pytest.approx(expected)
    assert ex["exit_basis_pct"] == pytest.approx((ex["perp_exit"] - ex["spot_exit"]) / ex["spot_exit"] * 100, abs=1e-4)
    assert res["exit_slippage"]["total"] is not None


def test_close_price_pnl_legacy_entry_from_audit(fake):
    fake.spot_bal["BTC"], fake.short = 1.0, 1.0
    pos = {"mode": "spot_perp", "symbol": "BTC", "exchange": "Binance", "spot_qty": 1.0, "perp_qty": 1.0,
           "entry_price": 101.0,
           "payments": [{"kind": "auto_open", "legs": [{"kind": "spot", "price": 99.5}]}]}
    res = te.execute_close(CREDS, pos)
    assert res["exec"]["spot_entry"] == 99.5 and res["price_pnl"] is not None


def test_close_without_entry_prices_has_no_price_pnl(fake):
    fake.spot_bal["BTC"], fake.short = 1.0, 1.0
    pos = {"mode": "spot_perp", "symbol": "BTC", "exchange": "Binance", "spot_qty": 1.0, "perp_qty": 1.0}
    res = te.execute_close(CREDS, pos)
    assert res["ok"] and res["price_pnl"] is None


def test_close_after_sl_includes_sl_fill_in_price_pnl(pex):
    pex.spot_bal["BTC"], pex.short, pex.liq = 2.0, 2.0, 148.0
    prot = te.place_protection(CREDS, POS)
    pex.fire_sl()                       # recompra 2.0 al ask del perp (100.3)
    res = te.execute_close(CREDS, {**POS, "spot_entry_price": 99.0, "protection": prot})
    ex = res["exec"]
    assert ex["perp_qty_out"] == pytest.approx(2.0) and ex["perp_exit"] == pytest.approx(100.3)
    expected = (ex["spot_exit"] - 99.0) * ex["spot_qty_out"] + (100.0 - 100.3) * 2.0
    assert res["price_pnl"] == pytest.approx(expected)


def test_job_history_net_includes_price_pnl(env, monkeypatch):
    app, runner, ex, uid = env
    opp = {**OPP, "funding_rate": 0.0001, "interval_hours": 8, "payments_per_day": 3}
    status, res, pos_id, err = _run_sync(runner, app, uid, "open",
                                         {"symbol": "BTC", "opportunity": opp, "capital": 300, "leverage": 2})
    assert status == "done", err
    from core.database import db
    from core.db_models import UserHistory, UserPosition
    with app.app_context():
        p = UserPosition.query.get(pos_id)
        assert p.spot_entry_price == pytest.approx(res["spot_avg_price_gross"])
        assert p.entry_slippage_usd == pytest.approx(res["entry_slippage"]["total"])
        p.earned_real = 0.5
        db.session.commit()
    ex.book["BTC/USDT"] = (102.0, 102.1)          # mercado sube: spot gana, perp pierde
    ex.book["BTC/USDT:USDT"] = (102.2, 102.3)
    status, cres, _, err = _run_sync(runner, app, uid, "close", {"position_id": pos_id, "symbol": "BTC"})
    assert status == "done", err
    with app.app_context():
        h = UserHistory.query.filter_by(user_id=uid).first()
        assert h.price_pnl == pytest.approx(cres["price_pnl"])
        assert h.net_earned == pytest.approx(h.earned + h.price_pnl - h.fees)
        assert h.exec_json["entry_basis_pct"] is not None and h.exec_json["exit_basis_pct"] is not None
        assert h.slippage_usd is not None
        # KPIs: el neto all-time incluye el PnL de precio.
        from core.db_persistence import DBPersistence
        agg = DBPersistence().aggregate_daily_earnings(uid, days=7)
        assert agg["all_time"]["price_pnl"] == pytest.approx(h.price_pnl)
        assert agg["all_time"]["net"] == pytest.approx(agg["all_time"]["earned"] + h.price_pnl
                                                       - agg["all_time"]["fees"])


def test_real_net_breakdown_sums_partials():
    from execution.jobs import real_net_breakdown

    class Row:
        realized_price_pnl = -0.4
        entry_slippage_usd = 0.1
        entry_basis_pct = 0.2

    out = real_net_breakdown(Row(), {"price_pnl": 1.0, "exit_slippage": {"total": 0.05}, "exec": {"x": 1}})
    assert out["price_pnl"] == pytest.approx(0.6)
    assert out["slippage_usd"] == pytest.approx(0.15)
    assert out["exec"]["partial_price_pnl"] == -0.4 and out["exec"]["x"] == 1

    class Bare:
        realized_price_pnl = None
        entry_slippage_usd = None
        entry_basis_pct = None

    assert real_net_breakdown(Bare(), {})["price_pnl"] is None
