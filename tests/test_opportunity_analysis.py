"""Panel de analistas, rachas, features de señales y armado del análisis (sin red)."""
import math

from analysis import analysts as an
from analysis import opportunity_analysis as oa
from analysis.signal_features import SIGNAL_FEATURES, signal_features

H = 3600
T8 = 8 * H * 1000


# ── Rachas de cobro ────────────────────────────────────────────────────────
def test_episode_starts_at_base_and_ends_after_two_non_positive():
    base = 0.0001                                 # 0.01 %/8h = tasa base
    rates = [0.00005, base, 0.0003, 0.0, -0.0001, 0.00005, 0.0002, 0.0002]
    ts = [i * T8 for i in range(len(rates))]
    eps = an.funding_episodes(rates, ts, 8)
    assert len(eps) == 2
    first, second = eps
    assert first["ended"] and first["start_ts"] == ts[1] and first["end_ts"] == ts[4]
    assert first["payments"] == 4 and math.isclose(first["days"], 4 / 3, abs_tol=0.01)
    assert not second["ended"] and second["start_ts"] == ts[6]


def test_one_non_positive_payment_does_not_end_episode():
    rates = [0.0002, 0.0, 0.0002, 0.0002]
    eps = an.funding_episodes(rates, [i * T8 for i in range(4)], 8)
    assert len(eps) == 1 and not eps[0]["ended"] and eps[0]["payments"] == 4


def test_episode_stats_median_and_age():
    day = 86_400_000
    eps = [{"start_ts": 0, "end_ts": 2 * day, "days": 2.0, "ended": True},
           {"start_ts": 3 * day, "end_ts": 7 * day, "days": 4.0, "ended": True},
           {"start_ts": 8 * day, "end_ts": 9 * day, "days": 1.0, "ended": False}]
    st = an.episode_stats(eps, 10 * day)
    assert st["ep_med_days"] == 3.0 and st["ep_count"] == 2 and st["ep_age_days"] == 2.0


# ── Votos y consenso ───────────────────────────────────────────────────────
TABLE = {
    "version": "test",
    "analysts": {
        "modelo": {"feature": "p_prod", "label": "Modelo", "cuts": [10, 30], "dir": 1, "weight": 0.2, "votes": True},
        "top": {"feature": "top_ls_24h", "label": "Top", "cuts": [1.0, 1.5], "dir": -1, "weight": 0.1, "votes": True},
        "vol": {"feature": "volatility_7d", "label": "Vol", "cuts": [0.03, 0.06], "dir": -1, "weight": 0.5, "votes": False},
    },
    "consensus_calib": [{"lo": 0, "hi": 5.0, "n": 10, "reach5": 0.4}, {"lo": 5.0, "hi": 10.01, "n": 20, "reach5": 0.8}],
    "analogs": {"pred_cuts": [5, 15, 30, 60], "relw_cuts": [1.0, 2.0],
                "cells": {"a1_p2_w0": {"n": 50, "reach5": 0.7}, "a1_p2_wx": {"n": 90, "reach5": 0.6}}},
}


def test_vote_respects_cuts_and_direction():
    m, t = TABLE["analysts"]["modelo"], TABLE["analysts"]["top"]
    assert an.vote(m, 40) == 1 and an.vote(m, 20) == 0 and an.vote(m, 5) == -1
    assert an.vote(t, 2.0) == -1 and an.vote(t, 0.8) == 1     # más bajo es mejor
    assert an.vote(m, None) is None and an.vote(m, "x") is None


def test_consensus_ignores_non_voting_and_missing():
    votes = {"modelo": 1, "top": -1, "vol": -1}
    # (0.2·1 + 0.1·(−1)) / 0.3 = 1/3 → 5.5 + 1.5 = 7.0; "vol" no vota
    assert an.consensus(votes, TABLE["analysts"]) == 7.0
    assert an.consensus({"modelo": None, "top": None}, TABLE["analysts"]) is None
    assert an.consensus({"modelo": 1, "top": None}, TABLE["analysts"]) == 10.0


def test_evaluate_calibration_and_analogs():
    ev = an.evaluate({"p_prod": 40, "top_ls_24h": 1.2, "volatility_7d": 0.08}, TABLE)
    assert ev["consensus"] == 8.5 and ev["label"] == "Muy favorable"   # top neutral (0)
    assert ev["calib"]["reach5"] == 0.8
    assert {a["key"]: a["vote"] for a in ev["analysts"]} == {"modelo": 1, "top": 0, "vol": -1}
    assert an.analog_cell(20, True, 0.5, TABLE)["n"] == 50          # p: 20 ≥ 5,15 → 2; w: 0
    assert an.analog_cell(20, True, None, TABLE)["n"] == 90         # sin ancho → celda gruesa
    assert an.analog_cell(20, False, 0.5, TABLE) is None


def test_load_table_missing_file_is_safe(tmp_path):
    assert an.load_table(str(tmp_path / "nope.json")) is False
    assert an.table() is None


# ── Features de señales ────────────────────────────────────────────────────
def _rows(n=80, basis=0.1, oi=1000.0):
    return [{"ts_hour": k * H, "basis_pct": basis + (0.1 if k >= n - 24 else 0.0),
             "open_interest": oi * (1.1 if k == n - 1 else 1.0), "open_interest_usd": 1e6,
             "top_long_short_pos_ratio": 1.5, "volatility_7d": 0.04} for k in range(n)]


def test_signal_features_windows():
    rows = _rows()
    f = signal_features(rows, 79 * H + 120)          # dentro de la hora 79
    assert set(f) == set(SIGNAL_FEATURES)
    assert math.isclose(f["basis_24h"], 0.2) and math.isclose(f["basis_chg_24h"], 0.1)
    assert math.isclose(f["log_oi_usd"], 6.0) and math.isclose(f["oi_chg_24h_pct"], 10.0, rel_tol=1e-9)
    assert f["top_ls_24h"] == 1.5 and f["volatility_7d"] == 0.04


def test_signal_features_no_lookahead_and_thin():
    rows = _rows()
    f = signal_features(rows, 40 * H)
    assert math.isclose(f["basis_24h"], 0.1)          # el salto de basis (horas ≥ 56) aún no ocurrió
    thin = signal_features(rows[:5], 4 * H)
    assert thin["basis_24h"] is None and thin["basis_chg_24h"] is None
    assert signal_features([], 0) == {k: None for k in SIGNAL_FEATURES}


# ── Armado del análisis ────────────────────────────────────────────────────
def _kline(t, close, base_vol=10.0):
    return [t, close, close, close, close, base_vol, t + H * 1000 - 1, base_vol * close]


def test_hourly_rows_basis_cex_and_alpha():
    t0 = 1_700_000_000_000 // (H * 1000) * (H * 1000)
    perp = {t0: _kline(t0, 101.0)}
    spot = {t0: _kline(t0, 100.0)}
    rows = oa.hourly_rows(perp, spot, {(t0 + H * 1000) // 1000: (5.0, 500.0, 1.2)}, alpha=False)
    assert len(rows) == 1 and math.isclose(rows[0]["basis_pct"], 1.0)
    assert rows[0]["ts_hour"] == (t0 + H * 1000) // 1000 and rows[0]["open_interest_usd"] == 500.0
    rows_a = oa.hourly_rows(perp, spot, {}, alpha=True)        # VWAP = cierre (vela plana)
    assert math.isclose(rows_a[0]["basis_pct"], 1.0)
    assert oa.hourly_rows(perp, {t0: _kline(t0, 100.0, 0.0)}, {}, alpha=True)[0]["basis_pct"] is None


class _FakeEx:
    def __init__(self, fail=False):
        self.fail = fail

    def fapiPublicGetFundingRate(self, p):
        if self.fail:
            raise RuntimeError("down")
        return [{"fundingTime": i * T8, "fundingRate": "0.0002"} for i in range(40)]

    def fapiPublicGetKlines(self, p):
        return [_kline(k * H * 1000, 101.0) for k in range(50)]

    def publicGetKlines(self, p):
        return [_kline(k * H * 1000, 100.0) for k in range(50)]

    def fapiDataGetOpenInterestHist(self, p):
        raise RuntimeError("451")

    def fapiDataGetTopLongShortPositionRatio(self, p):
        return []


class _FakeMgr:
    def __init__(self, ex):
        self.ex = ex

    def get_exchange(self, name):
        return self.ex


OPP = {"_id": "XUSDT_binance", "symbol": "X", "exchange": "Binance", "spot_venue": "binance",
       "interval_hours": 8, "model_prediction": 25.0, "rel_width": 1.2, "apr_p10": -5, "apr_p90": 60,
       "next_funding_ts": 123}


def test_build_with_failing_sources_degrades(monkeypatch):
    monkeypatch.setattr(an, "_table", TABLE)
    res = oa.build(OPP, _FakeMgr(_FakeEx(fail=True)), all_data=[], capital=1000)
    assert res["funding"] is None and res["market"] is None      # funding y OI caídos
    assert res["price"] and math.isclose(res["price"]["basis"][-1], 1.0)
    assert res["panel"]["consensus"] is not None                  # el modelo vota igual
    res2 = oa.build(OPP, _FakeMgr(_FakeEx()), all_data=[{"exchange": "Binance", "fr": 0.0001, "ipd": 3}])
    assert res2["funding"]["apr"][0] == 21.9 and res2["values"]["streak"] == 30
    assert res2["values"]["regime_median_daily"] == 0.0003
    assert oa.build(OPP, None)["funding"] is None


def test_cache_by_settlement(monkeypatch):
    calls = []
    monkeypatch.setattr(oa, "build", lambda *a, **k: calls.append(1) or {"ok": 1})
    oa._cache.clear()
    assert oa.get(OPP, None)[1] is False and oa.get(OPP, None)[1] is True
    assert oa.get({**OPP, "next_funding_ts": 456}, None)[1] is False and len(calls) == 2


def test_size_mult_only_when_validated():
    assert an.size_mult(7.0, TABLE) is None                      # sin size_by_consensus
    t = {**TABLE, "size_by_consensus": True}
    assert an.size_mult(7.0, t) == 1.25 and an.size_mult(5.5, t) == 1.0 and an.size_mult(4.0, t) == 0.75
    assert an.size_mult(None, t) is None


def test_worker_attaches_consensus_without_db(monkeypatch):
    from scanner.worker import ScannerWorker
    monkeypatch.setattr(an, "_table", {**TABLE, "size_by_consensus": True})
    w = ScannerWorker.__new__(ScannerWorker)
    w._flask_app = None
    opps = [{"mode": "spot_perp", "exchange": "Binance", "symbol": "X", "model_prediction": 40.0,
             "rel_width": 1.0, "history": {"streak": 30, "pct": 100}, "size_mult": 0.75},
            {"mode": "spot_perp", "exchange": "Binance", "symbol": "Y", "model_prediction": None}]
    w._attach_consensus(opps, [{"exchange": "Binance", "fr": 0.0001, "ipd": 3}])
    assert opps[0]["consensus"] == 10.0 and opps[0]["size_mult"] == 1.25
    assert opps[0]["consensus_votes"] == "1/1" and "consensus" not in opps[1]
