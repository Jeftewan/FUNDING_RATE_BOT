# Consenso calibrado contra el éxito real — 20261126

Éxito = APR neto realizado (salida al 3.er pago seguido ≤ 0, tope 30 d; costo Alpha 1.11 % del nocional, CEX 0.30 %) ≥ APR neto predicho al entrar. Candidatos con pred > 0 (CEX ∪ Alpha): 244,012; evaluados fuera de muestra 142,832 (2025-10-26 → 2026-10-26, walk-forward mensual, purga 31 d). Señales desde 2025-11-29. Tasa base de éxito 44.1%.

## Métricas fuera de muestra

| P | Brier | log-loss | BSS vs base | confiabilidad | resolución | ECE | ΔBrier vs A1 [IC 95 %] | trimestres mejor | tramo ≥ 9 | monótono |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| tasa base | 0.2547 | 0.7026 | +0.000 | 0.0067 | 0.0000 | 0.081 | +0.0000 [+0.0000, +0.0000] | 0% | 47% | sí |
| A1 consenso vigente + isotónica | 0.2547 | 0.7026 | +0.000 | 0.0067 | 0.0000 | 0.081 | +0.0000 [+0.0000, +0.0000] | 0% | 47% | sí |
| B pool logit + Venn-Abers | 0.2611 | 0.7205 | -0.025 | 0.0171 | 0.0028 | 0.116 | +0.0064 [+0.0030, +0.0101] | 60% | 54% | no |
| C HistGB + Venn-Abers | 0.2553 | 0.7081 | -0.002 | 0.0125 | 0.0047 | 0.083 | +0.0006 [-0.0032, +0.0046] | 60% | 75% | no |

### Tramos — A1 consenso vigente + isotónica

| consenso | n | P media | % éxito | APR real mediano | ±10 pp |
| --- | --- | --- | --- | --- | --- |
| 4–5 | 29937 | 50% | 43% | 12.1 | ok |
| 5–6 | 112895 | 53% | 44% | 12.7 | NO |

### Tramos — B pool logit + Venn-Abers

| consenso | n | P media | % éxito | APR real mediano | ±10 pp |
| --- | --- | --- | --- | --- | --- |
| 0–1 | 1008 | 7% | 35% | 25.4 | NO |
| 1–2 | 3501 | 14% | 39% | 21.4 | NO |
| 2–3 | 15375 | 26% | 50% | 23.0 | NO |
| 3–4 | 17742 | 36% | 45% | 16.8 | NO |
| 4–5 | 46876 | 45% | 39% | 13.4 | ok |
| 5–6 | 33473 | 55% | 42% | 10.3 | NO |
| 6–7 | 21375 | 64% | 53% | 6.8 | NO |
| 7–8 | 3314 | 74% | 54% | 5.3 | NO |
| 8–9 | 164 | 82% | 54% | 7.7 | NO |
| 9–10 | 4 | 93% | 100% | 32.4 | ok |

### Tramos — C HistGB + Venn-Abers

| consenso | n | P media | % éxito | APR real mediano | ±10 pp |
| --- | --- | --- | --- | --- | --- |
| 0–1 | 402 | 8% | 34% | 29.9 | NO |
| 1–2 | 4101 | 17% | 31% | 21.4 | NO |
| 2–3 | 19621 | 25% | 48% | 21.1 | NO |
| 3–4 | 28659 | 37% | 36% | 14.6 | ok |
| 4–5 | 40669 | 45% | 41% | 13.1 | ok |
| 5–6 | 27912 | 54% | 50% | 10.0 | ok |
| 6–7 | 15231 | 65% | 49% | 6.9 | NO |
| 7–8 | 5189 | 74% | 57% | 5.6 | NO |
| 8–9 | 958 | 84% | 73% | 7.3 | NO |
| 9–10 | 90 | 94% | 93% | 6.5 | ok |

### Brier por trimestre (vs A1)

| trimestre | n | B | C | A1 |
| --- | --- | --- | --- | --- |
| 2025Q4 | 18896 | 0.2492 | 0.2367 | 0.2587 |
| 2026Q1 | 26890 | 0.2531 | 0.2516 | 0.2639 |
| 2026Q2 | 35885 | 0.2876 | 0.2811 | 0.2543 |
| 2026Q3 | 48334 | 0.2543 | 0.2438 | 0.2495 |
| 2026Q4 | 12827 | 0.2465 | 0.2619 | 0.2500 |

## Criterio pre-registrado

- B pool logit + Venn-Abers — 1 ΔBrier vs A1 (IC < 0, ≥ 80 % trimestres): no
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
| B pool logit + Venn-Abers | 0.2769 | -0.120 | 0.150 | 70% |
| C HistGB + Venn-Abers | 0.2740 | -0.108 | 0.168 | 76% |

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
| P calibrada | 1239.2 | 23.8 | 0.22 |

| trimestre | picks | consenso vigente | P calibrada |
| --- | --- | --- | --- |
| 2025Q4 | 15 | 293.7 | 206.9 |
| 2026Q1 | 19 | 556.9 | 361.8 |
| 2026Q2 | 20 | 436.1 | 343.5 |
| 2026Q3 | 18 | 338.5 | 265.7 |
| 2026Q4 | 7 | 64.6 | 61.3 |

Gate sizing: NO pasa

## Veredicto

- Agregador elegido: ninguno pasa → se mantiene el consenso vigente.
## Interpretación y decisión (2026-11-26)

- **No pasa el criterio pre-registrado** (Venn-Abers con 60 d): B empeora a la tasa base (Brier +0.0064, IC
  [+0.0030, +0.0101]) y queda mal calibrado (ECE 0.116); C iguala a la base (ΔBrier +0.0006, IC cruza 0). Con
  180 d (`consensus_study_20261126_cal180.md`, sensibilidad post-hoc) B mejora la base apenas 2.6 % (ECE 0.053,
  tramo ≥ 9 sin n) y falla la confirmación con el motor (BSS −0.06). **No se exporta la tabla v2.**
- **Por qué**: "llegar al APR predicho" depende sobre todo del nivel de la predicción y del régimen del mes,
  no de la oportunidad. El modelo está sobre-disperso: decil bajo predice 1.2 % y rinde 4.5 %; decil alto
  predice 45.7 % y rinde 31.1 % (label realista). Por eso el éxito cae del 61 % (quintil bajo) al 29 %
  (quintil alto) y un consenso calibrado a "cumplir lo predicho" premia predicciones bajas: su tramo alto
  rinde 4–7 % APR y dimensionar con él pierde (23.8 % vs 32.4 % anual del consenso vigente).
- **El consenso vigente sí sirve para lo que mide**: con el label realista, del tramo < 3.5 al ≥ 7.5 el APR
  real mediano sube 7.0 → 20.8 % y el % que gana 72 → 91 %; su sizing gana al no-sizing (32.4 vs 25.9 %
  anual) en los 5 trimestres. Lo que no cumple es la **cifra esperada** que se muestra, no el orden.
- **Probado y descartado**: acierto reciente del modelo (`hit7`, éxito 53 → 30 % por quintil en estos 12 m) y
  cambio de la multitud en 14 d: correlación −0.43/−0.34 este año pero −0.03/−0.10 el año previo → propio de
  este régimen, no se usa. `basis_chg_24h` fuera por paridad (ρ 0.59 prod vs offline).
- **Censura**: las filas sin 30 d de horizonte observable se excluyen (antes el último mes solo tenía las
  salidas tempranas: éxito 2 %). Afecta también a `ml_v2`/`confidence_study` (hold-out del último mes).
- **Siguiente paso con potencial**: recalibrar la cifra "APR esperado" (isotónica p_prod → APR real mediano
  con costos reales) para que lo mostrado se cumpla ~50 % en todos los tramos; el consenso queda para el orden
  y el sizing. Registro en vivo activo desde este PR (`market_feature_snapshots.model_prediction/consensus`,
  `scripts/consensus_live.py`): primer chequeo ≈ 2027-01-05.
