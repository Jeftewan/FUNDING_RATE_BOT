# Consenso calibrado contra el éxito real — 20261126 (sensibilidad: Venn-Abers con 180 d)

Éxito = APR neto realizado (salida al 3.er pago seguido ≤ 0, tope 30 d; costo Alpha 1.11 % del nocional, CEX 0.30 %) ≥ APR neto predicho al entrar. Candidatos con pred > 0 (CEX ∪ Alpha): 244,012; evaluados fuera de muestra 142,832 (2025-10-26 → 2026-10-26, walk-forward mensual, purga 31 d). Señales desde 2025-11-29. Tasa base de éxito 44.1%.

## Métricas fuera de muestra

| P | Brier | log-loss | BSS vs base | confiabilidad | resolución | ECE | ΔBrier vs A1 [IC 95 %] | trimestres mejor | tramo ≥ 9 | monótono |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| tasa base | 0.2547 | 0.7026 | +0.000 | 0.0067 | 0.0000 | 0.081 | +0.0000 [+0.0000, +0.0000] | 0% | 47% | sí |
| A1 consenso vigente + isotónica | 0.2547 | 0.7026 | +0.000 | 0.0067 | 0.0000 | 0.081 | +0.0000 [+0.0000, +0.0000] | 0% | 47% | sí |
| B pool logit + Venn-Abers | 0.2481 | 0.6896 | +0.026 | 0.0035 | 0.0020 | 0.053 | -0.0066 [-0.0089, -0.0042] | 100% | 53% | no |
| C HistGB + Venn-Abers | 0.2501 | 0.6939 | +0.018 | 0.0056 | 0.0021 | 0.065 | -0.0046 [-0.0073, -0.0018] | 60% | 77% | sí |

### Tramos — A1 consenso vigente + isotónica

| consenso | n | P media | % éxito | APR real mediano | ±10 pp |
| --- | --- | --- | --- | --- | --- |
| 4–5 | 29937 | 50% | 43% | 12.1 | ok |
| 5–6 | 112895 | 53% | 44% | 12.7 | NO |

### Tramos — B pool logit + Venn-Abers

| consenso | n | P media | % éxito | APR real mediano | ±10 pp |
| --- | --- | --- | --- | --- | --- |
| 1–2 | 75 | 18% | 53% | 43.1 | NO |
| 2–3 | 591 | 26% | 42% | 31.3 | NO |
| 3–4 | 28681 | 37% | 43% | 20.8 | ok |
| 4–5 | 65846 | 44% | 40% | 13.0 | ok |
| 5–6 | 38628 | 54% | 50% | 8.4 | ok |
| 6–7 | 7630 | 64% | 51% | 6.1 | NO |
| 7–8 | 1337 | 72% | 53% | 6.2 | NO |
| 8–9 | 44 | 84% | 41% | -27.5 | ok |

### Tramos — C HistGB + Venn-Abers

| consenso | n | P media | % éxito | APR real mediano | ±10 pp |
| --- | --- | --- | --- | --- | --- |
| 0–1 | 15 | 9% | 0% | 21.4 | ok |
| 1–2 | 283 | 17% | 19% | 32.0 | ok |
| 2–3 | 3322 | 27% | 37% | 30.4 | NO |
| 3–4 | 29972 | 36% | 44% | 20.1 | ok |
| 4–5 | 60488 | 44% | 41% | 13.3 | ok |
| 5–6 | 24977 | 53% | 46% | 9.5 | ok |
| 6–7 | 21947 | 65% | 51% | 6.6 | NO |
| 7–8 | 1600 | 73% | 63% | 7.2 | NO |
| 8–9 | 150 | 84% | 65% | 5.9 | NO |
| 9–10 | 78 | 93% | 100% | 6.1 | ok |

### Brier por trimestre (vs A1)

| trimestre | n | B | C | A1 |
| --- | --- | --- | --- | --- |
| 2025Q4 | 18896 | 0.2422 | 0.2348 | 0.2587 |
| 2026Q1 | 26890 | 0.2497 | 0.2497 | 0.2639 |
| 2026Q2 | 35885 | 0.2523 | 0.2526 | 0.2543 |
| 2026Q3 | 48334 | 0.2492 | 0.2517 | 0.2495 |
| 2026Q4 | 12827 | 0.2372 | 0.2605 | 0.2500 |

## Criterio pre-registrado

- B pool logit + Venn-Abers — 1 ΔBrier vs A1 (IC < 0, ≥ 80 % trimestres): PASA
- B pool logit + Venn-Abers — 2 calibración (ECE ≤ 0.05, tramos ±10 pp, monótono): no
- B pool logit + Venn-Abers — 3 resolución (tramo ≥ 9 ≥ 1.5× base): no
- B pool logit + Venn-Abers — 5 confirmación motor (BSS > 0, ECE ≤ 0.08): no
- C HistGB + Venn-Abers — 1 ΔBrier vs A1 (IC < 0, ≥ 80 % trimestres): no
- C HistGB + Venn-Abers — 2 calibración (ECE ≤ 0.05, tramos ±10 pp, monótono): no
- C HistGB + Venn-Abers — 3 resolución (tramo ≥ 9 ≥ 1.5× base): PASA
- C HistGB + Venn-Abers — 5 confirmación motor (BSS > 0, ECE ≤ 0.08): no

## Confirmación con el motor de ejecución (24,986 filas, 25 tokens Alpha, éxito 58.7%)

| P | Brier | BSS vs base | ECE | tramo ≥ 9 |
| --- | --- | --- | --- | --- |
| tasa base | 0.2473 | +0.000 | 0.062 | 61% |
| A1 consenso vigente + isotónica | 0.2473 | +0.000 | 0.062 | 61% |
| B pool logit + Venn-Abers | 0.2611 | -0.056 | 0.136 | 69% |
| C HistGB + Venn-Abers | 0.2631 | -0.064 | 0.143 | 70% |

## Analistas (pool logit entrenado con todo)

| analista | variable | coef | log-odds decil bajo → alto | cobertura |
| --- | --- | --- | --- | --- |
| Modelo | `p_prod` | +1.125 | +0.83 → -0.94 | 100% |
| Tasa vigente | `rate_apr` | -0.565 | +0.00 → -0.44 | 100% |
| Brecha | `pred_gap` | -0.264 | -0.23 → -0.47 | 100% |
| Incertidumbre | `relw` | +0.280 | -0.60 → +0.45 | 81% |
| Racha | `streak` | +0.450 | +0.71 → -0.02 | 100% |
| Constancia | `pct` | +0.450 | +0.71 → -0.02 | 100% |
| Régimen | `regime_median_daily` | +0.650 | -0.04 → -0.02 | 100% |
| Volumen | `log_volume` | +0.703 | -0.03 → +0.43 | 100% |
| Intervalo | `ih` | +0.336 | +0.33 → +0.43 | 100% |
| Multitud | `crowd` | +0.573 | +0.26 → -0.49 | 100% |
| Basis | `basis_24h` | +0.088 | +0.11 → -0.20 | 40% |
| Interés abierto | `log_oi_usd` | +0.904 | -0.24 → -0.26 | 45% |
| Flujo OI | `oi_chg_24h_pct` | +0.050 | -0.21 → -0.27 | 45% |
| Top traders | `top_ls_24h` | +0.786 | -0.41 → -0.00 | 45% |
| Volatilidad | `volatility_7d` | -1.944 | -0.12 → -0.24 | 45% |

## Éxito por quintil de predicción (lo esperado sube → cuesta más cumplir)

| quintil p_prod | n | pred mediana | % éxito | APR real mediano |
| --- | --- | --- | --- | --- |
| 1 | 28671 | 3.1 | 61% | 6.3 |
| 2 | 28462 | 8.5 | 52% | 9.2 |
| 3 | 28568 | 15.2 | 43% | 12.5 |
| 4 | 28564 | 21.2 | 35% | 16.2 |
| 5 | 28567 | 33.5 | 29% | 24.0 |

## Sizing sobre los picks (79; P = B pool logit + Venn-Abers)

| sizing | PnL USD | anual % | DD % |
| --- | --- | --- | --- |
| sin sizing | 1351.9 | 25.9 | 0.29 |
| consenso vigente | 1689.8 | 32.4 | 0.36 |
| P calibrada | 1230.7 | 23.6 | 0.29 |

| trimestre | picks | consenso vigente | P calibrada |
| --- | --- | --- | --- |
| 2025Q4 | 15 | 293.7 | 239.3 |
| 2026Q1 | 19 | 556.9 | 365.3 |
| 2026Q2 | 20 | 436.1 | 297.4 |
| 2026Q3 | 18 | 338.5 | 265.2 |
| 2026Q4 | 7 | 64.6 | 63.4 |

Gate sizing: NO pasa

## Veredicto

- Agregador elegido: ninguno pasa → se mantiene el consenso vigente.