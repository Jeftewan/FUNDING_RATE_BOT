# ML train — modelo de scoring para producción — 2026-09-13 18:00

**Artefacto:** `models/scoring_model.joblib` (version `20260913`, 0.41 MB).
**Datos:** 1,438,228 filas, 2026-06-16 → 2026-09-12.
**Modelo:** GradientBoostingRegressor{'n_estimators': 300, 'max_depth': 3, 'learning_rate': 0.05, 'subsample': 0.7, 'random_state': 42}. **Target:** `net_apr`.
**scikit-learn:** 1.9.0 (debe coincidir EXACTO con requirements.txt de prod).

> Local-only. Entrena + valida + exporta; NO despliega. Para promover:
> `git add models/scoring_model.joblib && git commit && git push` → Railway redeploya.

## 1. Validación del modelo VIVO (predicciones previas ↔ net_apr real)

IC en vivo 0.391 sobre 14038 predicciones (modelo 20260710). Compará contra el uplift esperado; una caída fuerte = drift.

## 2. Walk-forward — modelo nuevo vs heurístico v11.0 (6 folds)

Por fold: rank-IC (robustez estadística) + net_apr medio del top-10% por ranking
(robustez ECONÓMICA — cuánta plata gana ordenar por modelo vs por heurístico).

| Ventana test | n_test | IC heur | IC ML | Uplift | net_apr top10 heur | net_apr top10 ML | Δtop10 | Δtop1% |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| 2026-07-31→2026-08-07 | 121781 | 0.659 | 0.786 | +0.126 | 26.1 | 27.4 | +1.3 | +8.9 |
| 2026-08-07→2026-08-14 | 120397 | 0.657 | 0.773 | +0.116 | 16.5 | 23.8 | +7.3 | +17.1 |
| 2026-08-14→2026-08-21 | 120055 | 0.654 | 0.707 | +0.054 | 16.2 | 19.1 | +2.9 | +4.9 |
| 2026-08-21→2026-08-28 | 121659 | 0.545 | 0.653 | +0.108 | 4.1 | 13.3 | +9.2 | +18.4 |
| 2026-08-28→2026-09-04 | 112238 | 0.65 | 0.764 | +0.114 | 14.0 | 17.9 | +3.9 | +8.8 |
| 2026-09-04→2026-09-11 | 53368 | 0.579 | 0.731 | +0.152 | 8.7 | 8.9 | +0.2 | +6.4 |

| Métrica | Valor |
| --- | --- |
| Uplift IC medio | +0.112 |
| Uplift IC σ | 0.029 |
| Positivo en todos los folds | Sí |
| Δ net_apr top-decil medio (ML − heur) | +4.1 |
| Δ net_apr top-1% medio (ML − heur) | +10.8 |

**net_apr está en % anualizado.** El Δtop es lo que subiría tu rendimiento si
tradeás las mejores oportunidades rankeadas por el modelo en vez de por el score.

Umbral PROMOVER: uplift IC>0 en todos los folds, medio ≥0.05, σ≤0.05, **y**
Δ net_apr top-decil medio > 0 (la mejora estadística se traduce en ganancia).

## 3. Feature importances (modelo final)

| feature | importance |
| --- | --- |
| streak | 0.546 |
| pct | 0.287 |
| fee_drag_det | 0.068 |
| ppd | 0.039 |
| settlement_avg | 0.015 |
| current_rate_abs | 0.009 |
| cv | 0.008 |
| min_ratio | 0.008 |
| pctl_percentile | 0.007 |
| z_value | 0.007 |
| reality_ratio | 0.004 |
| volume | 0.003 |
| mom_points | 0.000 |
| pctl_points | 0.000 |

## 4. Veredicto

**PROMOVER — el modelo bate al heurístico de forma estable (IC) Y eleva el net_apr del top (económico); commit + push.**

Recordatorio: el `.joblib` debe cargarse con la MISMA versión de scikit-learn
(1.9.0) que lo creó — está pinneada en requirements.txt y
requirements-dev.txt. Un mismatch rompe `joblib.load` en Railway.
