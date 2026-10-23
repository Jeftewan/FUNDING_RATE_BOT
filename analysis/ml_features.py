"""Shared feature-builder for the ML scoring model — PARIDAD por construcción.

El modelo se entrena offline (scripts/ml_train.py) y predice en producción
(analysis/ml_scorer.py). Si los features difieren entre ambos lados, la
predicción se rompe silenciosamente. Este módulo es la ÚNICA fuente de verdad
del vector de features y lo usan los DOS lados, garantizando paridad.

Decisiones de paridad (verificadas en la exploración del plan):
  * Los indicadores (z, momentum, percentile) salen de
    analysis.indicators.compute_all_indicators, que internamente toma abs() de
    todo → el signo del rate no afecta. Prod y offline producen el MISMO valor.
  * fee_drag NO se toma del orderbook (no reconstruible offline). Se computa de
    forma DETERMINISTA desde settlement_avg y ppd, idéntico en ambos lados
    (misma fórmula que scripts/_scoring_data.py:estimate_fee_drag).

Sin dependencias pesadas (solo stdlib) — importable desde prod sin sklearn.
"""

# Orden FIJO del vector. NO reordenar sin re-entrenar: el .joblib guarda este
# mismo orden y el modelo asume posiciones, no nombres.
FEATURE_NAMES = [
    "cv", "min_ratio", "streak", "pct", "volume", "settlement_avg", "ppd",
    "fee_drag_det", "current_rate_abs", "reality_ratio",
    "z_value", "mom_points", "pctl_percentile", "pctl_points",
]

# Fee round-trip (spot 0.10%×2 + perp 0.05%×2 = 0.30% taker) y horizonte de
# hold usado para el fee_drag determinista. Idénticos a
# scripts/_scoring_data.py:estimate_fee_drag (hold_days=30) → paridad con el
# fee_drag que ya trae extract_features offline.
ROUND_TRIP_FEE = 0.003
HOLD_DAYS_FEE = 30

# Ventana de liquidaciones para los features de historia. MISMO valor que
# scripts/scoring_optimizer.py:LOOKBACK (con el que se entrenó el modelo y se
# optimizó el heurístico v11.0). Antes prod usaba max(pagos_3d+5, 15): 15
# muestras en 8h, 23 en 4h, 77 en 1h → streak/pct/cv fuera de distribución.
LOOKBACK = 30
MIN_NONZERO = 5   # gate: < 5 tasas no triviales → sin features (thin history)


def window_stats(hist: list) -> dict | None:
    """Estadísticos de la ventana de liquidaciones `hist` (oldest→newest, ya
    recortada a LOOKBACK). Fuente única para prod y offline
    (scripts/_scoring_data.py:base_window_features la usa).

    Devuelve {cv, min_ratio, streak, pct, settlement_avg} o None si la ventana
    no tiene ≥ MIN_NONZERO tasas con magnitud no trivial.
    """
    import statistics
    abs_hist = [abs(r) for r in hist if abs(r) > 1e-12]
    if len(abs_hist) < MIN_NONZERO:
        return None
    mean_h = statistics.mean(abs_hist)
    std_h = statistics.stdev(abs_hist) if len(abs_hist) > 1 else 0.0
    streak = 0
    for r in reversed(hist):            # positivos consecutivos desde el final
        if r > 0:
            streak += 1
        else:
            break
    return {
        "cv": std_h / mean_h if mean_h > 1e-12 else 999.0,
        "min_ratio": min(abs_hist) / mean_h if mean_h > 1e-12 else 0.0,
        "streak": streak,
        "pct": sum(1 for r in hist if r > 0) / len(hist) * 100,
        "settlement_avg": mean_h,
    }


def fee_drag_deterministic(settlement_avg: float, ppd: float) -> float:
    """fee_drag = fee round-trip / revenue esperado en HOLD_DAYS_FEE días.

    Determinista (solo settlement_avg y ppd), NO depende del orderbook → mismo
    valor offline y en prod. Espeja estimate_fee_drag del backtest.
    """
    revenue = abs(settlement_avg) * ppd * HOLD_DAYS_FEE
    if revenue < 1e-10:
        return 1.0
    return min(ROUND_TRIP_FEE / revenue, 1.0)


def _indicator_scalars(indicators: dict) -> tuple:
    """Extrae (z, mom_points, pctl_percentile, pctl_points) del dict de
    compute_all_indicators (formato rico de prod). Defaults neutros idénticos
    a los de scripts/scoring_optimizer.py:extract_features.
    """
    indicators = indicators or {}
    z = indicators.get("z_score", {}) or {}
    mom = indicators.get("momentum", {}) or {}
    pctl = indicators.get("percentile", {}) or {}

    z_val = z.get("z", 0)
    mom_pts = mom.get("points", 3)
    pctl_pct = pctl.get("percentile", 50)
    pctl_pts = pctl.get("points", 3)
    return (
        float(z_val if z_val is not None else 0),
        float(mom_pts if mom_pts is not None else 3),
        float(pctl_pct if pctl_pct is not None else 50),
        float(pctl_pts if pctl_pts is not None else 3),
    )


def build_feature_vector(params: dict, indicators: dict) -> list:
    """Construye el vector de features en el orden de FEATURE_NAMES.

    `params` es el MISMO dict que analysis/arbitrage.py arma para
    opportunity_score (claves: cv, min_ratio, streak, pct, volume,
    settlement_avg, payments_per_day/ppd, current_rate). En offline se pasa una
    fila equivalente. `indicators` es el dict de compute_all_indicators
    (params["_indicators"] en prod; reconstruido por fila en ml_train).

    fee_drag y reality_ratio se DERIVAN aquí (no se leen de params) para
    garantizar que el modelo use exactamente los mismos valores en ambos lados.
    """
    cv = float(params.get("cv", 999))
    min_ratio = float(params.get("min_ratio", 0))
    streak = float(params.get("streak", 0))
    pct = float(params.get("pct", 0))
    volume = float(params.get("volume", 0) or 0)
    settlement_avg = abs(float(params.get("settlement_avg", 0)))
    ppd = float(params.get("payments_per_day", params.get("ppd", 3)) or 3)
    current_rate_abs = abs(float(params.get("current_rate", 0)))

    fee_drag_det = fee_drag_deterministic(settlement_avg, ppd)
    reality_ratio = (current_rate_abs / settlement_avg
                     if settlement_avg > 1e-12 else 0.0)

    z_value, mom_points, pctl_percentile, pctl_points = _indicator_scalars(indicators)

    return [
        cv, min_ratio, streak, pct, volume, settlement_avg, ppd,
        fee_drag_det, current_rate_abs, reality_ratio,
        z_value, mom_points, pctl_percentile, pctl_points,
    ]
