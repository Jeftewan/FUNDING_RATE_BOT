# Estudio de apalancamiento con seguros en la cartera — 20261025

Universo: 21 tokens con spot CEX con velas 1m (A2Z, ATA, BANANAS31, BEAMX, CATI, CHESS, DF, FIS, FLM, FORM, FUN, GIGGLE, HFT, MARSCOIN, NKN, PERP, REI, SYS, THE, TST, VOXEL), 2025-10 → 2026-09. SL al 80% del camino a liquidación (mmr 1%), venta spot δ=0.5%, fee 0.30%, salida H30. Cartera 5 slots, 5.200 USD; el nocional por slot es C/5·L/(L+1). PnL en USD sobre el capital total.

| leverage | ranking | PnL USD | anual % | trades | % ganadoras | hold d | % trades con evento | DD % |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| 1x | modelo vigente (pred>0) | 106.9 | 2.1 | 27 | 85.2 | 21.1 | 4 (universo) | 0.1 |
| 1x | tasa vigente | 140.9 | 2.7 | 101 | 59.4 | 18.3 | 3 (universo) | 0.1 |
| 2x | modelo vigente (pred>0) | 150.5 | 2.9 | 31 | 80.6 | 18.0 | 16 (universo) | 0.5 |
| 2x | tasa vigente | 213.1 | 4.1 | 111 | 51.4 | 16.7 | 14 (universo) | 0.4 |
| 3x | modelo vigente (pred>0) | 154.5 | 3.0 | 34 | 64.7 | 14.9 | 35 (universo) | 0.9 |
| 3x | tasa vigente | 252.8 | 4.9 | 127 | 55.9 | 14.7 | 25 (universo) | 0.5 |

Lectura: comparar filas del mismo ranking. El universo es chico (sesgo de supervivencia de la lista Alpha y de los 20 tokens de mayor carry), así que la cifra absoluta es optimista; la diferencia relativa entre leverages es lo que decide.
