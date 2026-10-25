# Estudio de apalancamiento con seguros en la cartera — 20261025

Universo: 21 tokens Alpha con velas 1m (4, AGT, AIN, AIOT, B2, BAS, BID, BLUAI, BTW, COLLECT, ESPORTS, HANA, JCT, KOMA, SIREN, SKYAI, STAR, TA, TAC, TAKE, ZEST), 2025-10 → 2026-09. SL al 80% del camino a liquidación (mmr 1%), venta spot δ=0.5%, fee Alpha 0.41%, salida H30. Cartera 5 slots, 5.200 USD; el nocional por slot es C/5·L/(L+1). PnL en USD sobre el capital total.

| leverage | ranking | PnL USD | anual % | trades | % ganadoras | hold d | % trades con evento | DD % |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| 1x | modelo vigente (pred>0) | 1725.4 | 33.2 | 90 | 84.4 | 20.6 | 23 (universo) | 0.4 |
| 1x | tasa vigente | 1720.3 | 33.1 | 96 | 84.4 | 19.3 | 23 (universo) | 0.4 |
| 2x | modelo vigente (pred>0) | 1863.7 | 35.9 | 111 | 79.3 | 16.9 | 43 (universo) | 1.3 |
| 2x | tasa vigente | 2263.7 | 43.6 | 115 | 76.5 | 16.2 | 43 (universo) | 0.6 |
| 3x | modelo vigente (pred>0) | 1245.0 | 24.0 | 141 | 69.5 | 13.2 | 57 (universo) | 1.5 |
| 3x | tasa vigente | 1431.4 | 27.5 | 145 | 68.3 | 12.9 | 56 (universo) | 2.0 |

Lectura: comparar filas del mismo ranking. El universo es chico (sesgo de supervivencia de la lista Alpha y de los 20 tokens de mayor carry), así que la cifra absoluta es optimista; la diferencia relativa entre leverages es lo que decide.
