# Optimizer Report — v10.5 baseline vs candidato

**Fecha:** 2026-06-10 11:22
**Trials:** 600
**Train:** 775,207 rows (hasta 2026-05-16) | **Val:** 333,827 rows (desde 2026-05-16)
**Horizonte objetivo:** 24h | **Alcance:** spot_perp

> El optimizador SOLO genera este reporte + un candidato. No aplica nada. Revisa abajo y, si convence, copia el candidato a `analysis/scoring.py` a mano.

## 1. Métricas (validación)

| Métrica | v10.5 Train | v10.5 Val | Cand. Train | Cand. Val | Δ Val |
| :---: | :---: | :---: | :---: | :---: | :---: |
| Spearman (Net APR) | 0.577 | 0.600 | 0.598 | 0.612 | 0.013 |
| Spearman (Duración) | 0.635 | 0.641 | 0.621 | 0.622 | -0.019 |
| Spearman (APR) | 0.443 | 0.416 | 0.502 | 0.471 | 0.056 |
| Monotonicity | 0.75 | 0.75 | 0.75 | 0.75 | 0.00 |
| Profit Rate Top | 4% | 4% | 14% | 11% | +7pp |
| Duración Top (h) | 126 | 122 | 113 | 110 | -12 |
| % en Top (≥70) | 29.5% | 33.5% | 23.7% | 28.0% |  |

## 2. Pesos: v10.5 vs candidato

| Dimensión | v10.5 | Candidato | Δ |
| :---: | :---: | :---: | :---: |
| Stability | 31 | 21 | -10 |
| Consistency | 44 | 46 | +2 |
| Liquidity | 4 | 6 | +2 |
| Yield | 13 | 17 | +4 |
| Fee Eff. | 5 | 8 | +3 |
| Trend | 3 | 1 | -2 |

## 3. Score buckets en validación

### v10.5

| Score | N | APR% avg | Profitable% | Duración | Net APR% |
| :---: | :---: | :---: | :---: | :---: | :---: |
| <40 | 132873 | -19.3 | 3% | 24h | -564.3 |
| 40-55 | 41363 | 6.0 | 8% | 61h | -264.6 |
| 55-70 | 47914 | 11.5 | 19% | 96h | -123.0 |
| 70-85 | 55146 | 4.3 | 4% | 126h | -73.8 |
| 85+ | 56531 | 8.5 | 4% | 118h | -57.4 |

### Candidato

| Score | N | APR% avg | Profitable% | Duración | Net APR% |
| :---: | :---: | :---: | :---: | :---: | :---: |
| <40 | 113015 | -22.7 | 2% | 22h | -591.4 |
| 40-55 | 55696 | 2.0 | 5% | 52h | -327.1 |
| 55-70 | 71606 | 6.3 | 7% | 116h | -96.3 |
| 70-85 | 47776 | 11.4 | 17% | 103h | -100.2 |
| 85+ | 45734 | 8.9 | 5% | 119h | -52.6 |

## 4. Veredicto

⚠️ **REVISAR** — mejora parcial. Evalúa si compensa el cambio antes de adoptar.
