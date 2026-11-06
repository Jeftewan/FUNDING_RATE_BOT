# Panel de analistas y consenso — 20261106

Candidatos (pred vigente > 0, universo CEX ∪ Alpha, label H30): 130,441 filas, 2025-11-06 → 2026-11-06; picks de la cartera: 86 (85 Alpha). Señales de mercado desde 2026-07-27 (~101 d). Vota un analista si |IC| ≥ 0.03 y el signo se repite en ≥ 75% de los periodos.

## Analistas

| analista | variable | periodos | IC medio | consistencia | cobertura | ¿vota? | dirección | cortes (terciles) |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| Modelo | `p_prod` | 5 trimestral | +0.404 | 100% | 100% | sí | más alto mejor | 9.849 / 20.39 |
| Racha | `streak` | 5 trimestral | -0.083 | 80% | 100% | no | más bajo mejor | 30 / 30 |
| Constancia | `pct` | 5 trimestral | -0.082 | 80% | 100% | no | más bajo mejor | 100 / 100 |
| Duración de picos | `ep_med_days` | 5 trimestral | +0.037 | 60% | 57% | no | más alto mejor | 1.835 / 14.17 |
| Edad del pico | `ep_age_days` | 5 trimestral | +0.040 | 80% | 100% | sí | más alto mejor | 49.83 / 133 |
| Incertidumbre | `relw` | 5 trimestral | -0.190 | 100% | 100% | sí | más bajo mejor | 1.74 / 2.38 |
| Régimen | `regime_median_daily` | 5 trimestral | -0.069 | 80% | 100% | no | más bajo mejor | 0.0003 / 0.0003 |
| Basis | `basis_24h` | 13 semanal | -0.013 | 54% | 75% | no | más bajo mejor | 0.1229 / 0.2666 |
| Tendencia basis | `basis_chg_24h` | 13 semanal | +0.002 | 46% | 75% | no | más alto mejor | -0.04998 / 0.04693 |
| Interés abierto | `log_oi_usd` | 13 semanal | +0.170 | 92% | 81% | sí | más alto mejor | 6.566 / 6.787 |
| Flujo OI | `oi_chg_24h_pct` | 13 semanal | -0.010 | 54% | 81% | no | más bajo mejor | -0.8678 / 0.637 |
| Top traders | `top_ls_24h` | 13 semanal | -0.022 | 46% | 81% | no | más bajo mejor | 1.362 / 1.688 |
| Volatilidad | `volatility_7d` | 13 semanal | +0.100 | 92% | 81% | sí | más alto mejor | 0.03801 / 0.06364 |

IC por periodo:

- Modelo: 2025Q4 +0.33, 2026Q1 +0.46, 2026Q2 +0.45, 2026Q3 +0.54, 2026Q4 +0.24
- Racha: 2025Q4 +0.01, 2026Q1 -0.07, 2026Q2 -0.07, 2026Q3 -0.11, 2026Q4 -0.18
- Constancia: 2025Q4 +0.01, 2026Q1 -0.07, 2026Q2 -0.07, 2026Q3 -0.11, 2026Q4 -0.17
- Duración de picos: 2025Q4 +0.19, 2026Q1 -0.01, 2026Q2 -0.05, 2026Q3 +0.04, 2026Q4 +0.02
- Edad del pico: 2025Q4 +0.08, 2026Q1 +0.07, 2026Q2 -0.05, 2026Q3 +0.05, 2026Q4 +0.05
- Incertidumbre: 2025Q4 -0.23, 2026Q1 -0.21, 2026Q2 -0.20, 2026Q3 -0.24, 2026Q4 -0.07
- Régimen: 2025Q4 -0.19, 2026Q1 -0.04, 2026Q2 -0.05, 2026Q3 -0.13, 2026Q4 +0.06
- Basis: 2026-07-27/2026-08-02 -0.00, 2026-08-03/2026-08-09 +0.04, 2026-08-10/2026-08-16 -0.02, 2026-08-17/2026-08-23 +0.10, 2026-08-24/2026-08-30 +0.21, 2026-08-31/2026-09-06 +0.12, 2026-09-07/2026-09-13 +0.03, 2026-09-14/2026-09-20 -0.05, 2026-09-21/2026-09-27 +0.09, 2026-09-28/2026-10-04 -0.20, 2026-10-05/2026-10-11 -0.28, 2026-10-12/2026-10-18 -0.01, 2026-10-19/2026-10-25 -0.20
- Tendencia basis: 2026-07-27/2026-08-02 +0.05, 2026-08-03/2026-08-09 +0.04, 2026-08-10/2026-08-16 +0.05, 2026-08-17/2026-08-23 -0.04, 2026-08-24/2026-08-30 +0.04, 2026-08-31/2026-09-06 -0.01, 2026-09-07/2026-09-13 -0.02, 2026-09-14/2026-09-20 +0.05, 2026-09-21/2026-09-27 -0.08, 2026-09-28/2026-10-04 +0.07, 2026-10-05/2026-10-11 -0.00, 2026-10-12/2026-10-18 -0.03, 2026-10-19/2026-10-25 -0.09
- Interés abierto: 2026-07-27/2026-08-02 +0.07, 2026-08-03/2026-08-09 +0.10, 2026-08-10/2026-08-16 +0.04, 2026-08-17/2026-08-23 +0.17, 2026-08-24/2026-08-30 +0.32, 2026-08-31/2026-09-06 +0.23, 2026-09-07/2026-09-13 +0.29, 2026-09-14/2026-09-20 +0.13, 2026-09-21/2026-09-27 +0.30, 2026-09-28/2026-10-04 +0.30, 2026-10-05/2026-10-11 +0.22, 2026-10-12/2026-10-18 -0.27, 2026-10-19/2026-10-25 +0.30
- Flujo OI: 2026-07-27/2026-08-02 +0.06, 2026-08-03/2026-08-09 +0.04, 2026-08-10/2026-08-16 -0.01, 2026-08-17/2026-08-23 +0.04, 2026-08-24/2026-08-30 -0.08, 2026-08-31/2026-09-06 +0.07, 2026-09-07/2026-09-13 -0.07, 2026-09-14/2026-09-20 +0.15, 2026-09-21/2026-09-27 +0.02, 2026-09-28/2026-10-04 -0.05, 2026-10-05/2026-10-11 -0.14, 2026-10-12/2026-10-18 -0.09, 2026-10-19/2026-10-25 -0.06
- Top traders: 2026-07-27/2026-08-02 -0.08, 2026-08-03/2026-08-09 -0.02, 2026-08-10/2026-08-16 +0.02, 2026-08-17/2026-08-23 -0.13, 2026-08-24/2026-08-30 -0.11, 2026-08-31/2026-09-06 +0.03, 2026-09-07/2026-09-13 +0.08, 2026-09-14/2026-09-20 +0.02, 2026-09-21/2026-09-27 -0.18, 2026-09-28/2026-10-04 +0.02, 2026-10-05/2026-10-11 +0.11, 2026-10-12/2026-10-18 +0.01, 2026-10-19/2026-10-25 -0.04
- Volatilidad: 2026-07-27/2026-08-02 +0.01, 2026-08-03/2026-08-09 +0.04, 2026-08-10/2026-08-16 +0.13, 2026-08-17/2026-08-23 +0.14, 2026-08-24/2026-08-30 +0.16, 2026-08-31/2026-09-06 +0.18, 2026-09-07/2026-09-13 +0.19, 2026-09-14/2026-09-20 +0.14, 2026-09-21/2026-09-27 +0.03, 2026-09-28/2026-10-04 +0.11, 2026-10-05/2026-10-11 +0.15, 2026-10-12/2026-10-18 -0.30, 2026-10-19/2026-10-25 +0.32

## Votos en los picks del modelo (APR realizado por voto)

| analista | voto | picks | % APR ≥ 5 | % ganan | APR mediano |
| --- | --- | --- | --- | --- | --- |
| Modelo | favorable | 82 | 80% | 83% | 43.7 |
| Modelo | neutral | 2 | 0% | 0% | -96.9 |
| Modelo | desfavorable | 2 | 50% | 50% | -62.7 |
| Racha | favorable | 6 | 67% | 67% | 20.1 |
| Racha | desfavorable | 80 | 79% | 81% | 43.7 |
| Constancia | favorable | 6 | 67% | 67% | 20.1 |
| Constancia | desfavorable | 80 | 79% | 81% | 43.7 |
| Duración de picos | favorable | 19 | 74% | 79% | 30.0 |
| Duración de picos | neutral | 21 | 62% | 67% | 33.7 |
| Duración de picos | desfavorable | 18 | 94% | 94% | 46.8 |
| Edad del pico | favorable | 16 | 75% | 75% | 42.9 |
| Edad del pico | neutral | 27 | 93% | 96% | 41.8 |
| Edad del pico | desfavorable | 43 | 70% | 72% | 33.8 |
| Incertidumbre | favorable | 61 | 77% | 80% | 47.7 |
| Incertidumbre | neutral | 16 | 88% | 88% | 27.5 |
| Incertidumbre | desfavorable | 9 | 67% | 67% | 30.1 |
| Régimen | favorable | 12 | 42% | 42% | -23.4 |
| Régimen | desfavorable | 74 | 84% | 86% | 46.8 |
| Basis | favorable | 10 | 60% | 70% | 26.0 |
| Basis | neutral | 6 | 67% | 67% | 24.5 |
| Basis | desfavorable | 3 | 67% | 67% | 57.9 |
| Tendencia basis | favorable | 7 | 71% | 71% | 57.9 |
| Tendencia basis | neutral | 6 | 67% | 67% | 28.9 |
| Tendencia basis | desfavorable | 6 | 50% | 67% | 9.6 |
| Interés abierto | favorable | 14 | 71% | 71% | 36.7 |
| Interés abierto | neutral | 5 | 40% | 60% | 3.8 |
| Interés abierto | desfavorable | 2 | 50% | 50% | 12.9 |
| Flujo OI | favorable | 9 | 44% | 44% | -16.1 |
| Flujo OI | neutral | 5 | 80% | 80% | 67.3 |
| Flujo OI | desfavorable | 7 | 71% | 86% | 18.2 |
| Top traders | favorable | 9 | 44% | 44% | -16.1 |
| Top traders | neutral | 10 | 70% | 80% | 28.9 |
| Top traders | desfavorable | 2 | 100% | 100% | 72.5 |
| Volatilidad | favorable | 9 | 67% | 67% | 33.7 |
| Volatilidad | neutral | 4 | 75% | 75% | 81.6 |
| Volatilidad | desfavorable | 8 | 50% | 62% | 11.0 |

## ¿El consenso mejora la cartera?

| política | 12 m PnL USD | 12 m anual % | trades | % gan. | ventana señales PnL | anual % | trades |
| --- | --- | --- | --- | --- | --- | --- | --- |
| modelo (pred>0) | 1436.6 | 28.5 | 86 | 80.2 | 314.0 | 24.4 | 24 |
| modelo + filtro consenso > 5 | 1444.6 | 28.7 | 86 | 80.2 | 322.9 | 25.1 | 24 |
| mezcla rango 1:0.3 | 1574.4 | 31.3 | 83 | 84.3 | 283.9 | 22.1 | 25 |
| mezcla rango 1:1 | 1375.7 | 27.3 | 79 | 88.6 | 285.3 | 22.2 | 25 |
| solo consenso | 956.5 | 19.0 | 71 | 90.1 | 217.6 | 16.9 | 17 |

## Sizing sobre los picks del modelo

| sizing | PnL USD | anual % | DD % |
| --- | --- | --- | --- |
| sin sizing | 1436.6 | 27.6 | 0.36 |
| tiers ancho relativo (vigente) | 1534.2 | 29.5 | 0.27 |
| tiers consenso | 1799.4 | 34.6 | 0.43 |

Por trimestre (PnL USD de los picks):

| trimestre | picks | sin sizing | ancho relativo | consenso |
| --- | --- | --- | --- | --- |
| 2025Q4 | 11 | 225.1 | 219.9 | 281.4 |
| 2026Q1 | 23 | 497.8 | 508.5 | 622.3 |
| 2026Q2 | 21 | 358.3 | 419.0 | 447.9 |
| 2026Q3 | 19 | 358.1 | 390.2 | 447.6 |
| 2026Q4 | 12 | -2.7 | -3.4 | 0.3 |

Gate sizing por consenso (≥ ancho relativo en todos los trimestres menos uno y mejor anual): PASA

## Consenso → resultado real (candidatos)

| consenso | etiqueta | n | % APR ≥ 5 | % ganan | APR mediano | p25–p75 | hold mediano d |
| --- | --- | --- | --- | --- | --- | --- | --- |
| 0–3.5 | Muy desfavorable | 34984 | 85% | 88% | 13.2 | 7.0–23.1 | 30.0 |
| 3.5–5 | Desfavorable | 19605 | 88% | 90% | 17.8 | 9.3–28.2 | 30.0 |
| 5.0–6 | Neutral | 17922 | 92% | 94% | 20.7 | 13.0–31.2 | 30.0 |
| 6.0–7.5 | Favorable | 21916 | 93% | 94% | 23.5 | 15.4–33.9 | 30.0 |
| 7.5–10 | Muy favorable | 36014 | 94% | 95% | 30.0 | 19.4–46.1 | 30.0 |

## Consenso → resultado real (picks)

| consenso | etiqueta | n | % APR ≥ 5 | % ganan | APR mediano | p25–p75 | hold mediano d |
| --- | --- | --- | --- | --- | --- | --- | --- |
| 3.5–5 | Desfavorable | 2 | 50% | 50% | -62.7 | -110.9–-14.4 | 6.4 |
| 5.0–6 | Neutral | 2 | 0% | 0% | -43.4 | -57.1–-29.7 | 3.9 |
| 6.0–7.5 | Favorable | 8 | 75% | 75% | 36.0 | 8.8–53.8 | 23.7 |
| 7.5–10 | Muy favorable | 74 | 81% | 84% | 45.7 | 19.0–77.7 | 30.0 |

## Análogos: 39 celdas (quintil de predicción [6.56, 12.11, 18.49, 25.74] × venue × ancho [1.74, 2.38])

## Veredicto

- Mejor política con consenso: `mezcla rango 1:0.3` 1574.4 USD vs modelo solo 1436.6 USD (12 m).
- Sizing por consenso: 34.6 % vs ancho relativo 29.5 % (DD 0.43 vs 0.27).

## Interpretación y decisión (2026-11-06)

- **Qué decide si una oportunidad "se cumple"**: entre candidatos con pred > 0, la tasa de cumplimiento
  (APR ≥ 5 %) es alta en todos los tramos de consenso (85–94 %) → una "probabilidad" casi no distingue.
  Lo que sí cambia es **cuánto** rinde: APR neto mediano 13 % (muy desfavorable) → 30 % (muy favorable),
  monótono. En los picks, el tramo "muy favorable" (74 de 86) tuvo mediana 46 %.
- **Analistas que votan** (separan el APR realizado fuera de muestra, signo estable): modelo (+0.40),
  incertidumbre del rango (−0.19: rango angosto = mejor), interés abierto (+0.17: más OI = mejor),
  volatilidad 7 d (+0.10) y edad de la racha (+0.04). El basis, el flujo de OI, los top traders y la
  tendencia del basis **no** separan → solo contexto. Racha/constancia/régimen tienen cortes degenerados
  (casi todos 30/30, 100 %, base) → no votan.
- **Orden**: no se cambia. La mezcla 1:0.3 gana 12 m (+138 USD) pero pierde en la ventana reciente;
  ordenar solo por consenso pierde 33 %.
- **Sizing**: el consenso reemplaza al ancho relativo (1.25× si ≥ 6, 0.75× si ≤ 5): 34.6 % vs 29.5 %
  anual, mejor en los 5 trimestres; DD 0.43 vs 0.27 % (ambos chicos). Exportado con
  `size_by_consensus: true`.
- En prod el chip de la lista calcula el consenso sin la edad de la racha (necesita historia larga) →
  puede diferir un poco del modal, que usa los 5 analistas.
