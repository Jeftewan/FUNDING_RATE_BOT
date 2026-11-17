"""Rentabilidad real sobre capital total e informe mensual (SQLite en memoria)."""
from datetime import datetime, timezone

import pytest
from flask import Flask

from core.database import db
from portfolio import returns as r


def test_position_return_on_total_capital():
    # 1x: 1.000 USD = 500 spot + 500 margen; 5 USD netos en 10 días.
    out = r.position_return(5, 1000, 10)
    assert out["roc_pct"] == pytest.approx(0.5)
    assert out["apr_pct"] == pytest.approx(18.25)
    assert r.position_return(5, 0, 10) == {"roc_pct": None, "apr_pct": None}
    assert r.position_return(5, 1000, 0.01)["apr_pct"] is None


def test_mark_to_market_uses_basis():
    pos = {"spot_qty": 100, "perp_qty": 100, "spot_entry_price": 1.0, "entry_price": 1.01,
           "capital_used": 202}
    # perp 1.02, basis +1 % → spot 1.02/1.01; el short pierde 1, el spot gana ~0.99.
    spot = r.spot_from_basis(1.02, 1.0)
    u = r.unrealized_price_pnl(pos, 1.02, spot)
    assert u == pytest.approx(100 * (spot - 1.0) + 100 * (1.01 - 1.02))
    m = r.active_mark_to_market(pos, 1.02, 2.0, 5, basis_pct=1.0)
    assert m["net_mtm"] == pytest.approx(2.0 + u, abs=1e-6)
    assert m["mtm_approx"] is False
    assert r.active_mark_to_market(pos, 1.02, 2.0, 5)["mtm_approx"] is True


def test_manual_position_without_prices_is_realized_only():
    pos = {"spot_qty": None, "perp_qty": None, "capital_used": 500}
    m = r.active_mark_to_market(pos, 1.0, 3.0, 2)
    assert m["unrealized_price_pnl"] is None and m["net_mtm"] == 3.0
    assert m["real_roc_pct"] == pytest.approx(0.6)


def test_closed_return():
    out = r.closed_return({"net_earned": -2, "capital_used": 400, "hours": 48})
    assert out["real_roc_pct"] == pytest.approx(-0.5)
    assert out["real_apr_pct"] == pytest.approx(-91.25)


# ── Informe mensual ────────────────────────────────────────────────────────
def _ts(y, m, d, h=0):
    return datetime(y, m, d, h, tzinfo=timezone.utc).timestamp()


@pytest.fixture
def app():
    a = Flask(__name__)
    a.config["SQLALCHEMY_DATABASE_URI"] = "sqlite://"
    a.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False
    db.init_app(a)
    with a.app_context():
        from core import db_models  # noqa: F401 — registra las tablas
        db.create_all()
        yield a
        db.session.remove()
        db.drop_all()


def _seed():
    from core import db_models as m
    u = m.User(email="inv@t.co")
    db.session.add(u)
    db.session.commit()
    # Abierta 25-oct, cerrada 5-nov: pagos en ambos meses.
    p = m.UserPosition(user_id=u.id, symbol="AAA", exchange="binance", mode="spot_perp",
                       entry_time=_ts(2026, 10, 25) * 1000, capital_used=1000, exposure=500,
                       entry_fees=1.0, status="closed",
                       closed_at=datetime(2026, 11, 5, tzinfo=timezone.utc),
                       payments_json=[
                           {"ts": _ts(2026, 10, 25, 1), "kind": "auto_open"},
                           {"ts": _ts(2026, 10, 28), "rate": 0.001, "earned": 3.0},
                           {"ts": _ts(2026, 11, 2), "rate": 0.001, "earned": 2.0},
                       ])
    db.session.add(p)
    db.session.commit()
    db.session.add(m.UserHistory(user_id=u.id, symbol="AAA", exchange="binance", capital_used=1000,
                                 exposure=500, hours=264, earned=5.0, fees=2.5, net_earned=3.5,
                                 price_pnl=1.0, closed_at=datetime(2026, 11, 5, tzinfo=timezone.utc),
                                 position_id=p.id))
    db.session.commit()
    return u.id


def test_monthly_report_splits_funding_by_payment_date(app):
    from portfolio.reports import monthly_report, available_months
    uid = _seed()
    now = _ts(2026, 11, 17)
    oct_ = monthly_report(uid, "2026-10", now=now)
    nov = monthly_report(uid, "2026-11", now=now)
    assert oct_["pnl"]["funding"] == pytest.approx(3.0)
    assert oct_["pnl"]["fees"] == pytest.approx(-1.0)          # entrada
    assert nov["pnl"]["funding"] == pytest.approx(2.0)
    assert nov["pnl"]["fees"] == pytest.approx(-1.5)           # salida = 2.5 − 1.0
    assert nov["pnl"]["price"] == pytest.approx(1.0)
    # Total = net_earned del historial.
    assert oct_["pnl"]["net"] + nov["pnl"]["net"] == pytest.approx(3.5)
    # Sin capital diario → capital desplegado promedio, marcado como estimado.
    assert oct_["capital"]["estimated"] is True
    assert oct_["capital"]["avg"] == pytest.approx(1000 * 7 / 31, rel=1e-3)
    assert oct_["n_closed"] == 0 and nov["n_closed"] == 1
    assert "2026-10" in available_months(uid)


def test_monthly_report_uses_daily_capital(app):
    from core import db_models as m
    from portfolio.reports import monthly_report
    uid = _seed()
    for d, cap in (("2026-11-01", 2000), ("2026-11-10", 3000)):
        db.session.add(m.UserCapitalDaily(user_id=uid, date=d, total_capital=cap, source="real"))
    db.session.commit()
    rep = monthly_report(uid, "2026-11", now=_ts(2026, 11, 17))
    # now = 17-nov 00:00 → 16 días: 1–9 nov = 2.000 (sin fila → registro previo), 10–16 = 3.000.
    avg = (9 * 2000 + 7 * 3000) / 16
    assert rep["capital"]["estimated"] is True and rep["capital"]["filled_days"] == 14
    assert rep["capital"]["avg"] == pytest.approx(avg, rel=1e-4)
    assert rep["capital"]["start"] == 2000 and rep["capital"]["end"] == 3000
    assert rep["return_pct"] == pytest.approx(1.5 / avg * 100, abs=1e-3)
    # Octubre (antes del primer registro) usa el primer registro, marcado como estimado.
    oct_ = monthly_report(uid, "2026-10", now=_ts(2026, 11, 17))
    assert oct_["capital"]["avg"] == pytest.approx(2000) and oct_["capital"]["estimated"] is True
    assert rep["partial_month"] is True


def test_empty_and_future_month(app):
    from portfolio.reports import monthly_report
    uid = _seed()
    rep = monthly_report(uid, "2026-08", now=_ts(2026, 11, 17))
    assert rep["pnl"]["net"] == 0 and rep["positions"] == []
    with pytest.raises(ValueError):
        monthly_report(uid, "2026-12", now=_ts(2026, 11, 17))


def test_record_capital_daily_config_fallback(app):
    from core import db_models as m
    from core.db_persistence import DBPersistence
    uid = _seed()
    db.session.add(m.UserConfig(user_id=uid, total_capital=1500))
    db.session.commit()
    out = DBPersistence().record_capital_daily(uid, now=datetime(2026, 11, 17, tzinfo=timezone.utc))
    assert out == {"date": "2026-11-17", "total_capital": 1500.0, "deployed_capital": 0, "source": "config"}
