# ML stability (walk-forward) — uplift ML vs heurístico v11.0 — 2026-06-17 10:08

**Setup:** train expandible (≥45d), test en ventanas sucesivas de
7d. GBR re-entrenado por fold (solo con pasado → sin look-ahead).
Target `net_apr`. 5 folds. Local-only, soporte de decisión.

## 1. Por fold

| Ventana test | n_test | IC heur | IC ML | Uplift | top10 heur | top10 ML | Δtop10 | top1% ML | %rent top10 ML |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| 2026-05-11→2026-05-18 | 115312 | 0.607 | 0.76 | +0.153 | -8.1 | -1.7 | +6.4 | 28.0 | 68% |
| 2026-05-18→2026-05-25 | 86487 | 0.605 | 0.718 | +0.114 | -5.5 | 0.9 | +6.4 | 18.3 | 68% |
| 2026-05-25→2026-06-01 | 92554 | 0.633 | 0.739 | +0.106 | 7.7 | 15.2 | +7.5 | 67.5 | 79% |
| 2026-06-01→2026-06-08 | 89162 | 0.663 | 0.753 | +0.089 | 4.2 | 12.8 | +8.6 | 51.5 | 75% |
| 2026-06-08→2026-06-15 | 47202 | 0.626 | 0.739 | +0.113 | -6.9 | 6.2 | +13.1 | 58.6 | 49% |

## 2. Resumen de estabilidad

| Métrica | Valor |
| --- | --- |
| Uplift IC medio | +0.115 |
| Uplift IC desvío (σ) | 0.021 |
| Uplift positivo en todos los folds | Sí |
| Δ net_apr top-decil medio (ML − heur) | +8.4 |
| Δ net_apr top-1% medio (ML − heur) | +24.4 |

**Interpretación de ganancia:** el Δ net_apr del top es lo que subiría tu rendimiento
anualizado si tradeás las mejores oportunidades rankeadas por ML en vez de por el
score. (net_apr está en % anualizado; recordá que a fee 0.30% el heurístico deja el
top en ~break-even.)

## 3. Veredicto

**ESTABLE y MATERIAL — el ML bate al heurístico de forma consistente. Vale explorar el camino híbrido (ML offline).**

Umbrales: ESTABLE = uplift>0 en todos los folds, medio ≥0.05, σ≤0.05.
Aun si es estable, el primer paso es el **híbrido offline** (GBR en local re-deriva
pesos o pre-computa scores cacheados), NO inferencia sklearn en Railway (no tiene
numpy/sklearn). ML online solo si el híbrido no alcanza y el uplift es grande/durable.
