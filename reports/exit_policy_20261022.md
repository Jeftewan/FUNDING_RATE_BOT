# Estudio de regla de salida — 2026-10-22

Filas elegibles 426,825 desde 2025-09-22 (incluye hold-out desde 2026-09-22). Neto por nocional; cartera 5 slots, 5.200 USD, 1x.

## Neto medio por trade del top-10 % diario (en % del nocional)

| ranking | A: 1er ≤0 | B: 2 seguidos ≤0 | C: 14 d fijo | H30: hasta 30 d (B) |
| --- | --- | --- | --- | --- |
| heur | -0.008 | 0.014 | -0.228 | 0.238 |
| p_prod | 0.019 | 0.035 | -0.194 | 0.268 |
| p_new | 0.018 | 0.035 | -0.260 | 0.260 |

## Cartera simulada por política (walk-forward 12 m + hold-out)

| ranking | política | PnL USD | anual % | trades | % ganadoras | hold medio d |
| --- | --- | --- | --- | --- | --- | --- |
| heur | A | 34.7 | 0.6 | 255 | 43.5 | 7.5 |
| heur | B | 61.5 | 1.1 | 189 | 62.4 | 10.2 |
| heur | C | -111.4 | -2.0 | 137 | 81.8 | 14.0 |
| heur | H30 | 150.5 | 2.7 | 117 | 57.3 | 16.7 |
| p_prod | A | 92.2 | 1.6 | 231 | 50.6 | 8.3 |
| p_prod+pred>0 | A | 145.8 | 2.6 | 160 | 56.2 | 8.7 |
| p_prod | B | 137.6 | 2.4 | 177 | 66.1 | 10.9 |
| p_prod+pred>0 | B | 150.5 | 2.7 | 137 | 73.7 | 11.1 |
| p_prod | C | -108.4 | -2.0 | 137 | 83.9 | 14.0 |
| p_prod+pred>0 | C | -178.5 | -3.2 | 113 | 81.4 | 14.0 |
| p_prod | H30 | 180.2 | 3.2 | 105 | 66.7 | 18.6 |
| p_prod+pred>0 | H30 | 209.7 | 3.7 | 88 | 71.6 | 18.7 |
| p_new | A | 62.5 | 1.1 | 248 | 46.8 | 7.7 |
| p_new+pred>0 | A | 73.2 | 1.3 | 241 | 48.1 | 8.0 |
| p_new | B | 126.0 | 2.2 | 186 | 65.6 | 10.4 |
| p_new+pred>0 | B | 134.8 | 2.4 | 180 | 67.8 | 10.7 |
| p_new | C | -451.0 | -8.1 | 137 | 78.1 | 14.0 |
| p_new+pred>0 | C | -335.4 | -6.0 | 135 | 79.3 | 14.0 |
| p_new | H30 | 191.7 | 3.4 | 115 | 62.6 | 17.0 |
| p_new+pred>0 | H30 | 202.7 | 3.6 | 108 | 66.7 | 18.0 |

**Mejor combinación:** ranking `p_prod+pred>0` + política `H30` → 209.7 USD (3.7 % anual).

## Piso de rendimiento (política H30, ranking p_prod, gate pred>0)

| piso tasa diaria (vigente y media ventana) | PnL USD | anual % | trades | % ganadoras | hold d |
| --- | --- | --- | --- | --- | --- |
| 0.00 %/día | 209.7 | 3.7 | 88 | 71.6 | 18.7 |
| 0.05 %/día | 139.5 | 2.5 | 59 | 61.0 | 15.1 |
| 0.10 %/día | 70.4 | 1.2 | 32 | 62.5 | 14.0 |
| 0.20 %/día | 29.1 | 0.5 | 10 | 60.0 | 10.2 |
| 0.30 %/día | 9.2 | 0.2 | 5 | 40.0 | 4.1 |
| rank por tasa vigente, pred>0, media ≥ 0.05 %/día | 145.1 | 2.6 | 65 | 66.2 | 16.0 |
| rank por tasa vigente, pred>0, media ≥ 0.10 %/día | 70.0 | 1.2 | 37 | 67.6 | 15.0 |
| rank por tasa vigente, pred>0, media ≥ 0.20 %/día | 15.6 | 0.3 | 14 | 64.3 | 11.5 |