# Cross perp vs spot_perp Binance — 2026-10-25

Fase 6 del plan de rentabilidad: decidir con datos si reactivar `cross_exchange` (Binance/Bybit/OKX/Bitget) a igual riesgo (leverage ≤ 3x por pierna, DD ≤ 5 %). Solo investigación: no toca producción. Scripts: `scripts/cross_dataset.py`, `scripts/cross_backtest.py`.

## 1. Datos

- **binance**: 708 perps USDT del universo común (≥ 2 venues) con datos; desde 2025-01-01.
- **bybit**: 718 perps USDT del universo común (≥ 2 venues) con datos; desde 2025-01-01.
- **okx**: 484 perps USDT del universo común (≥ 2 venues) con datos; desde 2025-01-01.
- **bitget**: 722 perps USDT del universo común (≥ 2 venues) con datos; desde 2026-07-27.
- Spot_perp Binance: 878 perps (dataset con deslistados); pierna spot en CEX o, si no hay spot CEX, en Binance Alpha desde su `listingTime` (155,151 candidatos Alpha, fee 0.41 %).
- Rejilla 2025-01-01 → 2026-10-22 16:00 UTC. Walk-forward 12 meses 2025-09-01 → 2026-09-22; **hold-out intocado** 2026-09-22 → 2026-10-22.
- Candidatos (frontera de 8 h con alguna señal ≥ 0.03 %/día y liquidez ≥ 100,000 USD): spot_perp CEX ∪ Alpha 565,435, cross 854,754 (12 pares dirigidos de venues).
- Klines 1d para riesgo por pierna: 946 símbolos.
- Limitaciones: Bitget publica solo ~90 d de funding por API (sin archivo) → entra al final del periodo (ver §6). Bybit/OKX/Bitget solo incluyen perps listados hoy (sesgo de supervivencia leve); Binance incluye deslistados. Precio de ambas piernas = klines del venue de referencia (sin basis entre venues).

Motor vs `exit_policy_study` (H30, pares de 8 h, 57,922 entradas comunes): diferencia media -0.0001 pp, mediana |Δ| 0.0000 pp, 100.0 % idénticas.

## 2. Supuestos

- Cartera: 5 slots, 5,200 USD, decisión cada 8 h, un símbolo una vez.
- Nocional por slot: spot_perp N = slot·L/(L+1); cross N = slot·L/2 por pierna (ambas piernas con margen).
- Fees round-trip sobre N: spot_perp base 0.30 % / optimista 0.20 % (realizado); cross base = taker de ambos venues ×2 + slippage 0.02 %/fill (Binance–Bybit ≈ 0.29 %), optimista = maker 0.02 % + 0.01 %/fill (≈ 0.12 %).
- Cross: 1 USD por trade para pre-posicionar capital; rebalanceo 1 USD por transferencia.
- Seguro por pierna al 80% de la distancia a liquidación (1/L − 1%); evento = cierre anticipado + 0.3 % de N. Rebalanceo diario (variante `reb`) re-centra si el movimiento supera el 50% de la distancia.
- Salida: dos buckets de 8 h seguidos con Δf ≤ 0, tope 30 d; señales: `s3`/`s7` = Δf diario medio de 3/7 d, `cur` = Δ de la última tasa liquidada (diaria); spot_perp además `p_prod>0` (modelo vigente) y `heur` (v11.0), disponibles desde el inicio del walk-forward.

## 3. Rejilla completa (walk-forward 12 m y hold-out, fees base)

Cifras in-sample de selección: sirven para ver la forma, no para elegir (ver §4).

### spot_perp CEX (top 12 por anual WF + mejor DD-factible por leverage)

| config | WF anual % | WF DD % | WF trades | % gan. | hold d | eventos | rebal | WF anual % (opt) | hold-out PnL USD | hold-out anual % | hold-out DD % |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| s7≥0.03% L1 | 5.1 | 0.7 | 127 | 62.2 | 15.0 | 28 | 0 | 6.3 | 25.8 | 5.8 | 0.1 |
| s7≥0.03% L1 reb | 4.9 | 0.7 | 126 | 61.1 | 15.3 | 24 | 23 | 6.1 | 27.8 | 6.3 | 0.1 |
| s3≥0.03% L1 reb | 4.7 | 1.0 | 137 | 57.7 | 14.2 | 20 | 27 | 6.0 | 29.0 | 6.6 | 0.1 |
| s7≥0.03% L2 | 4.6 | 0.9 | 145 | 52.4 | 13.2 | 61 | 0 | 6.4 | 35.7 | 8.1 | 0.1 |
| s7≥0.03% L2 reb | 4.6 | 0.9 | 141 | 54.6 | 13.6 | 54 | 66 | 6.4 | 31.1 | 7.0 | 0.1 |
| s3≥0.03% L1 | 4.5 | 0.9 | 145 | 55.9 | 13.4 | 26 | 0 | 5.8 | 31.5 | 7.1 | 0.1 |
| s3≥0.03% L2 | 4.4 | 1.6 | 167 | 47.9 | 11.7 | 60 | 0 | 6.5 | 41.3 | 9.4 | 0.1 |
| s7≥0.06% L1 | 4.3 | 0.7 | 107 | 60.7 | 11.3 | 22 | 0 | 5.3 | 21.6 | 4.9 | 0.1 |
| s7≥0.06% L1 reb | 4.2 | 0.7 | 107 | 58.9 | 11.3 | 19 | 18 | 5.2 | 21.6 | 4.9 | 0.1 |
| p_prod>0 L1 | 3.8 | 0.3 | 83 | 71.1 | 19.2 | 16 | 0 | 4.6 | 25.8 | 5.8 | 0.1 |
| s7≥0.06% L2 | 3.8 | 0.9 | 128 | 51.6 | 9.1 | 53 | 0 | 5.4 | 32.4 | 7.3 | 0.1 |
| p_prod>0 L2 | 3.7 | 0.3 | 98 | 57.1 | 16.4 | 39 | 0 | 4.9 | 34.8 | 7.9 | 0.1 |
| s7≥0.03% L3 | 3.1 | 1.0 | 174 | 44.8 | 11.1 | 94 | 0 | 5.6 | 40.2 | 9.1 | 0.1 |

### spot_perp CEX ∪ Alpha (top 12 por anual WF + mejor DD-factible por leverage)

| config | WF anual % | WF DD % | WF trades | % gan. | hold d | eventos | rebal | WF anual % (opt) | hold-out PnL USD | hold-out anual % | hold-out DD % |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| cur≥0.06% L3 | 38.3 | 0.8 | 192 | 62.5 | 10.1 | 127 | 0 | 38.6 | 109.8 | 24.9 | 0.1 |
| cur≥0.03% L3 | 38.2 | 0.8 | 196 | 61.7 | 9.9 | 131 | 0 | 38.5 | 109.8 | 24.9 | 0.1 |
| s7≥0.03% L3 | 36.4 | 0.4 | 194 | 56.2 | 9.8 | 132 | 0 | 36.5 | 105.4 | 23.9 | 0.2 |
| s7≥0.06% L3 | 36.2 | 0.4 | 198 | 55.1 | 9.6 | 136 | 0 | 36.3 | 105.4 | 23.9 | 0.2 |
| s7≥0.10% L3 | 36.2 | 0.4 | 198 | 55.1 | 9.6 | 136 | 0 | 36.3 | 105.4 | 23.9 | 0.2 |
| s3≥0.10% L2 | 35.4 | 0.7 | 155 | 66.5 | 12.4 | 85 | 0 | 35.5 | 80.2 | 18.2 | 0.1 |
| s3≥0.03% L2 | 34.7 | 0.7 | 153 | 66.0 | 12.4 | 84 | 0 | 34.8 | 80.2 | 18.2 | 0.1 |
| s3≥0.06% L2 | 34.7 | 0.7 | 153 | 66.0 | 12.4 | 84 | 0 | 34.8 | 80.2 | 18.2 | 0.1 |
| cur≥0.10% L3 | 33.8 | 0.8 | 196 | 55.1 | 10.0 | 132 | 0 | 34.1 | 106.2 | 24.0 | 0.2 |
| s3≥0.06% L2 reb | 33.4 | 0.8 | 182 | 57.7 | 10.5 | 111 | 139 | 33.5 | 91.1 | 20.6 | 0.1 |
| s3≥0.03% L2 reb | 33.3 | 0.8 | 183 | 57.4 | 10.4 | 112 | 139 | 33.4 | 91.1 | 20.6 | 0.1 |
| s7≥0.06% L2 reb | 33.3 | 0.8 | 160 | 60.6 | 11.9 | 100 | 105 | 33.3 | 103.0 | 23.3 | 0.2 |
| s7≥0.06% L1 reb | 29.5 | 0.2 | 111 | 81.1 | 16.9 | 37 | 36 | 29.5 | 78.8 | 17.8 | 0.1 |

### cross (top 12 por anual WF + mejor DD-factible por leverage)

| config | WF anual % | WF DD % | WF trades | % gan. | hold d | eventos | rebal | WF anual % (opt) | hold-out PnL USD | hold-out anual % | hold-out DD % |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| cur≥0.03% L2 reb | 15.0 | 7.6 | 993 | 31.6 | 1.9 | 307 | 109 | 46.6 | 273.9 | 62.0 | 3.0 |
| cur≥0.06% L2 reb | 15.0 | 7.6 | 993 | 31.6 | 1.9 | 307 | 109 | 46.6 | 273.9 | 62.0 | 3.0 |
| cur≥0.10% L2 reb | 15.0 | 7.6 | 993 | 31.6 | 1.9 | 307 | 109 | 46.6 | 273.9 | 62.0 | 3.0 |
| cur≥0.03% L2 | 11.6 | 8.3 | 1030 | 31.9 | 1.9 | 351 | 0 | 44.3 | 268.6 | 60.8 | 3.0 |
| cur≥0.06% L2 | 11.6 | 8.3 | 1030 | 31.9 | 1.9 | 351 | 0 | 44.3 | 268.6 | 60.8 | 3.0 |
| cur≥0.10% L2 | 11.6 | 8.3 | 1030 | 31.9 | 1.9 | 351 | 0 | 44.3 | 268.6 | 60.8 | 3.0 |
| cur≥0.03% L1 reb | 11.1 | 3.9 | 831 | 31.5 | 2.3 | 96 | 49 | 24.3 | 103.6 | 23.5 | 1.9 |
| cur≥0.06% L1 reb | 11.1 | 3.9 | 831 | 31.5 | 2.3 | 96 | 49 | 24.3 | 103.6 | 23.5 | 1.9 |
| cur≥0.10% L1 reb | 11.1 | 3.9 | 831 | 31.5 | 2.3 | 96 | 49 | 24.3 | 103.6 | 23.5 | 1.9 |
| cur≥0.03% L1 | 9.5 | 4.5 | 814 | 31.9 | 2.4 | 104 | 0 | 22.4 | 99.0 | 22.4 | 1.9 |
| cur≥0.06% L1 | 9.5 | 4.5 | 814 | 31.9 | 2.4 | 104 | 0 | 22.4 | 99.0 | 22.4 | 1.9 |
| cur≥0.10% L1 | 9.5 | 4.5 | 814 | 31.9 | 2.4 | 104 | 0 | 22.4 | 99.0 | 22.4 | 1.9 |

## 4. Walk-forward OOS (config elegida cada mes con datos previos, DD ≤ 5 %)

### spot_perp CEX

| mes | config elegida (datos previos) | PnL base USD | anual % | DD % | trades | eventos SL | PnL opt USD |
| --- | --- | --- | --- | --- | --- | --- | --- |
| 2025-09 | cur≥0.10% L2 | 20.2 | 4.7 | 0.3 | 19 | 13 | 33.7 |
| 2025-10 | cur≥0.10% L1 reb | 2.6 | 0.6 | 0.3 | 11 | 1 | 7.8 |
| 2025-11 | cur≥0.10% L2 | 65.3 | 15.3 | 0.2 | 12 | 2 | 74.0 |
| 2025-12 | cur≥0.10% L2 | -9.3 | -2.1 | 0.4 | 20 | 2 | 4.3 |
| 2026-01 | cur≥0.10% L2 | -97.2 | -22.0 | 2.0 | 18 | 3 | -83.7 |
| 2026-02 | s7≥0.06% L1 | 19.3 | 4.8 | 0.0 | 5 | 0 | 22.1 |
| 2026-03 | s7≥0.06% L1 | 43.8 | 9.9 | 0.1 | 13 | 4 | 49.7 |
| 2026-04 | s7≥0.06% L1 | 15.7 | 3.7 | 0.2 | 7 | 0 | 19.9 |
| 2026-05 | s7≥0.06% L1 | 24.3 | 5.5 | 0.1 | 7 | 1 | 28.0 |
| 2026-06 | s7≥0.06% L1 | 15.5 | 3.6 | 0.1 | 9 | 2 | 19.2 |
| 2026-07 | s7≥0.06% L1 | -5.8 | -1.3 | 0.7 | 8 | 0 | -1.6 |
| 2026-08 | s7≥0.06% L1 | 28.4 | 3.8 | 0.5 | 18 | 6 | 38.3 |

### spot_perp CEX ∪ Alpha

| mes | config elegida (datos previos) | PnL base USD | anual % | DD % | trades | eventos SL | PnL opt USD |
| --- | --- | --- | --- | --- | --- | --- | --- |
| 2025-09 | cur≥0.06% L2 | 161.9 | 37.9 | 0.1 | 11 | 6 | 163.2 |
| 2025-10 | cur≥0.06% L2 | 113.3 | 25.7 | 0.3 | 14 | 8 | 116.1 |
| 2025-11 | p_prod>0 L3 | 149.2 | 34.9 | 0.1 | 13 | 7 | 149.2 |
| 2025-12 | p_prod>0 L3 | 97.4 | 22.0 | 0.2 | 10 | 6 | 97.4 |
| 2026-01 | p_prod>0 L3 | 140.8 | 31.9 | 0.4 | 14 | 11 | 140.8 |
| 2026-02 | p_prod>0 L3 | 62.2 | 15.6 | 0.5 | 19 | 15 | 62.2 |
| 2026-03 | p_prod>0 L2 | 157.6 | 35.7 | 0.1 | 7 | 2 | 157.6 |
| 2026-04 | p_prod>0 L2 | 131.6 | 30.8 | 0.2 | 25 | 20 | 131.6 |
| 2026-05 | p_prod>0 L3 | 188.0 | 42.6 | 0.2 | 8 | 5 | 188.0 |
| 2026-06 | p_prod>0 L3 | 101.1 | 23.6 | 0.6 | 29 | 23 | 101.1 |
| 2026-07 | p_prod>0 L2 | 86.9 | 19.7 | 0.2 | 15 | 9 | 86.9 |
| 2026-08 | p_prod>0 L2 | 298.2 | 40.3 | 0.2 | 12 | 9 | 298.2 |

### cross

| mes | config elegida (datos previos) | PnL base USD | anual % | DD % | trades | eventos SL | PnL opt USD |
| --- | --- | --- | --- | --- | --- | --- | --- |
| 2025-09 | — (ninguna con DD ≤ 5 %) | 0 | 0 | 0 | 0 | 0 | 0 |
| 2025-10 | — (ninguna con DD ≤ 5 %) | 0 | 0 | 0 | 0 | 0 | 0 |
| 2025-11 | — (ninguna con DD ≤ 5 %) | 0 | 0 | 0 | 0 | 0 | 0 |
| 2025-12 | — (ninguna con DD ≤ 5 %) | 0 | 0 | 0 | 0 | 0 | 0 |
| 2026-01 | — (ninguna con DD ≤ 5 %) | 0 | 0 | 0 | 0 | 0 | 0 |
| 2026-02 | — (ninguna con DD ≤ 5 %) | 0 | 0 | 0 | 0 | 0 | 0 |
| 2026-03 | — (ninguna con DD ≤ 5 %) | 0 | 0 | 0 | 0 | 0 | 0 |
| 2026-04 | — (ninguna con DD ≤ 5 %) | 0 | 0 | 0 | 0 | 0 | 0 |
| 2026-05 | — (ninguna con DD ≤ 5 %) | 0 | 0 | 0 | 0 | 0 | 0 |
| 2026-06 | — (ninguna con DD ≤ 5 %) | 0 | 0 | 0 | 0 | 0 | 0 |
| 2026-07 | — (ninguna con DD ≤ 5 %) | 0 | 0 | 0 | 0 | 0 | 0 |
| 2026-08 | — (ninguna con DD ≤ 5 %) | 0 | 0 | 0 | 0 | 0 | 0 |

**OOS 12 m (suma de los meses):** spot_perp CEX 122.8 USD (2.2 % anual; opt 211.5 USD) · spot_perp CEX ∪ Alpha 1,688.0 USD (30.7 % anual; opt 1,692.2 USD) · cross 0.0 USD (0.0 % anual; opt 0.0 USD).

## 5. Hold-out intocado (30 d)

| familia | config (elegida con todo lo anterior) | pre-hold-out anual % | pre DD % | hold-out PnL base | anual % | DD % | trades | PnL opt |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| spot_perp CEX | s7≥0.03% L1 | 4.5 | 0.7 | 25.8 | 5.8 | 0.1 | 6 | 30.2 |
| spot_perp CEX ∪ Alpha | p_prod>0 L2 | 32.6 | 0.4 | 66.1 | 15.0 | 0.1 | 12 | 66.1 |
| cross | — | | | | | | | |

## 6. Desglose cross (config `cur≥0.03% L1 reb`, WF + hold-out)

| par (long>short) | trades | PnL USD | % ganadoras |
| --- | --- | --- | --- |
| binance>bitget | 18 | -10.6 | 38.9 |
| binance>bybit | 179 | 77.4 | 29.6 |
| binance>okx | 113 | -111.4 | 31.9 |
| bitget>binance | 11 | -14.0 | 18.2 |
| bitget>bybit | 30 | -3.5 | 40.0 |
| bitget>okx | 6 | -12.1 | 16.7 |
| bybit>binance | 210 | 58.5 | 28.1 |
| bybit>bitget | 39 | -80.2 | 28.2 |
| bybit>okx | 65 | -134.4 | 32.3 |
| okx>binance | 168 | 982.0 | 36.9 |
| okx>bitget | 5 | -8.9 | 20.0 |
| okx>bybit | 58 | -27.0 | 29.3 |

- Trades en perps **sin spot en Binance** (no replicables con spot_perp): 445 de 902, PnL 297.1 USD.
- Perps TradFi (acciones/índices/metales) en el universo común: AAPL, AMD, AMZN, AVGO, BABA, CL, COIN, COPPER, CRCL, EWJ, EWY, GME, GOOGL, HOOD, INTC, META, MSFT, MSTR, MU, NATGAS, NFLX, NVDA, ORCL, PAXG, PLTR, QQQ, SPX, SPY, TSLA, TSM, XAG, XAU, XAUT; trades: 26, PnL -55.8 USD.
- Mejores símbolos: FLOWUSDT 389.7, DRIFTUSDT 220.1, ONEUSDT 193.2, HUSDT 99.1, RIVERUSDT 87.9
- Peores símbolos: LABUSDT -79.0, RESOLVUSDT -54.1, BLURUSDT -32.4, ENSOUSDT -30.6, MMTUSDT -30.3

### Dependencia de outliers (config `cur≥0.03% L1 reb` y su par L2)

| config | universo | pre-WF anual % | pre-WF DD % | WF anual % | WF DD % | hold-out anual % | WF anual % (opt) |
| --- | --- | --- | --- | --- | --- | --- | --- |
| cur L1 reb | todo | -12.1 | 8.0 | 11.1 | 3.9 | 23.5 | 24.3 |
| cur L1 reb | sin top-3 símbolos (FLOW, DRIFT, H) | -12.1 | 8.0 | -0.7 | 5.1 | 23.5 | 12.1 |
| cur L1 reb | sin top-10 símbolos | -11.7 | 7.7 | -7.6 | 8.0 | -15.1 | 5.6 |
| cur L2 reb | todo | -21.6 | 14.3 | 15.0 | 7.6 | 62.0 | 46.6 |
| cur L2 reb | sin top-3 símbolos (FLOW, DRIFT, H) | -22.7 | 15.0 | -11.3 | 14.8 | 62.0 | 20.3 |
| cur L2 reb | sin top-10 símbolos | -24.3 | 16.0 | -22.9 | 25.3 | -27.3 | 8.3 |

### Bitget (ventana 2026-08-04 → 2026-10-22, config `cur≥0.03% L1 reb`)

| variante | PnL USD | anual % | DD % | trades | trades con Bitget |
| --- | --- | --- | --- | --- | --- |
| con Bitget | 18.1 | 1.6 | 2.6 | 194 | 103 |
| sin Bitget | 97.3 | 8.6 | 2.2 | 172 | 0 |

## 7. Veredicto

_(redactado a mano tras revisar las cifras; el script no lo genera)_

**No reactivar `cross_exchange`** (ni con Bybit/OKX/Bitget ni con leverage). Mantener
`ARBITRAGE_MODES=spot_perp` y concentrar el capital en spot_perp **CEX ∪ Alpha** a 2x.

1. **Gate a igual riesgo:** ninguna config cross pasa DD ≤ 5 % con los datos anteriores a ningún
   mes del walk-forward → el walk-forward OOS no opera cross (0 USD). spot_perp CEX ∪ Alpha: 1.688 USD
   OOS en 12 meses (30,7 % anual, DD mensual ≤ 0,6 %) y hold-out +66 USD (15,0 % anual, DD 0,1 %) con
   `p_prod>0 L2`. spot_perp solo CEX: 2,2 % OOS / 5,8 % hold-out → el edge está en Alpha, como en
   `reports/attribution_20261025.md`.
2. **El cross no tiene edge sistemático:** la mejor config in-sample (`cur L1 reb`) da 11,1 % anual en
   WF pero −12,1 % (DD 8 %) en ene–sep 2025, 31 % de trades ganadores y hold medio de ~2 d (los fees
   taker de 4 fills se comen el diferencial). El PnL lo hacen episodios de funding extremo
   (FLOW tras su colapso de dic-2025: OKX −91 % acumulado; DRIFT, H, ONE): **sin los 3 mejores
   símbolos el WF cae a −0,7 % (L1) / −11,3 % (L2)**; sin los 10 mejores, −7,6 % / −22,9 %.
   Coincide con la literatura (~40 % de las oportunidades cross rentables tras costos) y con el IC
   en vivo −0,15.
3. **Leverage:** el ROC cross escala con Δf·L/2, pero el DD y los eventos de seguro escalan más
   rápido (L2: DD 7,6 % WF, 14 % pre-WF; L3 nunca entra al top). En spot_perp CEX ∪ Alpha el
   walk-forward elige 2–3x (`p_prod>0`); in-sample L3 ≈ 38 % vs L2 ≈ 35 % vs L1 ≈ 30 % anual, pero
   aquí el costo de evento es simétrico (0,3 %); con la asimetría on-chain de BSC
   (`reports/leverage_study_20261025_bsc.md`) gana 2x → **recomendado 2x en Alpha, hasta 3x en CEX**.
4. **Venues:** solo `okx>binance` suma (+982 USD, casi todo FLOW); `bybit>okx`, `binance>okx` y
   `bybit>bitget` restan. Bitget, en su única ventana con datos (~80 d), **resta**: 1,6 % anual con
   Bitget vs 8,6 % sin él.
5. **Perps sin spot:** la mitad de los trades cross (445/902) son perps sin spot en Binance
   (+297 USD), pero es el mismo efecto de episodios extremos. Los 33 perps TradFi comunes
   (TSLA, NVDA, XAU, SPY…) dieron 26 trades y −56 USD: no son candidatos con este método.
6. **Fees maker** (escenario optimista, 0,12 % round-trip) vuelven el cross positivo (24 % L1 /
   47 % L2 en WF; 12 % / 20 % sin los 3 mejores símbolos), pero exige llenado maker en ambas piernas
   con holds de ~2 d, no medido en vivo, y aun así queda por debajo de spot_perp CEX ∪ Alpha con más
   DD.

**Qué reabriría la pregunta:** (a) ejecución maker medida en vivo en ambas piernas (fee ≤ 0,12 %);
(b) un detector de episodios de funding extremo (|Δf| > 0,5 %/8 h por colapso o desliste) como
**alerta manual**, no como estrategia sistemática; (c) ≥ 12 meses de historial de Bitget (hoy la
API solo guarda ~90 d: `scripts/cross_dataset.py` es incremental, conviene correrlo cada mes).

Limitaciones: precio de ambas piernas = klines 1d del venue de referencia (sin basis entre venues ni
mechas intradía), latencia de rebalanceo de 1 d, sesgo de supervivencia leve en Bybit/OKX/Bitget y en
la lista Alpha, y la selección de config del walk-forward concatena meses de corridas continuas
distintas (aproximación).
