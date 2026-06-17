# Simulación de hold por decil de score — 2026-06-16

**Fuente:** `reports/backtest_20260615_features.csv` (823,299 filas) | **Universo:** `positive` | Score v10.6 | net direccion-ajustada.
Hold = entrar, mantener W intervalos, salir. Net = `realizado_W − fee` (en puntos %).

---

## 1. Headline: spread top-bottom y monotonicidad por (ventana × fee)

`net_d10` = net medio del mejor decil de score; `net_d1` = el peor.
`rho_monotonia` = Spearman(decil, net medio): 1.0 = el score ordena perfecto la ganancia.

| ventana | fee | rho_monotonia | net_d1 | net_d10 | spread_d10_d1 | sharpe_d10 | %rent_d10 |
| --- | --- | --- | --- | --- | --- | --- | --- |
| 8i (~3d) | 0.05 | 0.636 | -0.0902 | -0.0214 | 0.0688 | -0.18 | 11.9 |
| 8i (~3d) | 0.10 | 0.636 | -0.1402 | -0.0714 | 0.0688 | -0.601 | 1.0 |
| 8i (~3d) | 0.20 | 0.636 | -0.2402 | -0.1714 | 0.0688 | -1.443 | 0.3 |
| 8i (~3d) | 0.30 | 0.636 | -0.3402 | -0.2714 | 0.0688 | -2.285 | 0.1 |
| 24i (~8d) | 0.05 | 0.673 | -0.2166 | 0.0272 | 0.2438 | 0.088 | 70.3 |
| 24i (~8d) | 0.10 | 0.673 | -0.2666 | -0.0228 | 0.2438 | -0.074 | 64.3 |
| 24i (~8d) | 0.20 | 0.673 | -0.3666 | -0.1228 | 0.2438 | -0.397 | 11.5 |
| 24i (~8d) | 0.30 | 0.673 | -0.4666 | -0.2228 | 0.2438 | -0.721 | 1.5 |
| 72i (~24d) | 0.05 | 0.709 | -0.5181 | 0.1477 | 0.6658 | 0.154 | 88.1 |
| 72i (~24d) | 0.10 | 0.709 | -0.5681 | 0.0977 | 0.6658 | 0.102 | 69.7 |
| 72i (~24d) | 0.20 | 0.709 | -0.6681 | -0.0023 | 0.6658 | -0.002 | 64.4 |
| 72i (~24d) | 0.30 | 0.709 | -0.7681 | -0.1023 | 0.6658 | -0.107 | 57.4 |

**Cómo leerlo:**
- Si `rho_monotonia` es alto y `spread_d10_d1 > 0`, el score ya separa ganadores de perdedores.
- Compará `sharpe_d10` entre ventanas: dónde el mejor decil tiene mejor ganancia/volatilidad.
- Buscá el cruce de fee donde el decil top deja de ser rentable en la ventana corta (24i).

---

## 2. Detalle por decil (fee 0.30%)

### Ventana 8i (~3d) — fee 0.30%

| decile | n | score_lo | score_hi | mean_net | median_net | pct_profit | sharpe |
| --- | --- | --- | --- | --- | --- | --- | --- |
| 1.0 | 82330.0 | 0.0 | 29.0 | -0.3402 | -0.2958 | 3.4 | -0.828 |
| 2.0 | 82330.0 | 29.0 | 39.0 | -0.3112 | -0.29 | 1.7 | -1.224 |
| 3.0 | 82330.0 | 39.0 | 46.0 | -0.2956 | -0.29 | 2.8 | -1.23 |
| 4.0 | 82330.0 | 46.0 | 54.0 | -0.2743 | -0.285 | 3.5 | -1.239 |
| 5.0 | 82330.0 | 54.0 | 63.0 | -0.2343 | -0.2634 | 7.8 | -0.984 |
| 6.0 | 82329.0 | 63.0 | 69.0 | -0.2546 | -0.29 | 3.7 | -1.585 |
| 7.0 | 82330.0 | 69.0 | 70.0 | -0.2814 | -0.29 | 0.9 | -2.312 |
| 8.0 | 82330.0 | 70.0 | 77.0 | -0.2448 | -0.26 | 2.8 | -1.404 |
| 9.0 | 82330.0 | 77.0 | 87.0 | -0.2699 | -0.26 | 0.2 | -1.955 |
| 10.0 | 82330.0 | 87.0 | 93.0 | -0.2714 | -0.26 | 0.1 | -2.285 |
### Ventana 24i (~8d) — fee 0.30%

| decile | n | score_lo | score_hi | mean_net | median_net | pct_profit | sharpe |
| --- | --- | --- | --- | --- | --- | --- | --- |
| 1.0 | 82330.0 | 0.0 | 29.0 | -0.4666 | -0.3029 | 7.2 | -0.46 |
| 2.0 | 82330.0 | 29.0 | 39.0 | -0.3594 | -0.2806 | 4.6 | -0.559 |
| 3.0 | 82330.0 | 39.0 | 46.0 | -0.3099 | -0.2733 | 7.0 | -0.504 |
| 4.0 | 82330.0 | 46.0 | 54.0 | -0.2501 | -0.2639 | 8.3 | -0.475 |
| 5.0 | 82330.0 | 54.0 | 63.0 | -0.1345 | -0.2146 | 19.0 | -0.218 |
| 6.0 | 82329.0 | 63.0 | 69.0 | -0.1782 | -0.27 | 12.7 | -0.399 |
| 7.0 | 82330.0 | 69.0 | 70.0 | -0.2464 | -0.27 | 3.9 | -0.897 |
| 8.0 | 82330.0 | 70.0 | 77.0 | -0.1432 | -0.18 | 19.6 | -0.255 |
| 9.0 | 82330.0 | 77.0 | 87.0 | -0.2189 | -0.18 | 2.6 | -0.64 |
| 10.0 | 82330.0 | 87.0 | 93.0 | -0.2228 | -0.18 | 1.5 | -0.721 |
### Ventana 72i (~24d) — fee 0.30%

| decile | n | score_lo | score_hi | mean_net | median_net | pct_profit | sharpe |
| --- | --- | --- | --- | --- | --- | --- | --- |
| 1.0 | 82330.0 | 0.0 | 29.0 | -0.7681 | -0.3368 | 13.2 | -0.34 |
| 2.0 | 82330.0 | 29.0 | 39.0 | -0.4952 | -0.2633 | 15.5 | -0.334 |
| 3.0 | 82330.0 | 39.0 | 46.0 | -0.3832 | -0.2419 | 20.0 | -0.253 |
| 4.0 | 82330.0 | 46.0 | 54.0 | -0.2315 | -0.2046 | 24.8 | -0.166 |
| 5.0 | 82330.0 | 54.0 | 63.0 | 0.0726 | -0.0912 | 41.0 | 0.041 |
| 6.0 | 82329.0 | 63.0 | 69.0 | 0.0013 | -0.21 | 31.0 | 0.001 |
| 7.0 | 82330.0 | 69.0 | 70.0 | -0.1619 | -0.21 | 11.8 | -0.186 |
| 8.0 | 82330.0 | 70.0 | 77.0 | 0.1262 | 0.06 | 60.1 | 0.077 |
| 9.0 | 82330.0 | 77.0 | 87.0 | -0.0977 | 0.0276 | 54.9 | -0.09 |
| 10.0 | 82330.0 | 87.0 | 93.0 | -0.1023 | 0.0443 | 57.4 | -0.107 |

CSV por ventana: `reports/profit_sim_20260616_w{8,24,72}.csv`.

---

## 3. Mejor decil (score ≥ p90): corto vs largo a distintos fees

¿El top del score rinde más neto en ~8d o en ~24d, y a partir de qué fee se invierte?

| ventana | fee | mean_net | %rent | sharpe |
| --- | --- | --- | --- | --- |
| 24i (~8d) | 0.05 | 0.0298 | 70.8 | 0.088 |
| 24i (~8d) | 0.10 | -0.0202 | 64.8 | -0.06 |
| 24i (~8d) | 0.20 | -0.1202 | 12.5 | -0.356 |
| 24i (~8d) | 0.30 | -0.2202 | 1.4 | -0.651 |
| 72i (~24d) | 0.05 | 0.1557 | 88.9 | 0.148 |
| 72i (~24d) | 0.10 | 0.1057 | 70.6 | 0.101 |
| 72i (~24d) | 0.20 | 0.0057 | 65.4 | 0.005 |
| 72i (~24d) | 0.30 | -0.0943 | 58.8 | -0.09 |

---

## Para decidir Etapa 2

- Si en la ventana 24i (~8d) el decil top tiene `sharpe` y `%rent` pobres pero en 72i
  mejora mucho → el scoring actual sirve para holds largos, no para 7–15d. El re-objetivo
  Sharpe debería re-pesar hacia drivers que SÍ predicen net en 24i (ver §2/§3 del diagnóstico).
- Si el decil top ya es el más rentable y monótono en 24i → el problema no es el ranking
  sino la SELECCIÓN (umbral de score / fees reales). Ajustar threshold, no pesos.
