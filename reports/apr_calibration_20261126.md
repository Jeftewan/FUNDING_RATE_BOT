# Recalibración del APR esperado — 20261126

Script: `scripts/apr_calibration_study.py` (criterio pre-registrado en el docstring). Filas: candidatos `p_prod > 0` de `consensus_study` (244,012); OOS 142,832 (2025-10-26 → 2026-10-26), walk-forward mensual con purga 31 d.

Cumple = APR neto realizado ≥ APR mostrado. Pinball = |real − mostrado| / 2 (pp de APR).

| variante | pinball | Δ vs M0 [IC 95 %] | cumple | trimestres mejor | 1 | 2 | 3 |
| --- | --- | --- | --- | --- | --- | --- | --- |
| M0 | 9.57 | — | 44.1% | 0% | — | — | — |
| M1 | 9.30 | -0.27 [-0.38, -0.16] | 42.2% | 80% | ✔ | ✘ | ✘ |
| M2 | 9.25 | -0.31 [-0.42, -0.20] | 48.5% | 100% | ✔ | ✔ | ✘ |
| M3 | 9.24 | -0.33 [-0.44, -0.22] | 49.9% | 100% | ✔ | ✔ | ✘ |

**Resultado: ninguna pasa → no se exporta.**

### M0 por decil del valor mostrado

| decil | n | mostrado (mediana) | cumple | real mediano |
| --- | --- | --- | --- | --- |
| 1 | 14,284 | 1.2 | 61% | 4.5 |
| 2 | 14,283 | 4.8 | 60% | 7.4 |
| 3 | 14,283 | 7.7 | 54% | 8.4 |
| 4 | 14,283 | 10.1 | 51% | 10.4 |
| 5 | 14,283 | 13.4 | 45% | 11.6 |
| 6 | 14,283 | 16.4 | 41% | 13.3 |
| 7 | 14,283 | 19.5 | 37% | 15.1 |
| 8 | 14,283 | 23.2 | 33% | 17.5 |
| 9 | 14,283 | 28.2 | 31% | 20.1 |
| 10 | 14,284 | 45.7 | 28% | 31.1 |

Trimestres (cumple): 2025Q4 47%, 2026Q1 38%, 2026Q2 42%, 2026Q3 49%, 2026Q4 41%

### M1 por decil del valor mostrado

| decil | n | mostrado (mediana) | cumple | real mediano |
| --- | --- | --- | --- | --- |
| 1 | 14,284 | 7.4 | 40% | 4.9 |
| 2 | 14,283 | 9.5 | 44% | 8.2 |
| 3 | 14,283 | 10.7 | 38% | 7.8 |
| 4 | 14,283 | 13.3 | 42% | 11.0 |
| 5 | 14,283 | 14.5 | 44% | 12.0 |
| 6 | 14,283 | 15.6 | 42% | 12.5 |
| 7 | 14,283 | 17.2 | 44% | 15.1 |
| 8 | 14,283 | 19.5 | 43% | 17.4 |
| 9 | 14,283 | 24.2 | 39% | 19.8 |
| 10 | 14,284 | 34.8 | 46% | 31.0 |

Trimestres (cumple): 2025Q4 43%, 2026Q1 34%, 2026Q2 41%, 2026Q3 49%, 2026Q4 34%

### M2 por decil del valor mostrado

| decil | n | mostrado (mediana) | cumple | real mediano |
| --- | --- | --- | --- | --- |
| 1 | 14,284 | 4.7 | 51% | 5.1 |
| 2 | 14,283 | 7.7 | 51% | 8.0 |
| 3 | 14,283 | 8.5 | 48% | 8.1 |
| 4 | 14,283 | 9.9 | 53% | 10.8 |
| 5 | 14,283 | 11.2 | 49% | 10.9 |
| 6 | 14,283 | 13.7 | 47% | 12.8 |
| 7 | 14,283 | 15.4 | 48% | 14.9 |
| 8 | 14,283 | 17.2 | 46% | 16.0 |
| 9 | 14,283 | 22.1 | 45% | 20.0 |
| 10 | 14,284 | 31.7 | 46% | 30.1 |

Trimestres (cumple): 2025Q4 52%, 2026Q1 41%, 2026Q2 48%, 2026Q3 54%, 2026Q4 39%

### M3 por decil del valor mostrado

| decil | n | mostrado (mediana) | cumple | real mediano |
| --- | --- | --- | --- | --- |
| 1 | 14,284 | 5.1 | 54% | 5.7 |
| 2 | 14,283 | 7.2 | 57% | 8.0 |
| 3 | 14,283 | 8.4 | 51% | 8.8 |
| 4 | 14,283 | 9.6 | 51% | 10.1 |
| 5 | 14,283 | 11.1 | 47% | 10.4 |
| 6 | 14,283 | 13.2 | 52% | 13.8 |
| 7 | 14,283 | 15.4 | 49% | 15.1 |
| 8 | 14,283 | 17.7 | 47% | 16.8 |
| 9 | 14,283 | 22.0 | 44% | 19.5 |
| 10 | 14,284 | 31.9 | 47% | 31.1 |

Trimestres (cumple): 2025Q4 52%, 2026Q1 39%, 2026Q2 49%, 2026Q3 59%, 2026Q4 36%

### Confirmación con el motor de ejecución

| variante | n | cumple | cumple M0 | pinball | pinball M0 |
| --- | --- | --- | --- | --- | --- |
| M1 | 24,986 | 61.8% | 58.7% | 18.03 | 18.40 |
| M2 | 24,986 | 67.4% | 58.7% | 18.45 | 18.40 |
| M3 | 24,986 | 66.7% | 58.7% | 18.43 | 18.40 |

Mismas filas con el label de costo fijo (Alpha 1.11 %): M0 cumple 51.5%, M1 cumple 56.2%, M2 cumple 62.7%, M3 cumple 62.3%; APR real mediano: costo fijo 20.1 vs motor 23.1.
