# ML Binance — dataset de tasas liquidadas — 20261022

**Datos:** 2,178,924 filas con label (869 símbolos), 2024-01-06 → 2026-10-22; elegibles en prod 983,916.
**Label:** real14 (sale tras pagar el 1er funding ≤ 0, tope 14 d, fee 0.30 %, APR sobre 14 d, por nocional). **Walk-forward:** 12 folds mensuales 2025-09 → 2026-09-22, purga 15 d. **Hold-out intocado:** desde 2026-09-22.

## 1. Walk-forward (filas elegibles, IC = Spearman diario promedio)

| variante | IC | t | días | top-10% y_real14 | folds con uplift>0 | uplift medio |
| --- | --- | --- | --- | --- | --- | --- |
| E0 | 0.542 | 133.8 | 386 | 0.7 | 0.92 | 0.023 |
| E0a | 0.550 | 146.5 | 386 | 0.9 | 1.0 | 0.031 |
| E1 | 0.529 | 108.4 | 386 | 0.6 | 0.83 | 0.011 |
| heurístico v11.0 | 0.518 | | | 0.1 | | |
| top settlement_avg | 0.059 | | | -4.5 | | |

IC del modelo E0 contra el label oráculo (el del modelo vigente): 0.564.

## 2. Cartera simulada (5 slots, 5.200 USD, 1x, decisión cada 8 h, política del label)

| estrategia | PnL USD | retorno anual % | trades | % ganadoras | hold medio d |
| --- | --- | --- | --- | --- | --- |
| E0 | 67.5 | 1.2 | 265 | 44.2 | 7.1 |
| E0+gate | 59.4 | 1.1 | 265 | 43.4 | 6.9 |
| E0a | 134.3 | 2.4 | 226 | 54.4 | 8.5 |
| E0a+gate | 134.3 | 2.4 | 226 | 54.4 | 8.5 |
| E1 | 76.8 | 1.4 | 237 | 48.5 | 8.0 |
| E1+gate | 95.8 | 1.7 | 238 | 48.7 | 7.7 |
| heur | 75.2 | 1.4 | 235 | 48.1 | 8.1 |
| heur+gate70 | 45.7 | 0.8 | 232 | 39.2 | 6.7 |
| sa | -1421.8 | -25.9 | 1056 | 5.6 | 1.6 |

## 3. Hold-out intocado (30 d)

| estrategia | IC | top-10% | cartera PnL | anual % | trades | cartera con gate pred>0 |
| --- | --- | --- | --- | --- | --- | --- |
| E0 | 0.421 | -1.9 | -10.3 | -2.4 | 28 | 9.2 / 2.1% |
| E0a | 0.434 | -1.3 | -12.3 | -2.8 | 29 | 0.1 / 0.0% |
| E1 | 0.403 | -2.0 | -12.1 | -2.8 | 29 | 6.8 / 1.6% |
| heur | 0.374 | -2.4 | -7.1 | -1.6 | 26 | — |
| sa | 0.231 | -3.4 | -110.4 | -25.3 | 79 | — |

## 4. Por fold (IC modelo / IC heurístico)

| fold | E0 | E0a | E1 |
| --- | --- | --- | --- |
| 2025-09 | 0.480 / 0.498 | 0.540 / 0.498 | 0.391 / 0.498 |
| 2025-10 | 0.518 / 0.500 | 0.534 / 0.500 | 0.483 / 0.500 |
| 2025-11 | 0.595 / 0.570 | 0.605 / 0.570 | 0.584 / 0.570 |
| 2025-12 | 0.567 / 0.528 | 0.574 / 0.528 | 0.567 / 0.528 |
| 2026-01 | 0.609 / 0.592 | 0.617 / 0.592 | 0.607 / 0.592 |
| 2026-02 | 0.631 / 0.611 | 0.629 / 0.611 | 0.628 / 0.611 |
| 2026-03 | 0.574 / 0.559 | 0.574 / 0.559 | 0.573 / 0.559 |
| 2026-04 | 0.528 / 0.502 | 0.528 / 0.502 | 0.528 / 0.502 |
| 2026-05 | 0.499 / 0.479 | 0.499 / 0.479 | 0.500 / 0.479 |
| 2026-06 | 0.557 / 0.529 | 0.553 / 0.529 | 0.563 / 0.529 |
| 2026-07 | 0.537 / 0.493 | 0.534 / 0.493 | 0.537 / 0.493 |
| 2026-08 | 0.467 / 0.425 | 0.473 / 0.425 | 0.453 / 0.425 |

Variantes probadas: 3 (E0, E0a, E1). Se elige la más simple que pase el gate (uplift>0 en ≥80 % de folds, uplift medio ≥0.05, mejor cartera que heurístico y settlement_avg en walk-forward Y en hold-out).