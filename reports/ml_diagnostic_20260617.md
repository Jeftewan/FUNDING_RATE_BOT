# ML diagnostic — techo de predictibilidad vs heurístico v11.0 — 2026-06-17 09:47

**Target:** `net_apr` (APR-neto, lo que el scoring optimiza). **Split:** temporal
(test = último 1/3, desde 2026-05-19). **Features ML:** 14 crudos/indicadores
(sin columnas derivadas del score → sin leakage). **Modelo:** GradientBoostingRegressor.

> Diagnóstico local, NO producción. Mide si un modelo flexible predice net_apr
> mejor que el heurístico, y qué features pesan. Prod no tiene numpy/sklearn.

## 1. Rank IC (Spearman pred ↔ net_apr) en test

|  | Rank IC (test) |
| --- | --- |
| Heurístico v11.0 | 0.638 |
| GradientBoosting | 0.737 |
| **Uplift (ML − heur)** | **+0.099** |

**Umbral de decisión:** uplift < 0.05 → quedarse heurístico (barato, interpretable,
sin deps en prod) y usar las importances para afinar pesos. Uplift ≥ 0.05 y estable
→ considerar híbrido (ML offline re-deriva pesos), no inferencia sklearn online.

**Veredicto:** CONSIDERAR ML (verificar estabilidad/decil top antes de comprometer prod)

## 2. Backtest por decil en test (net_apr medio | % rentable)

### Ordenado por score heurístico v11.0
| decil | n | net_apr | % rent |
| --- | --- | --- | --- |
| 1 | 30433.0 | -86.8 | 1% |
| 2 | 30432.0 | -81.5 | 3% |
| 3 | 30432.0 | -78.8 | 4% |
| 4 | 30432.0 | -70.1 | 8% |
| 5 | 30433.0 | -54.2 | 14% |
| 6 | 30432.0 | -24.8 | 8% |
| 7 | 30432.0 | -11.6 | 3% |
| 8 | 30432.0 | -25.8 | 41% |
| 9 | 30432.0 | -13.3 | 54% |
| 10 | 30433.0 | -1.0 | 64% |

### Ordenado por predicción ML
| decil | n | net_apr | % rent |
| --- | --- | --- | --- |
| 1 | 30433.0 | -96.2 | 1% |
| 2 | 30432.0 | -90.5 | 1% |
| 3 | 30432.0 | -81.9 | 2% |
| 4 | 30432.0 | -70.3 | 5% |
| 5 | 30433.0 | -53.4 | 12% |
| 6 | 30432.0 | -35.9 | 20% |
| 7 | 30432.0 | -13.0 | 49% |
| 8 | 30432.0 | -8.9 | 28% |
| 9 | 30432.0 | -7.6 | 9% |
| 10 | 30433.0 | 9.9 | 73% |

Top decil — heurístico net_apr -1.0 (64% rent) vs
ML 9.9 (73% rent).

## 3. Feature importances (GradientBoosting)

| feature | importance |
| --- | --- |
| pct | 0.430 |
| streak | 0.261 |
| fee_drag | 0.145 |
| current_rate | 0.084 |
| ppd | 0.041 |
| pctl_percentile | 0.016 |
| settlement_avg | 0.006 |
| volume | 0.005 |
| cv | 0.005 |
| z_value | 0.003 |
| reality_ratio | 0.002 |
| min_ratio | 0.002 |
| mom_points | 0.000 |
| pctl_points | 0.000 |

Accionable aun sin adoptar ML: las features de mayor importancia indican qué
dimensiones del heurístico merecen más peso (cruzar con los pesos v11.0).
