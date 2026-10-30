# Confianza por oportunidad — 20261030

Picks de la cartera del modelo vigente (top-5 cada 8 h, pred > 0, 5 slots, 5.200 USD, 1x, salida H30) en walk-forward mensual 2025-09 → 2026-09-22 + hold-out hasta 2026-10-22. Cabezas entrenadas solo con filas pred vigente > 0 (229,833). El modelo vigente es in-sample en su ventana de train (2026-07-12 → 2026-10-08, dataset de snapshots distinto).

- Picks: **97** (97 Alpha). APR predicho medio 54.3 %, realizado 36.5 % (mediana 42.8 %); llegan a lo predicho: **43 %**.
- Calibración expansiva (≥ 30 picks cerrados antes), evaluada en 63 picks:
  - P(APR ≥ 5 %): media cruda 0.92 / calibrada 0.81 vs real 0.76; Brier 0.197 → 0.201.
  - Banda p10–p90: cobertura cruda 0.56 → CQR 0.81 (objetivo 0.80); ancho medio CQR 171 pp de APR.

## Sizing por confianza (Alta 1.25× / Baja 0.75×, corte = mediana expansiva), anual %

| segmento | picks | base | p5 | ancho relativo |
| --- | --- | --- | --- | --- |
| 2025-T3 (sin calibración) | 5 | 39.2 | 39.2 | 39.2 |
| 2025-T4 (sin calibración) | 23 | 32.8 | 32.8 | 32.8 |
| 2026-T1 | 20 | 40.0 | 35.6 | 40.8 |
| 2026-T2 | 19 | 28.7 | 29.0 | 33.1 |
| 2026-T3 | 19 | 28.7 | 28.2 | 31.5 |
| hold-out | 11 | -1.4 | -2.2 | -0.8 |
| **total** | 97 | 30.7 | 29.7 | 32.5 |

DD %: base 0.34, p5 0.29, ancho 0.26. Trimestres WF con sizing ≥ base: p5 1/3, ancho 3/3.

## Fiabilidad de P(APR ≥ 5 %) calibrada (picks evaluados, quintiles)

| quintil | n | P predicha | frecuencia real | APR real medio |
| --- | --- | --- | --- | --- |
| 1 | 13 | 0.55 | 0.62 | 16.2 |
| 2 | 12 | 0.85 | 0.75 | 15.4 |
| 3 | 13 | 0.87 | 1.00 | 71.1 |
| 4 | 12 | 0.88 | 0.50 | 6.4 |
| 5 | 13 | 0.94 | 0.92 | 53.8 |

## Tiers con la métrica `relw` (picks evaluados, corte expansivo)

| tier | n | P(APR ≥ 5 %) real | APR real medio | APR real mediana |
| --- | --- | --- | --- | --- |
| alta | 33 | 0.79 | 55.1 | 55.5 |
| baja | 30 | 0.73 | 9.2 | 26.5 |

## Gate (métrica de tier elegida: `relw`)

- segments_ok: OK
- holdout_ok: OK
- dd_ok: OK
- coverage_ok: OK
- **PASA**
