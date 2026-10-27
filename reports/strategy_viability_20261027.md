# Viabilidad de la estrategia spot_perp CEX ∪ Alpha — 20261027

Motor de `scripts/rotation_study.py` sin rotar: salida H30, 1x, decisión cada 8 h, fee CEX 0.30 %, Alpha 0.41 % del nocional. Primer token Alpha en el universo: 2024-12-18. Referencia libre de riesgo: 4 % anual.

## 1. Edge por trimestre (ranking por tasa vigente, 5 slots, 5.200 USD)

| trimestre | universo | anual % | trades | % ganadoras | ocupación slots % |
| --- | --- | --- | --- | --- | --- |
| 2024Q4 | CEX | 4.4 | 28 | 53.6 | 100.0 |
| 2024Q4 | CEX ∪ Alpha | 4.4 | 28 | 53.6 | 100.0 |
| 2025Q1 | CEX | 2.1 | 38 | 39.5 | 100.0 |
| 2025Q1 | CEX ∪ Alpha | 2.5 | 26 | 57.7 | 100.0 |
| 2025Q2 | CEX | -1.9 | 62 | 21.0 | 100.0 |
| 2025Q2 | CEX ∪ Alpha | 5.9 | 24 | 62.5 | 100.0 |
| 2025Q3 | CEX | 3.9 | 37 | 37.8 | 100.0 |
| 2025Q3 | CEX ∪ Alpha | 13.4 | 16 | 68.8 | 100.0 |
| 2025Q4 | CEX | -3.3 | 69 | 18.8 | 100.0 |
| 2025Q4 | CEX ∪ Alpha | 21.7 | 22 | 68.2 | 100.0 |
| 2026Q1 | CEX | -3.1 | 65 | 20.0 | 100.0 |
| 2026Q1 | CEX ∪ Alpha | 14.4 | 21 | 76.2 | 100.0 |
| 2026Q2 | CEX | -1.6 | 39 | 25.6 | 100.0 |
| 2026Q2 | CEX ∪ Alpha | 23.2 | 17 | 88.2 | 100.0 |
| 2026Q3 | CEX | 1.6 | 26 | 65.4 | 100.0 |
| 2026Q3 | CEX ∪ Alpha | 19.3 | 18 | 94.4 | 100.0 |
| 2026Q4 | CEX | -7.0 | 20 | 10.0 | 100.0 |
| 2026Q4 | CEX ∪ Alpha | 1.0 | 14 | 42.9 | 100.0 |

Cada trimestre se simula por separado: las posiciones abiertas al cierre del trimestre no cuentan (sesgo a la baja, mayor en el último trimestre, parcial). Sin gate de predicción: entra aunque la tasa sea baja (por eso la ocupación es 100 %).

Periodo walk-forward 2025-09-22 → 2026-10-22, ranking modelo vigente (pred > 0): **29.3 % anual**, 86 trades, ocupación de slots 99.8 % → exceso sobre la tasa libre de riesgo ≈ 25.3 pp.

## 2. Concentración del PnL (walk-forward, modelo vigente)

44 símbolos operados. Top-5: LYN +162, BULLA +137, SIREN +132, GUA +122, JELLYJELLY +99.

| se excluye del universo | anual % | trades |
| --- | --- | --- |
| nada | 29.3 | 86 |
| top-1 por PnL | 27.6 | 89 |
| top-3 por PnL | 24.6 | 90 |
| top-5 por PnL | 21.9 | 89 |
| top-10 por PnL | 20.4 | 86 |

Top-3 = 26.2 % del PnL (excluirlos re-asigna el slot al siguiente candidato).

Retorno mensual sobre el capital (por mes de cierre): 2025-09 +0.0 %, 2025-10 +2.9 %, 2025-11 +1.9 %, 2025-12 +1.8 %, 2026-01 +2.2 %, 2026-02 +1.4 %, 2026-03 +1.6 %, 2026-04 +4.4 %, 2026-05 +4.4 %, 2026-06 +1.9 %, 2026-07 +1.8 %, 2026-08 +2.0 %, 2026-09 +3.2 %, 2026-10 +2.0 %.
Meses negativos: 0 de 14.

## 3. Sensibilidad a la fee (entrada + salida)

| fee × | fee Alpha % | anual % |
| --- | --- | --- |
| 0.5 | 0.21 | 30.9 |
| 0.75 | 0.31 | 30.1 |
| 1.0 | 0.41 | 29.3 |
| 1.5 | 0.62 | 27.6 |
| 2.0 | 0.82 | 26.0 |

## 4. Capacidad: impacto on-chain medido (30 tokens Alpha operados, cotizaciones KyberSwap del 20261027)

Impacto de compra (una vía) vs una compra de 50 USD, mediana entre tokens: 1,000 USD → 0.25 %, 2,600 USD → 0.56 %, 5,000 USD → 0.92 %, 13,000 USD → 2.84 %, 26,000 USD → 4.77 %. Detalle por token en `cache/binance/alpha_depth_20261027.csv`.

La fee Alpha de 0.41 % corresponde a un nocional real de ~800 USD por pierna; para N mayor se suma 2·(impacto(N) − impacto(real)) (compra + venta). La liquidez es la de hoy (no hay histórico) y se aplica a todos los tokens Alpha del periodo.

| capital USD | slots | N/pierna USD | impacto 1 vía % | fee Alpha efectiva % | anual % |
| --- | --- | --- | --- | --- | --- |
| 5,200 | 5 | 520 | 0.25 | 0.41 | 29.3 |
| 10,400 | 5 | 1,040 | 0.26 | 0.44 | 29.1 |
| 26,000 | 5 | 2,600 | 0.56 | 1.04 | 24.3 |
| 52,000 | 10 | 2,600 | 0.56 | 1.04 | 20.5 |
| 104,000 | 10 | 5,200 | 0.99 | 1.91 | 14.0 |
| 260,000 | 20 | 6,500 | 1.44 | 2.81 | 3.8 |

Limitaciones: liquidez on-chain actual (no histórica) y un solo tramo por entrada (un TWAP de varios tramos espaciados reduce el impacto si el pool se re-arbitra entre tramos); lista Alpha actual (sesgo de supervivencia); sin basis de entrada/salida.