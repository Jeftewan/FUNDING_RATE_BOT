# Atribución realizado vs simulado — 2026-10-25

Cierres en `user_history`: 33 · pruebas excluidas (capital < 50 USD): 4 · analizados: 29.

## 1. PnL de precio anómalo (revisar antes de usarlo)

| id | símbolo | capital | price_pnl | net_earned | cierre |
| --- | --- | --- | --- | --- | --- |
| 46 | JCT | 1260 | +438.44 | +438.55 | 2026-10-24 |

En un hedge 1x el PnL de precio debería ser del orden del basis (±1 %). Estas cifras se excluyen del análisis (se usa solo funding − fees).

## 2. Funding neto por venue (sin PnL de precio)

| venue | trades | capital medio | funding neto USD | ROC anual % (capital·tiempo) | hold medio d |
| --- | --- | --- | --- | --- | --- |
| Binance | 21 | 1688 | 427.6 | 32.0 | 11.5 |
| Bitget | 8 | 1463 | 144.3 | 52.2 | 8.4 |
| **total** | 29 | 1626 | 571.9 | 35.4 | 10.7 |

## 3. Trades Binance: ¿qué veía el simulador al entrar?

17 de 21 trades Binance tienen fila en el dataset del simulador (slot de 8 h de la apertura).

| id | símbolo | apertura | tasa diaria % | rango tasa | rango modelo | rango heur | n slot | spot | funding real (rec/dataset) % | ROC real anual % | sim H30 neto % / d |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| 16 | BULLA | 2026-04-20 20 | 0.756 | 2.0 | 1.0 | nan | 344.0 | Alpha | 5.86 / 8.30 | 45 | 8.84 / 30.0 |
| 15 | ARIA | 2026-05-01 19 | 0.405 | 7.0 | 3.0 | nan | 353.0 | Alpha | 3.81 / 4.07 | 51 | 4.37 / 30.0 |
| 18 | SOON | 2026-05-14 14 | 0.030 | 32.0 | 26.0 | nan | 343.0 | Alpha | -1.52 / 0.21 | -56 | 1.36 / 30.0 |
| 26 | BSB | 2026-06-16 21 | 0.252 | 6.0 | 24.0 | nan | 349.0 | Alpha | 0.55 / 0.55 | 8 | 1.82 / 30.0 |
| 30 | SIREN | 2026-06-17 20 | 0.846 | 1.0 | 2.0 | nan | 398.0 | Alpha | 6.63 / 6.56 | 89 | 10.45 / 30.0 |
| 28 | ESPORTS | 2026-06-22 16 | 0.049 | 31.0 | 2.0 | nan | 327.0 | Alpha | 3.46 / 2.07 | 74 | 1.63 / 4.6 |
| 27 | SLX | 2026-06-22 20 | 0.308 | 5.0 | 16.0 | nan | 327.0 | Alpha | 0.55 / 0.50 | 10 | 0.04 / 6.7 |
| 29 | IN | 2026-06-27 19 | 0.449 | 2.0 | 7.0 | nan | 340.0 | Alpha | 0.45 / -9.52 | -1 | -3.89 / 3.0 |
| 31 | VELVET | 2026-06-29 16 | 0.348 | 5.0 | 10.0 | nan | 381.0 | Alpha | 0.51 / 0.52 | 15 | 0.17 / 3.3 |
| 33 | ESPORTS | 2026-07-05 20 | 0.425 | 2.0 | 1.0 | nan | 398.0 | Alpha | 3.11 / 3.14 | 37 | 3.27 / 11.8 |
| 35 | BULLA | 2026-07-20 21 | 0.386 | 3.0 | 2.0 | nan | 382.0 | Alpha | 1.33 / 1.33 | 31 | 3.97 / 30.0 |
| 38 | HANA | 2026-07-28 02 | 0.157 | 16.0 | 2.0 | nan | 321.0 | Alpha | 3.21 / 3.21 | 22 | 3.97 / 30.0 |
| 37 | BTW | 2026-08-07 02 | 0.306 | 9.0 | 1.0 | nan | 357.0 | Alpha | 4.87 / 4.86 | 80 | 9.22 / 30.0 |
| 39 | ESPORTS | 2026-08-18 00 | 0.561 | 2.0 | 1.0 | nan | 345.0 | Alpha | 8.08 / 8.09 | 51 | 8.36 / 29.2 |
| 40 | STAR | 2026-08-18 00 | 0.228 | 10.0 | 4.0 | nan | 345.0 | Alpha | 3.96 / 3.96 | 22 | 2.80 / 15.3 |
| 41 | KOMA | 2026-09-22 01 | 0.315 | 10.0 | 5.0 | nan | 455.0 | Alpha | 2.93 / 2.95 | 15 | 2.87 / 29.5 |
| 42 | SIREN | 2026-09-22 01 | 0.345 | 6.0 | 4.0 | nan | 455.0 | Alpha | 4.70 / 4.69 | 30 | 4.05 / 24.3 |

## 4. Simulador por universo de spot

Política H30, 5 slots, 5.200 USD, 1x, decisión cada 8 h. Fee CEX 0.30 % (supuesto); fee Alpha = mediana real de los trades Binance = 0.41 % del nocional. Alpha = token en la lista pública de Binance Alpha con `listingTime` ≤ fecha y sin spot CEX.

| periodo | universo | ranking | PnL USD | anual % | trades | % ganadoras | hold d |
| --- | --- | --- | --- | --- | --- | --- | --- |
| periodo real 2026-04-20→2026-10-22 | CEX | p_prod (pred>0) | 117.7 | 4.5 | 38 | 78.9 | 21.9 |
| periodo real 2026-04-20→2026-10-22 | CEX | tasa vigente | 37.0 | 1.4 | 77 | 41.6 | 11.8 |
| periodo real 2026-04-20→2026-10-22 | Alpha | p_prod (pred>0) | 694.0 | 26.4 | 44 | 86.4 | 20.7 |
| periodo real 2026-04-20→2026-10-22 | Alpha | tasa vigente | 682.8 | 25.9 | 56 | 73.2 | 16.3 |
| periodo real 2026-04-20→2026-10-22 | CEX ∪ Alpha | p_prod (pred>0) | 694.0 | 26.4 | 44 | 86.4 | 20.7 |
| periodo real 2026-04-20→2026-10-22 | CEX ∪ Alpha | tasa vigente | 678.8 | 25.8 | 59 | 69.5 | 15.4 |
| walk-forward completo 2025-09-22→2026-10-22 | CEX | p_prod (pred>0) | 209.7 | 3.7 | 88 | 71.6 | 18.7 |
| walk-forward completo 2025-09-22→2026-10-22 | CEX | tasa vigente | 64.6 | 1.1 | 187 | 38.0 | 10.3 |
| walk-forward completo 2025-09-22→2026-10-22 | Alpha | p_prod (pred>0) | 1637.1 | 29.1 | 94 | 85.1 | 20.7 |
| walk-forward completo 2025-09-22→2026-10-22 | Alpha | tasa vigente | 1564.3 | 27.8 | 122 | 71.3 | 16.0 |
| walk-forward completo 2025-09-22→2026-10-22 | CEX ∪ Alpha | p_prod (pred>0) | 1682.5 | 29.9 | 95 | 84.2 | 20.5 |
| walk-forward completo 2025-09-22→2026-10-22 | CEX ∪ Alpha | tasa vigente | 1465.5 | 26.0 | 121 | 71.9 | 16.1 |

## 5. Mismas entradas reales (Binance), salida del simulador (H30)

17 trades: real 430.3 USD (hold medio 13.8 d) vs simulado 569.2 USD (hold medio 21.6 d) con el mismo nocional.

## Lectura

- El simulador de `exit_policy_study.py`/`ml_binance.py` filtra `has_spot` (spot CEX) y deja fuera los tokens cuyo spot solo existe en Binance Alpha, que es donde se hicieron los trades rentables. Con ese universo la cartera simulada se acerca al ROC realizado.
- Sesgos que quedan: la lista Alpha es la de hoy (tokens borrados no aparecen → supervivencia), sin liquidez histórica del pool, y la fee Alpha es la mediana de pocos trades.
- Todo estudio o gate de modelo nuevo debe correr sobre CEX ∪ Alpha con la fee Alpha real.
