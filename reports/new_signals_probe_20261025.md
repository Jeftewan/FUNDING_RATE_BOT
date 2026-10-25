# Sonda de señales nuevas — 20261025

Filas (settlements con señales y label y7 completo): 37,691 (8,533 Alpha), 2026-09-25 → 2026-10-18, 24 días. y7 = funding liquidado (t, t+7 d] por nocional. IC = Spearman diario promedio (t sobre días; los labels de 7 d se solapan → t inflado, leer como orientativo).

| señal | cobertura % | IC vs y7 | t | IC vs residuo del modelo | t | días |
| --- | --- | --- | --- | --- | --- | --- |
| **modelo vigente (p_prod)** | 100 | 0.600 | 19.8 | — | — | 24 |
| basis_pct | 100 | 0.305 | 9.7 | 0.171 | 11.2 | 24 |
| oi_change_1h_pct | 100 | 0.012 | 2.1 | -0.012 | -1.8 | 24 |
| oi_change_24h_pct | 94 | 0.036 | 2.6 | -0.017 | -1.4 | 23 |
| log_oi_usd | 100 | 0.048 | 3.6 | 0.125 | 7.7 | 24 |
| taker_buy_sell_ratio | 99 | -0.013 | -1.8 | -0.014 | -2.0 | 24 |
| top_long_short_pos_ratio | 100 | -0.184 | -4.2 | -0.114 | -3.9 | 24 |
| volatility_7d | 100 | -0.022 | -1.1 | 0.053 | 2.1 | 24 |

## HistGB chico (train < 2026-10-02, test ≥ 2026-10-09, purga 7 d)

| modelo | IC test vs y7 |
| --- | --- |
| modelo vigente | 0.433 |
| 14 features + p_prod | 0.264 |
| 14 features + p_prod + señales | 0.294 |

## Blend modelo + basis (corte 2026-10-07)

| w | IC A (elige w) | top-5 y7 % A (elige w) | IC B (evalúa) | top-5 y7 % B (evalúa) |
| --- | --- | --- | --- | --- |
| 0.0 | 0.721 | 1.678 | 0.474 | 0.604 |
| 0.1 | 0.728 | 0.653 | 0.479 | 0.249 |
| 0.2 | 0.730 | 0.653 | 0.480 | 0.319 |
| 0.3 | 0.728 | 0.663 | 0.480 | 0.235 |
| 0.5 | 0.720 | 0.648 | 0.471 | 0.241 |
| 1.0 | 0.678 | 0.552 | 0.422 | 0.143 |

Lectura: una señal vale la pena si su IC sobre el **residuo** es estable y del mismo signo; aun así no se exporta un modelo hasta tener ≥ 3 meses de señales y pasar el gate de ml_v2.py.
