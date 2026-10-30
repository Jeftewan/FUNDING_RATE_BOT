"""Prod-side: confianza por oportunidad — P(APR neto ≥ 5 %) y rango p10–p90.

Segunda dimensión junto al APR esperado del modelo de ranking (analysis/ml_scorer):
NO cambia el orden; se muestra en la tarjeta y sugiere el capital (tier Alta/Baja).
Entrenado y calibrado sobre los picks de la cartera en scripts/confidence_study.py
(reports/confidence_YYYYMMDD.md).

Mismo guardrail que ml_scorer: singleton cargado al startup, import perezoso de
joblib, cualquier fallo → None (el scan nunca cae y la tarjeta no muestra nada).
"""
import logging

from analysis.ml_features import build_feature_vector, FEATURE_NAMES

log = logging.getLogger("bot")

_bundle = None
model_version = None
_REQUIRED = ("q10", "q50", "q90", "p5", "conformal_q", "tier_reach",
             "tier_metric", "tier_high_is_good", "tier_cut", "size_hi", "size_lo")


def load_model(path: str) -> bool:
    """Carga models/confidence_model.joblib. True si quedó operativo."""
    global _bundle, model_version
    try:
        import os
        if not os.path.exists(path):
            log.warning(f"Confidence model not found at {path} — sin confianza por oportunidad")
            return False
        import joblib
        bundle = joblib.load(path)
        missing = [k for k in _REQUIRED if k not in bundle]
        if missing:
            log.error(f"Confidence model incompleto (faltan {missing}) — desactivado.")
            return False
        if list(bundle.get("feature_names", [])) != FEATURE_NAMES:
            log.error("Confidence model feature mismatch — re-entrenar. Desactivado.")
            return False
        if bundle.get("feature_spec") != "settled_v1":
            log.error(f"Confidence model feature_spec {bundle.get('feature_spec')} no soportado — desactivado.")
            return False
        _bundle = bundle
        model_version = bundle.get("model_version")
        log.info(f"Confidence model loaded: version={model_version} tier={bundle['tier_metric']}")
        return True
    except Exception as e:
        log.error(f"Failed to load confidence model ({path}): {e}")
        _bundle = None
        model_version = None
        return False


def is_loaded() -> bool:
    return _bundle is not None


def predict_confidence(params: dict, indicators: dict):
    """Devuelve {confidence, apr_p10, apr_p90, confidence_tier, size_mult, rel_width} o None.

    `params` deben ser los de spec settled_v1 (ArbitrageScanner._model_params).
    `confidence` = frecuencia real de APR neto ≥ 5 % del tier en los picks del
    backtest (calibrada por construcción); `rel_width` = ancho relativo de la
    banda (menor = más certeza), útil para ordenar dentro de un tier.
    """
    if _bundle is None:
        return None
    try:
        b = _bundle
        vec = [build_feature_vector(params, indicators)]
        q10, q50, q90 = sorted(float(b[k].predict(vec)[0]) for k in ("q10", "q50", "q90"))
        rel_width = (q90 - q10) / max(abs(q50), 5.0)
        metric = float(b["p5"].predict_proba(vec)[0][1]) if b["tier_metric"] == "p5" else rel_width
        good = metric >= b["tier_cut"] if b["tier_high_is_good"] else metric <= b["tier_cut"]
        tier = "alta" if good else "baja"
        q = float(b["conformal_q"])
        return {
            "confidence": float(b["tier_reach"][tier]),
            "apr_p10": round(q10 - q, 1),
            "apr_p90": round(q90 + q, 1),
            "confidence_tier": tier,
            "size_mult": float(b["size_hi"] if good else b["size_lo"]),
            "rel_width": round(rel_width, 3),
        }
    except Exception as e:
        log.warning(f"ML predict_confidence failed: {e}")
        return None
