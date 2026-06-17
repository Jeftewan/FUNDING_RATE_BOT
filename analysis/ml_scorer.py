"""Prod-side ML scorer — carga el modelo entrenado y rankea oportunidades.

Singleton de módulo: load_model() se llama una vez al startup (app.py) y deja
el modelo + la calibración en variables de módulo. predict_score() arma el
vector con el feature-builder COMPARTIDO (analysis/ml_features) y devuelve un
score 0–100 calibrado, o None si no hay modelo / falla la predicción.

Guardrail (caja negra que influye en el ranking que ve el usuario): cualquier
fallo → None → el caller usa el score heurístico v11.0 como fallback. El modelo
nunca tumba un scan.

Dependencias pesadas (joblib/sklearn) se importan PEREZOSAMENTE dentro de
load_model; si no están instaladas, el módulo sigue importable y prod cae al
heurístico.
"""
import bisect
import logging

from analysis.ml_features import build_feature_vector, FEATURE_NAMES

log = logging.getLogger("bot")

# ── Singleton state ──────────────────────────────────────────────────────────
_model = None
_calibration = None          # lista ordenada asc de percentiles p0..p100 de las
                             # predicciones de train → mapea pred a score 0–100.
_feature_names = None
model_version = None         # fecha del modelo (str) o None — se loguea/persiste.


def load_model(path: str) -> bool:
    """Carga el artefacto .joblib. Devuelve True si quedó operativo.

    El artefacto es un dict: {model, calibration_pcts, feature_names,
    model_version, train_window, val_metrics}. Si falta el archivo o la versión
    de sklearn no coincide (pickle incompatible), loguea y devuelve False — prod
    sigue con el heurístico.
    """
    global _model, _calibration, _feature_names, model_version
    try:
        import os
        if not os.path.exists(path):
            log.warning(f"ML model not found at {path} — usando heurístico v11.0")
            return False
        import joblib
        bundle = joblib.load(path)
        model = bundle["model"]
        calib = list(bundle["calibration_pcts"])
        feats = list(bundle["feature_names"])

        if feats != FEATURE_NAMES:
            log.error(
                "ML model feature mismatch: el artefacto fue entrenado con un "
                f"orden de features distinto ({len(feats)} vs {len(FEATURE_NAMES)}). "
                "Re-entrenar. Usando heurístico."
            )
            return False

        _model = model
        _calibration = calib
        _feature_names = feats
        model_version = bundle.get("model_version")
        log.info(
            f"ML model loaded: version={model_version} "
            f"features={len(feats)} calib_pts={len(calib)}"
        )
        return True
    except Exception as e:
        log.error(f"Failed to load ML model ({path}): {e} — usando heurístico v11.0")
        _model = None
        _calibration = None
        _feature_names = None
        model_version = None
        return False


def is_loaded() -> bool:
    return _model is not None


def _calibrate(pred: float) -> int:
    """Mapea la predicción cruda (net_apr) a score 0–100 vía los percentiles
    de train. bisect cuenta cuántos puntos de calibración quedan por debajo."""
    if not _calibration:
        return 0
    score = bisect.bisect_right(_calibration, pred)
    # _calibration tiene 101 puntos (p0..p100) → score ya cae en [0,100].
    return max(0, min(score, 100))


def predict_score(params: dict, indicators: dict):
    """Devuelve (model_score:int 0–100, model_pred:float) o None.

    None cuando no hay modelo o la predicción lanza → el caller usa el
    heurístico. Nunca propaga la excepción.
    """
    if _model is None:
        return None
    try:
        vec = build_feature_vector(params, indicators)
        pred = float(_model.predict([vec])[0])
        return _calibrate(pred), pred
    except Exception as e:
        log.warning(f"ML predict_score failed ({params.get('mode', '?')}): {e}")
        return None
