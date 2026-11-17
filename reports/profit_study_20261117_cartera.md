# Rentabilidad por usuario — combinaciones, leverage y capital — 20261117

5 cupos iguales, un token a la vez, nocional por cupo = capital / 5 · L/(L+1) (costo de impacto a ese tamaño). Rentabilidad mensual sobre el CAPITAL TOTAL. Robustez: misma cartera arrancando 0–5 meses más tarde (6 trayectorias). Universo: 25 tokens solo-Alpha con velas 1m.

## Por operación (nocional del usuario de 10,000 USD)

| política | n | media % nocional | p5 | eventos SL % | rebalanceos | días (mediana) |
|---|---|---|---|---|---|---|
| P0 vigente 1x | 33071 | +1.65 | -1.16 | 22.4 | 0.00 | +30.0 |
| Como se operó: salida a los 3 d | 35053 | +0.07 | -0.70 | 2.6 | 0.00 | +3.0 |
| Salida a los 7 d | 35003 | +0.37 | -0.71 | 6.5 | 0.00 | +7.0 |
| R2 1x rebalanceo 60 % | 33071 | +1.85 | +0.00 | 1.2 | 0.72 | +30.0 |
| R2 2x rebalanceo 60 % | 33070 | +1.79 | +0.00 | 3.9 | 1.66 | +30.0 |
| R2 3x rebalanceo 60 % | 33068 | +1.52 | -0.36 | 9.9 | 2.52 | +30.0 |
| Combo 2x: R2 60 % + 3 pagos + θx 0/48 h + 100 USD/5 min | 32557 | +1.95 | +0.00 | 4.4 | 1.70 | +30.1 |
| Combo 3x: R2 60 % + 3 pagos + θx 0/48 h + 100 USD/5 min | 32557 | +1.68 | -0.38 | 10.9 | 2.57 | +30.1 |

## Mensual sobre capital total por tamaño de usuario

| política | capital USD | media mensual | rango de las 6 trayectorias | mediana mes | p10 mes | meses ≥ 2.5 % | DD máx % | utilización |
|---|---|---|---|---|---|---|---|---|
| P0 vigente 1x | 2,000 | +2.01 % | +1.75 a +2.36 % | +1.35 % | +0.09 % | 21 % | 2.3 | 95 % |
| P0 vigente 1x | 5,000 | +2.00 % | +1.49 a +2.30 % | +1.42 % | +0.09 % | 25 % | 2.3 | 95 % |
| P0 vigente 1x | 10,000 | +2.03 % | +1.74 a +2.49 % | +1.55 % | +0.16 % | 16 % | 1.7 | 95 % |
| P0 vigente 1x | 25,000 | +2.06 % | +1.59 a +2.49 % | +1.36 % | +0.65 % | 17 % | 1.3 | 94 % |
| Como se operó: salida a los 3 d | 2,000 | +1.09 % | +1.06 a +1.13 % | +1.02 % | +0.15 % | 2 % | 1.2 | 85 % |
| Como se operó: salida a los 3 d | 5,000 | +1.12 % | +0.81 a +1.79 % | +0.94 % | +0.30 % | 6 % | 2.3 | 85 % |
| Como se operó: salida a los 3 d | 10,000 | +1.30 % | +0.97 a +1.63 % | +0.76 % | +0.26 % | 14 % | 0.7 | 85 % |
| Como se operó: salida a los 3 d | 25,000 | +0.54 % | +0.36 a +0.71 % | +0.42 % | -0.38 % | 10 % | 2.4 | 84 % |
| Salida a los 7 d | 2,000 | +2.10 % | +1.88 a +2.31 % | +1.67 % | +0.81 % | 22 % | 2.5 | 91 % |
| Salida a los 7 d | 5,000 | +1.75 % | +1.61 a +2.01 % | +1.69 % | +0.39 % | 19 % | 3.1 | 91 % |
| Salida a los 7 d | 10,000 | +1.80 % | +1.45 a +2.31 % | +1.34 % | +0.52 % | 19 % | 2.7 | 90 % |
| Salida a los 7 d | 25,000 | +1.18 % | +0.78 a +1.50 % | +1.10 % | -0.08 % | 17 % | 1.9 | 90 % |
| R2 1x rebalanceo 60 % | 2,000 | +1.82 % | +1.54 a +2.49 % | +1.70 % | +0.75 % | 19 % | 0.1 | 95 % |
| R2 1x rebalanceo 60 % | 5,000 | +1.75 % | +1.54 a +2.37 % | +1.59 % | +0.57 % | 16 % | 0.1 | 95 % |
| R2 1x rebalanceo 60 % | 10,000 | +1.70 % | +1.47 a +2.28 % | +1.66 % | +0.77 % | 10 % | 0.2 | 95 % |
| R2 1x rebalanceo 60 % | 25,000 | +1.57 % | +1.33 a +2.19 % | +1.46 % | +0.73 % | 8 % | 0.2 | 94 % |
| R2 2x rebalanceo 60 % | 2,000 | +2.36 % | +1.88 a +2.85 % | +2.02 % | +1.13 % | 32 % | 0.2 | 95 % |
| R2 2x rebalanceo 60 % | 5,000 | +2.34 % | +1.88 a +2.79 % | +1.97 % | +1.11 % | 29 % | 0.2 | 95 % |
| R2 2x rebalanceo 60 % | 10,000 | +2.31 % | +2.11 a +2.69 % | +1.97 % | +0.68 % | 30 % | 1.2 | 95 % |
| R2 2x rebalanceo 60 % | 25,000 | +1.83 % | +1.60 a +2.47 % | +1.73 % | +0.89 % | 13 % | 0.5 | 94 % |
| R2 3x rebalanceo 60 % | 2,000 | +2.00 % | +1.75 a +2.17 % | +2.12 % | +0.18 % | 29 % | 2.0 | 95 % |
| R2 3x rebalanceo 60 % | 5,000 | +2.15 % | +2.06 a +2.30 % | +2.15 % | +0.76 % | 30 % | 0.4 | 95 % |
| R2 3x rebalanceo 60 % | 10,000 | +1.85 % | +1.69 a +2.02 % | +1.84 % | +0.21 % | 14 % | 2.0 | 94 % |
| R2 3x rebalanceo 60 % | 25,000 | +1.86 % | +1.73 a +2.08 % | +1.71 % | +0.13 % | 22 % | 2.0 | 93 % |
| Combo 2x: R2 60 % + 3 pagos + θx 0/48 h + 100 USD/5 min | 2,000 | +2.14 % | +1.78 a +2.47 % | +1.87 % | +1.07 % | 22 % | 0.3 | 95 % |
| Combo 2x: R2 60 % + 3 pagos + θx 0/48 h + 100 USD/5 min | 5,000 | +2.07 % | +1.75 a +2.44 % | +1.83 % | +1.16 % | 19 % | 0.3 | 95 % |
| Combo 2x: R2 60 % + 3 pagos + θx 0/48 h + 100 USD/5 min | 10,000 | +2.13 % | +1.66 a +2.40 % | +1.72 % | +1.02 % | 19 % | 0.2 | 95 % |
| Combo 2x: R2 60 % + 3 pagos + θx 0/48 h + 100 USD/5 min | 25,000 | +1.87 % | +1.55 a +2.23 % | +1.58 % | +0.77 % | 16 % | 1.4 | 95 % |
| Combo 3x: R2 60 % + 3 pagos + θx 0/48 h + 100 USD/5 min | 2,000 | +2.06 % | +1.85 a +2.59 % | +1.95 % | +0.18 % | 25 % | 3.1 | 95 % |
| Combo 3x: R2 60 % + 3 pagos + θx 0/48 h + 100 USD/5 min | 5,000 | +1.99 % | +1.66 a +2.54 % | +1.83 % | +0.31 % | 24 % | 1.9 | 96 % |
| Combo 3x: R2 60 % + 3 pagos + θx 0/48 h + 100 USD/5 min | 10,000 | +1.68 % | +1.55 a +1.75 % | +1.84 % | +0.03 % | 11 % | 2.1 | 95 % |
| Combo 3x: R2 60 % + 3 pagos + θx 0/48 h + 100 USD/5 min | 25,000 | +1.47 % | +1.12 a +1.77 % | +1.55 % | -0.12 % | 10 % | 2.1 | 95 % |
