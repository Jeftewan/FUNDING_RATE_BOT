# Optimizer Report — baseline v10.6 vs candidato

**Fecha:** 2026-06-17 09:02
**Trials:** 600
**Train:** 824,904 rows (hasta 2026-05-19) | **Val:** 304,323 rows (desde 2026-05-20)
**Objetivo:** APR-neto (velocidad de capital) ajustado a riesgo | **Hold máx:** 336h | **Fee:** 0.30% | **Alcance:** spot_perp

> El optimizador SOLO genera este reporte + un candidato. No aplica nada. Revisa abajo y, si convence, copia el candidato a `analysis/scoring.py` a mano.

## 1. Métricas (validación)

| Métrica | v10.6 Train | v10.6 Val | Cand. Train | Cand. Val | Δ Val |
| :---: | :---: | :---: | :---: | :---: | :---: |
| Spearman (Net APR) | 0.575 | 0.623 | 0.624 | 0.638 | 0.015 |
| Sharpe top (APR-neto) | -0.376 | -0.266 | 0.173 | -0.119 | 0.147 |
| Net APR top | -22.4 | -13.2 | 18.1 | -7.5 | 5.7 |
| Monotonicity | 0.88 | 1.00 | 1.00 | 1.00 | 0.00 |
| Profit Rate Top | 43% | 55% | 72% | 59% | +4pp |
| Duración Top (h) | 194 | 221 | 223 | 215 | -6 |
| % en Top (≥70) | 23.5% | 27.2% | 8.7% | 5.9% |  |

## 2. Pesos: v10.6 vs candidato

| Dimensión | v10.6 | Candidato | Δ |
| :---: | :---: | :---: | :---: |
| Stability | 21 | 4 | -17 |
| Consistency | 46 | 50 | +4 |
| Liquidity | 6 | 0 | -6 |
| Yield | 17 | 30 | +13 |
| Fee Eff. | 8 | 16 | +8 |
| Trend | 1 | 0 | -1 |

## 3. Score buckets en validación

### v10.6

| Score | N | APR% avg | Profitable% | Duración | Net APR% |
| :---: | :---: | :---: | :---: | :---: | :---: |
| <40 | 102310 | -17.8 | 3% | 32h | -81.6 |
| 40-55 | 47171 | -0.8 | 12% | 92h | -55.9 |
| 55-70 | 72033 | 3.7 | 15% | 227h | -20.1 |
| 70-85 | 42084 | 10.4 | 42% | 191h | -18.0 |
| 85+ | 40725 | 6.9 | 58% | 230h | -10.9 |

### Candidato

| Score | N | APR% avg | Profitable% | Duración | Net APR% |
| :---: | :---: | :---: | :---: | :---: | :---: |
| <40 | 96463 | -11.9 | 3% | 33h | -82.0 |
| 40-55 | 51103 | -13.5 | 11% | 75h | -62.4 |
| 55-70 | 138739 | 4.0 | 29% | 222h | -19.1 |
| 70-85 | 16271 | 22.2 | 69% | 203h | 3.7 |
| 85+ | 1747 | 24.4 | 72% | 160h | 34.3 |

## 3b. Sensibilidad al fee — top 15% del candidato (val)

Mismo top-15% por rango que la sección 1. El ranking no cambia con el fee; cambian los niveles absolutos. Muestra a qué fee el top deja de ser rentable (→ holdear más o subir el umbral de score).

| Fee round-trip | Net APR top | Profitable% |
| :---: | :---: | :---: |
| 0.15% | 2.2 | 75% |
| 0.20% | -1.3 | 71% |
| 0.30% | -7.5 | 59% |
| 0.40% | -12.8 | 49% |

## 4. Veredicto

✅ **ADOPTAR** — el candidato mejora el ranking (Spearman) y el APR-neto del top manteniendo monotonicity, sin overfitting. Copia a `analysis/scoring.py`.
