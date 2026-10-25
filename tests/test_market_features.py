"""market_feature_snapshots: colector (exchange simulado), upsert SQLite y tolerancia a fallos."""
import types

import pytest
from flask import Flask

from core.database import db
from exchanges import market_features as mf

NOW = 1_792_941_474.0           # 2026-10-23 …:57 UTC
HOUR = mf.hour_bucket(NOW)
DAY_MS = 86_400_000


class FakeBinance:
    def __init__(self, fail=()):
        self.fail = set(fail)
        self.calls = []

    def _call(self, name, value, params=None):
        self.calls.append((name, (params or {}).get("symbol")))
        if name in self.fail or (name, (params or {}).get("symbol")) in self.fail:
            raise RuntimeError(f"{name} 503")
        return value

    def fapiPublicGetPremiumIndex(self, p=None):
        return self._call("premium", [
            {"symbol": "AAAUSDT", "markPrice": "1.01", "indexPrice": "1.0",
             "lastFundingRate": "0.0005", "nextFundingTime": 1792944000000},
            {"symbol": "BBBUSDT", "markPrice": "2", "indexPrice": "2",
             "lastFundingRate": "0.0001", "nextFundingTime": 1792944000000}])

    def fapiPublicGetFundingInfo(self, p=None):
        return self._call("finfo", [{"symbol": "AAAUSDT", "fundingIntervalHours": 4,
                                     "adjustedFundingRateCap": "0.02",
                                     "adjustedFundingRateFloor": "-0.02"}])

    def publicGetTickerBookTicker(self, p=None):
        return self._call("spot_bt", [{"symbol": "AAAUSDT", "bidPrice": "0.99", "askPrice": "1.01"}])

    def fapiPublicGetTickerBookTicker(self, p=None):
        return self._call("perp_bt", [{"symbol": "AAAUSDT", "bidPrice": "1.00", "askPrice": "1.02"}])

    def fapiDataGetOpenInterestHist(self, p):
        return self._call("oi", [{"sumOpenInterest": "100", "sumOpenInterestValue": "101"},
                                 {"sumOpenInterest": "110", "sumOpenInterestValue": "111"}], p)

    def fapiDataGetTakerlongshortRatio(self, p):
        return self._call("taker", [{"buySellRatio": "1.25"}], p)

    def fapiDataGetTopLongShortPositionRatio(self, p):
        return self._call("top", [{"longShortRatio": "1.7"}], p)

    def fapiPublicGetKlines(self, p):
        day = int(NOW // 86400)
        # 8 días completos + el día en curso (se descarta).
        closes = [1.0, 1.1, 1.0, 1.1, 1.0, 1.1, 1.0, 1.1, 5.0]
        return self._call("klines", [[(day - 8 + i) * DAY_MS, 0, 0, 0, c] for i, c in enumerate(closes)], p)


def _collector(ex):
    mgr = types.SimpleNamespace(get_exchange=lambda name: ex if name == "binance" else None)
    return mf.MarketFeatureCollector(mgr, sleep=lambda s: None, alpha_loader=lambda: {})


@pytest.fixture
def app():
    a = Flask(__name__)
    a.config["SQLALCHEMY_DATABASE_URI"] = "sqlite://"
    a.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False
    db.init_app(a)
    with a.app_context():
        from core import db_models  # noqa: F401
        db.create_all()
        yield a
        db.session.remove()
        db.drop_all()


def _rows():
    from core.db_models import MarketFeatureSnapshot
    return MarketFeatureSnapshot.query.order_by(MarketFeatureSnapshot.symbol).all()


def test_collect_parsea_todas_las_fuentes():
    rows = _collector(FakeBinance()).collect(["AAA", "BBB"], now=NOW)
    a = next(r for r in rows if r["symbol"] == "AAA")
    assert a["exchange"] == "Binance" and a["ts_hour"] == HOUR
    assert a["funding_rate_est"] == pytest.approx(0.0005)
    assert a["mark_price"] == pytest.approx(1.01) and a["index_price"] == pytest.approx(1.0)
    assert a["basis_pct"] == pytest.approx(1.0)          # mids 1.00 → 1.01
    assert a["oi_change_1h_pct"] == pytest.approx(10.0)
    assert a["open_interest_usd"] == pytest.approx(111)
    assert a["taker_buy_sell_ratio"] == pytest.approx(1.25)
    assert a["top_long_short_pos_ratio"] == pytest.approx(1.7)
    assert (a["funding_interval_hours"], a["funding_cap"], a["funding_floor"]) == (4, 0.02, -0.02)
    # La vela del día en curso (5.0) no entra: retornos ±log(1.1) alternados.
    assert a["volatility_7d"] == pytest.approx(mf.vol_7d([1.0, 1.1] * 4))
    b = next(r for r in rows if r["symbol"] == "BBB")
    assert b["funding_interval_hours"] == 8 and b["funding_cap"] is None   # no listado en fundingInfo
    assert b["basis_pct"] is None                                          # sin book spot


def test_vol_cacheada_por_dia():
    ex = FakeBinance()
    c = _collector(ex)
    c.collect(["AAA"], now=NOW)
    c.collect(["AAA"], now=NOW + 3600)
    assert sum(1 for n, _ in ex.calls if n == "klines") == 1


def test_fuente_caida_deja_null_sin_lanzar():
    rows = _collector(FakeBinance(fail={"premium", "spot_bt", ("taker", "AAAUSDT")})).collect(["AAA"], now=NOW)
    (a,) = rows
    assert a["funding_rate_est"] is None and a["basis_pct"] is None
    assert a["taker_buy_sell_ratio"] is None
    assert a["oi_change_1h_pct"] == pytest.approx(10.0)


def test_errores_seguidos_cortan_futures_data():
    ex = FakeBinance(fail={"oi", "taker", "top"})
    syms = [f"S{i}" for i in range(12)]
    _collector(ex).collect(syms, now=NOW)
    n_oi = sum(1 for n, _ in ex.calls if n == "oi")
    assert n_oi == mf.MAX_CONSECUTIVE_ERRORS


def test_sin_exchange_o_sin_simbolos():
    assert _collector(None).collect(["AAA"], now=NOW) == []
    assert _collector(FakeBinance()).collect([], now=NOW) == []


def test_upsert_idempotente_y_null_no_pisa(app):
    rows = _collector(FakeBinance()).collect(["AAA", "BBB"], now=NOW)
    assert mf.upsert_rows(db, rows) == 2
    # Re-scan en la misma hora con taker caído y un OI nuevo.
    ex = FakeBinance(fail={"taker"})
    ex.fapiDataGetOpenInterestHist = lambda p: [{"sumOpenInterest": "100"}, {"sumOpenInterest": "120"}]
    again = _collector(ex).collect(["AAA"], now=NOW + 60)
    assert mf.upsert_rows(db, again + again) == 1           # dedup en el lote
    got = _rows()
    assert len(got) == 2
    a = got[0]
    assert a.oi_change_1h_pct == pytest.approx(20.0)        # actualizado
    assert a.taker_buy_sell_ratio == pytest.approx(1.25)    # NULL no pisó
    # Hora siguiente → fila nueva.
    mf.upsert_rows(db, _collector(FakeBinance()).collect(["AAA"], now=NOW + 3600))
    assert len(_rows()) == 3


def test_purge_retencion(app):
    old = _collector(FakeBinance()).collect(["AAA"], now=NOW - (mf.RETENTION_DAYS + 1) * 86400)
    mf.upsert_rows(db, old + _collector(FakeBinance()).collect(["BBB"], now=NOW))
    assert mf.purge_old(db, now=NOW) == 1
    assert [r.symbol for r in _rows()] == ["BBB"]


def test_worker_no_tumba_el_scan_y_reintenta(app, monkeypatch):
    from scanner.worker import ScannerWorker
    w = ScannerWorker.__new__(ScannerWorker)
    w._flask_app, w._mf_collector, w._mf_purge_day = app, None, None
    w.exchange_manager = types.SimpleNamespace(get_exchange=lambda n: FakeBinance())
    monkeypatch.setattr(mf, "upsert_rows", lambda *a: (_ for _ in ()).throw(RuntimeError("db down")))
    w._mf_hour = 7
    w._store_market_features(["AAA"], hour=7)               # no lanza
    assert w._mf_hour is None                               # el próximo scan reintenta


def test_worker_una_vez_por_hora(app, monkeypatch):
    from scanner import worker as wk
    w = wk.ScannerWorker.__new__(wk.ScannerWorker)
    w._flask_app, w._mf_hour = app, None
    started = []
    monkeypatch.setattr(wk.threading, "Thread",
                        lambda target, args, daemon: types.SimpleNamespace(start=lambda: started.append(args)))
    opps = [types.SimpleNamespace(symbol="AAA", exchange="Binance"),
            types.SimpleNamespace(symbol="XXX", exchange="Bybit")]
    w._maybe_store_market_features(opps)
    w._maybe_store_market_features(opps)
    assert len(started) == 1 and started[0][0] == ["AAA"]


def _bar(open_ms, close, base, quote):
    return [open_ms, close, close, close, close, base, open_ms + 59_999, quote]


def test_vwap_window_and_fallback():
    end = HOUR * 1000
    bars = [_bar(end - 6 * 60_000, 9.0, 1, 9),            # fuera de la ventana
            *[_bar(end - k * 60_000, 2.0, 10, 21) for k in range(5, 0, -1)],
            _bar(end, 9.0, 1, 9)]                          # vela en curso, fuera
    win = mf.bars_before(bars, end)
    assert len(win) == 5
    assert mf.vwap(win) == pytest.approx(2.1)
    # sin volumen → media de cierres (minutos sin trades on-chain)
    assert mf.vwap([_bar(end - 60_000, 3.0, 0, 0), _bar(end - 120_000, 5.0, 0, 0)]) == 4.0
    assert mf.vwap([]) is None


class FakeAlphaBinance(FakeBinance):
    """CCC solo tiene spot on-chain (sin book CEX)."""

    def fapiPublicGetKlines(self, p):
        if p.get("interval") == "1m":
            end = p["endTime"] + 1
            return self._call("klines1m", [_bar(end - k * 60_000, 1.02, 100, 102) for k in range(5, 0, -1)], p)
        return super().fapiPublicGetKlines(p)


def test_alpha_leg_basis_from_onchain_vwap():
    ex = FakeAlphaBinance()
    seen = {}

    def alpha_klines(alpha_id, q):
        seen.update(q, alpha_id=alpha_id)
        end = q["endTime"] + 1
        return [_bar(end - k * 60_000, 1.0, 50, 50) for k in range(5, 0, -1)]

    col = mf.MarketFeatureCollector(
        types.SimpleNamespace(get_exchange=lambda n: ex), sleep=lambda s: None,
        alpha_loader=lambda: {"CCC": {"alpha_id": "ALPHA_9", "liquidity": 450000.0}},
        alpha_klines=alpha_klines)
    rows = {r["symbol"]: r for r in col.collect(["AAA", "CCC"], now=NOW)}
    assert rows["AAA"]["spot_source"] == "cex" and rows["AAA"]["alpha_liquidity_usd"] is None
    c = rows["CCC"]
    assert c["spot_source"] == "alpha" and c["alpha_liquidity_usd"] == 450000.0
    assert c["spot_mid"] == pytest.approx(1.0)
    assert c["basis_pct"] == pytest.approx(2.0)                  # perp VWAP 1.02 vs spot 1.00
    assert seen["alpha_id"] == "ALPHA_9" and seen["endTime"] == HOUR * 1000 - 1


def test_alpha_leg_failure_keeps_row():
    ex = FakeAlphaBinance()

    def boom(alpha_id, q):
        raise RuntimeError("alpha 503")

    col = mf.MarketFeatureCollector(
        types.SimpleNamespace(get_exchange=lambda n: ex), sleep=lambda s: None,
        alpha_loader=lambda: {"CCC": {"alpha_id": "ALPHA_9", "liquidity": 1.0}}, alpha_klines=boom)
    c = {r["symbol"]: r for r in col.collect(["CCC"], now=NOW)}["CCC"]
    assert c["spot_source"] == "alpha" and c["basis_pct"] is None and c["open_interest"] == 110.0
