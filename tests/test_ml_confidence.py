"""Confianza por oportunidad (analysis/ml_confidence) — sin red.

Fija: degradación segura sin artefacto, contrato de salida, tier por corte y
paridad de features settled_v1 aunque el modelo de ranking sea estimate_v0.
"""
import joblib
import numpy as np
import pytest

from analysis import arbitrage as arb
from analysis import ml_confidence as mc
from analysis import ml_features as mf
from analysis.indicators import compute_all_indicators
from tests.test_scan_quality import Cfg, FakeEM, _fr


@pytest.fixture(autouse=True)
def reset(monkeypatch):
    monkeypatch.setattr(mc, "_bundle", None)
    monkeypatch.setattr(arb, "calculate_spot_perp_fees",
                        lambda ex, cap, vol, symbol=None: {"total_cost": cap * 0.003})


def _artifact(path, cut=1e9, high_is_good=False, **over):
    from sklearn.ensemble import HistGradientBoostingClassifier, HistGradientBoostingRegressor
    rng = np.random.default_rng(0)
    X = rng.normal(size=(400, len(mf.FEATURE_NAMES)))
    y = X[:, 0] * 10 + rng.normal(size=400)
    reg = dict(max_iter=10, random_state=0)
    bundle = {
        "q10": HistGradientBoostingRegressor(loss="quantile", quantile=0.1, **reg).fit(X, y),
        "q50": HistGradientBoostingRegressor(loss="quantile", quantile=0.5, **reg).fit(X, y),
        "q90": HistGradientBoostingRegressor(loss="quantile", quantile=0.9, **reg).fit(X, y),
        "p5": HistGradientBoostingClassifier(**reg).fit(X, (y > 0).astype(int)),
        "conformal_q": 7.0, "tier_reach": {"alta": 0.85, "baja": 0.6},
        "tier_metric": "relw", "tier_high_is_good": high_is_good, "tier_cut": cut,
        "size_hi": 1.25, "size_lo": 0.75,
        "feature_names": mf.FEATURE_NAMES, "feature_spec": "settled_v1", "model_version": "test",
        **over,
    }
    joblib.dump(bundle, path)
    return path


PARAMS = {"cv": 0.3, "min_ratio": 0.5, "streak": 10, "pct": 90, "volume": 5e6,
          "settlement_avg": 0.0003, "payments_per_day": 3, "current_rate": 0.0004}


def test_missing_artifact_degrades_to_none(tmp_path):
    assert mc.load_model(str(tmp_path / "nope.joblib")) is False
    assert mc.predict_confidence(PARAMS, {}) is None


def test_rejects_wrong_spec(tmp_path):
    assert mc.load_model(str(_artifact(tmp_path / "m.joblib", feature_spec="estimate_v0"))) is False
    assert not mc.is_loaded()


def test_output_contract_and_tiers(tmp_path):
    assert mc.load_model(str(_artifact(tmp_path / "a.joblib", cut=1e9)))   # relw ≤ 1e9 → alta
    out = mc.predict_confidence(PARAMS, {})
    assert set(out) == {"confidence", "apr_p10", "apr_p90", "confidence_tier", "size_mult", "rel_width"}
    assert out["apr_p10"] <= out["apr_p90"]
    assert out["confidence_tier"] == "alta" and out["confidence"] == 0.85 and out["size_mult"] == 1.25
    mc.load_model(str(_artifact(tmp_path / "b.joblib", cut=-1.0)))          # nada ≤ −1 → baja
    out = mc.predict_confidence(PARAMS, {})
    assert out["confidence_tier"] == "baja" and out["confidence"] == 0.6 and out["size_mult"] == 0.75


def test_scan_uses_settled_v1_params_for_confidence(monkeypatch):
    """Con el modelo de ranking en estimate_v0, la confianza igual recibe la
    ventana de 30 liquidaciones previas + última liquidada (como el entrenamiento)."""
    from analysis import ml_scorer
    seen = {}
    monkeypatch.setattr(ml_scorer, "_feature_spec", "estimate_v0")
    monkeypatch.setattr(ml_scorer, "predict_score", lambda p, ind: (60, 25.0))
    monkeypatch.setattr(mc, "is_loaded", lambda: True)
    monkeypatch.setattr(mc, "predict_confidence", lambda p, ind: seen.update(
        vec=mf.build_feature_vector(p, ind)) or {"confidence": 0.8, "apr_p10": -10.0, "apr_p90": 90.0,
                                                 "confidence_tier": "alta", "size_mult": 1.25, "rel_width": 1.2})
    hist = [0.0001 * (1 + (i % 5) / 10) * (-1 if i == 20 else 1) for i in range(45)]
    fr = _fr("AAA", 0.0009, ih=4)
    opp = arb.ArbitrageScanner(FakeEM(hist), Cfg())._analyze_spot_perp(fr)
    assert opp.confidence == 0.8 and opp.confidence_tier == "alta"
    assert opp.to_dict()["apr_p90"] == 90.0
    window, cur = hist[-(mf.LOOKBACK + 1):-1], hist[-1]
    ws = mf.window_stats(window)
    expected = mf.build_feature_vector(
        {"cv": ws["cv"], "min_ratio": ws["min_ratio"], "streak": ws["streak"], "pct": ws["pct"],
         "volume": fr.volume_24h, "settlement_avg": ws["settlement_avg"],
         "payments_per_day": fr.payments_per_day, "current_rate": cur},
        compute_all_indicators(abs(cur), window))
    assert seen["vec"] == pytest.approx(expected)


def test_no_confidence_when_model_expects_loss(monkeypatch):
    from analysis import ml_scorer
    called = []
    monkeypatch.setattr(ml_scorer, "predict_score", lambda p, ind: (10, -4.0))
    monkeypatch.setattr(mc, "is_loaded", lambda: True)
    monkeypatch.setattr(mc, "predict_confidence", lambda *a: called.append(1))
    hist = [0.0001] * 40
    opp = arb.ArbitrageScanner(FakeEM(hist), Cfg())._analyze_spot_perp(_fr("AAA", 0.0002))
    assert opp.confidence is None and not called
