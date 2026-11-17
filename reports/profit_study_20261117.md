# Rentabilidad por operación y por usuario — 20261117

Candidatos del modelo vigente (`p_prod` > 0) en 25 tokens solo-Alpha con velas 1m, 2025-10-08 → 2026-10-21. Nocional 1,000 USD por operación, gas 0.05 USD por swap, ρ 0.8, mmr 5 %. Valores en % del nocional; sin entrada = 0. Salida B = dos pagos seguidos ≤ 0 (tope 30 d).

## 1. Por operación (frente a P0)

| política | n | entra % | media | mediana | p5 | entrada | salida | funding | gas | eventos SL % | rebal. | días | Δ media [IC 95 %] | Δp5 | trim. | pasa |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| P0 vigente (θ+0.25/−0.15, 15 min, B, 1x) | 33071 | 78 | +1.65 | +1.65 | -1.16 | +0.14 | -0.17 | +2.16 | 0.03 | 22.4 | 0.00 | +30.0 | — | — | — | base |
| E3 tramos 100 USD cada 1 min | 33082 | 82 | +1.67 | +1.58 | -1.24 | +0.14 | -0.17 | +2.15 | 0.09 | 23.1 | 0.00 | +30.0 | +0.02 [-0.00, +0.05] | -0.08 | 3/5 | no |
| E3 tramos 300 USD cada 1 min | 33076 | 79 | +1.61 | +1.58 | -1.25 | +0.11 | -0.20 | +2.16 | 0.04 | 22.5 | 0.00 | +30.0 | -0.04 [-0.06, -0.02] | -0.09 | 1/5 | no |
| E3 tramos 100 USD cada 5 min | 33082 | 82 | +1.70 | +1.61 | -1.18 | +0.16 | -0.16 | +2.15 | 0.09 | 23.1 | 0.00 | +30.0 | +0.05 [+0.03, +0.08] | -0.02 | 5/5 | sí |
| E3 impacto ≤0.05 % cada 15 min | 33082 | 82 | +1.65 | +1.58 | -1.09 | +0.16 | -0.16 | +2.09 | 0.06 | 23.1 | 0.00 | +30.0 | +0.01 [-0.02, +0.04] | +0.08 | 3/5 | no |
| E3 un solo swap | 33010 | 63 | +1.25 | +1.62 | -0.96 | +0.13 | -0.35 | +2.22 | 0.01 | 18.2 | 0.00 | +30.0 | -0.39 [-0.51, -0.29] | +0.20 | 0/5 | no |
| E4 θe 0.50 / 12 h | 33074 | 67 | +1.45 | +1.70 | -0.96 | +0.20 | -0.16 | +2.15 | 0.03 | 19.8 | 0.00 | +29.9 | -0.20 [-0.27, -0.14] | +0.20 | 0/5 | no |
| E4 θe 0.50 / 24 h | 33070 | 76 | +1.66 | +1.69 | -1.16 | +0.19 | -0.14 | +2.15 | 0.03 | 22.4 | 0.00 | +29.7 | +0.01 [-0.04, +0.06] | +0.00 | 3/5 | no |
| E4 θe 0.75 / 24 h | 33078 | 58 | +1.28 | +1.74 | -0.82 | +0.25 | -0.18 | +2.15 | 0.03 | 17.7 | 0.00 | +29.5 | -0.37 [-0.47, -0.28] | +0.34 | 1/5 | no |
| X1 toma de ganancia basis ≥ +0.30 | 33198 | 78 | -0.03 | -0.15 | -0.83 | +0.14 | -0.38 | +0.22 | 0.03 | 0.6 | 0.00 | +0.8 | -1.68 [-1.93, -1.47] | +0.33 | 0/5 | no |
| X1 toma de ganancia basis ≥ +0.60 | 33198 | 78 | +0.18 | +0.03 | -0.85 | +0.14 | -0.39 | +0.50 | 0.03 | 2.1 | 0.00 | +2.2 | -1.46 [-1.72, -1.24] | +0.31 | 0/5 | no |
| X2 salir con 3 pagos ≤ 0 | 32529 | 79 | +1.71 | +1.72 | -1.18 | +0.14 | -0.20 | +2.25 | 0.03 | 23.4 | 0.00 | +30.0 | +0.04 [+0.02, +0.06] | -0.01 | 4/5 | sí |
| X2 hold mínimo 7 d | 32890 | 78 | +1.69 | +1.71 | -1.20 | +0.14 | -0.18 | +2.22 | 0.03 | 23.0 | 0.00 | +30.0 | +0.03 [+0.02, +0.05] | -0.03 | 4/5 | sí |
| X3 salida θx −0.30 / 48 h | 33106 | 78 | +1.63 | +1.63 | -1.16 | +0.14 | -0.19 | +2.15 | 0.03 | 22.3 | 0.00 | +30.0 | -0.02 [-0.02, -0.01] | +0.00 | 0/5 | no |
| X3 salida θx 0.00 / 48 h | 33097 | 78 | +1.67 | +1.68 | -1.15 | +0.14 | -0.16 | +2.17 | 0.03 | 22.4 | 0.00 | +30.0 | +0.02 [+0.01, +0.03] | +0.01 | 5/5 | sí |
| R1 2x | 33071 | 78 | +0.76 | +0.93 | -1.70 | +0.14 | -0.74 | +1.59 | 0.03 | 39.5 | 0.00 | +20.7 | -0.89 [-1.12, -0.70] | -0.53 | 0/5 | no |
| R1 3x | 33071 | 78 | +0.43 | +0.35 | -2.03 | +0.14 | -0.79 | +1.22 | 0.03 | 50.7 | 0.00 | +11.3 | -1.21 [-1.47, -1.01] | -0.87 | 0/5 | no |
| R2 1x rebalanceo al 40 % del camino | 33071 | 78 | +1.71 | +1.96 | +0.00 | +0.14 | -0.34 | +2.43 | 0.04 | 0.8 | 1.17 | +30.0 | +0.07 [-0.18, +0.29] | +1.16 | 4/5 | no |
| R2 2x rebalanceo al 40 % | 33071 | 78 | +1.76 | +1.84 | +0.00 | +0.14 | -0.14 | +2.30 | 0.05 | 2.1 | 2.67 | +30.0 | +0.11 [-0.15, +0.37] | +1.16 | 4/5 | no |
| R2 3x rebalanceo al 40 % | 33071 | 78 | +1.68 | +1.71 | +0.00 | +0.14 | -0.11 | +2.18 | 0.07 | 5.6 | 4.09 | +30.0 | +0.03 [-0.24, +0.31] | +1.16 | 3/5 | no |
| R2 2x rebalanceo al 60 % | 33071 | 78 | +1.83 | +1.85 | +0.00 | +0.14 | -0.07 | +2.31 | 0.04 | 3.9 | 1.65 | +30.0 | +0.19 [-0.05, +0.41] | +1.16 | 4/5 | no |

## 2. Selección: filtros sobre las operaciones de P0

| filtro | n | % de P0 | media | p5 | eventos SL % | media de las excluidas |
|---|---|---|---|---|---|---|
| S1 gate costo m=0 (pred·7d − costo ≥ 0) | 19242 | 58 | +2.03 | -1.01 | 22.4 | +1.11 |
| S1 gate costo m=0.25 | 8038 | 24 | +2.22 | -0.94 | 24.4 | +1.46 |
| S1 gate costo m=0.50 | 2895 | 9 | +2.75 | -1.16 | 28.0 | +1.54 |
| S2 vol7 ≤ p75 de la muestra | 24803 | 75 | +1.74 | -1.01 | 21.0 | +1.35 |
| S2 recorrido 24 h ≤ 15 % | 21427 | 65 | +1.75 | -0.88 | 19.3 | +1.46 |
| S2 recorrido 24 h ≤ 25 % | 27902 | 84 | +1.71 | -1.03 | 21.1 | +1.27 |

## 3. Cartera por usuario (5 cupos, 10,000 USD; rentabilidad mensual sobre capital total)

Universo = estos tokens solo-Alpha: la utilización es una cota inferior de la de prod (que ve ~100 tokens).

| política | leverage | operaciones | media mensual | mediana | p10 | meses ≥ 2.5 % | utilización | DD % | mensual si utilización 90 % |
|---|---|---|---|---|---|---|---|---|---|
| P0 vigente (θ+0.25/−0.15, 15 min, B, 1x) | 1x | 94 | +1.97 % | +1.62 % | +0.46 % | 15 % | 96 % | 1.4 | +1.86 % |
| E3 tramos 100 USD cada 1 min | 1x | 90 | +1.90 % | +1.55 % | -0.76 % | 31 % | 95 % | 2.4 | +1.80 % |
| E3 tramos 300 USD cada 1 min | 1x | 93 | +1.73 % | +1.32 % | -0.23 % | 15 % | 95 % | 1.9 | +1.64 % |
| E3 tramos 100 USD cada 5 min | 1x | 90 | +1.65 % | +1.37 % | -0.08 % | 15 % | 95 % | 1.5 | +1.56 % |
| E3 impacto ≤0.05 % cada 15 min | 1x | 97 | +2.33 % | +1.71 % | +1.07 % | 31 % | 96 % | 0.5 | +2.20 % |
| E3 un solo swap | 1x | 90 | +1.65 % | +1.70 % | +0.62 % | 15 % | 93 % | 1.0 | +1.60 % |
| E4 θe 0.50 / 12 h | 1x | 88 | +2.18 % | +1.45 % | +0.92 % | 23 % | 92 % | 1.1 | +2.14 % |
| E4 θe 0.50 / 24 h | 1x | 90 | +1.14 % | +1.13 % | +0.19 % | 0 % | 93 % | 1.1 | +1.11 % |
| E4 θe 0.75 / 24 h | 1x | 86 | +1.76 % | +1.42 % | +0.53 % | 15 % | 90 % | 0.3 | +1.76 % |
| X1 toma de ganancia basis ≥ +0.30 | 1x | 1460 | -1.71 % | -1.02 % | -4.43 % | 0 % | 76 % | 22.3 | -2.03 % |
| X1 toma de ganancia basis ≥ +0.60 | 1x | 692 | +0.28 % | +0.57 % | -1.85 % | 8 % | 85 % | 4.5 | +0.30 % |
| X2 salir con 3 pagos ≤ 0 | 1x | 83 | +1.81 % | +1.29 % | +0.84 % | 15 % | 95 % | 1.2 | +1.71 % |
| X2 hold mínimo 7 d | 1x | 85 | +1.69 % | +1.38 % | +0.27 % | 15 % | 96 % | 1.4 | +1.58 % |
| X3 salida θx −0.30 / 48 h | 1x | 93 | +1.86 % | +1.44 % | +0.31 % | 15 % | 96 % | 1.2 | +1.75 % |
| X3 salida θx 0.00 / 48 h | 1x | 93 | +2.02 % | +1.59 % | +0.83 % | 15 % | 95 % | 1.2 | +1.91 % |
| R1 2x | 2x | 118 | +1.67 % | +1.67 % | +0.78 % | 15 % | 95 % | 0.7 | +1.58 % |
| R1 3x | 3x | 154 | +1.63 % | +1.39 % | +0.11 % | 23 % | 95 % | 1.9 | +1.55 % |
| R2 1x rebalanceo al 40 % del camino | 1x | 74 | +1.37 % | +1.48 % | +0.95 % | 0 % | 95 % | 0.0 | +1.30 % |
| R2 2x rebalanceo al 40 % | 2x | 76 | +1.77 % | +1.96 % | +1.12 % | 8 % | 96 % | 0.1 | +1.67 % |
| R2 3x rebalanceo al 40 % | 3x | 78 | +1.89 % | +2.00 % | +0.92 % | 23 % | 95 % | 0.3 | +1.78 % |
| R2 2x rebalanceo al 60 % | 2x | 76 | +2.10 % | +2.00 % | +1.39 % | 23 % | 95 % | 0.2 | +1.99 % |
| P0 + S1 gate costo m=0 (pred·7d − costo ≥ 0) | 1x | 88 | +1.86 % | +1.43 % | +0.83 % | 15 % | 95 % | 1.2 | +1.77 % |
| P0 + S1 gate costo m=0.25 | 1x | 82 | +1.64 % | +1.24 % | -0.04 % | 15 % | 93 % | 3.2 | +1.59 % |
| P0 + S1 gate costo m=0.50 | 1x | 74 | +2.10 % | +1.29 % | +0.66 % | 23 % | 77 % | 1.2 | +2.45 % |
| P0 + S2 vol7 ≤ p75 de la muestra | 1x | 83 | +1.98 % | +1.50 % | +0.78 % | 23 % | 95 % | 1.2 | +1.89 % |
| P0 + S2 recorrido 24 h ≤ 15 % | 1x | 82 | +2.09 % | +1.60 % | +0.73 % | 31 % | 96 % | 1.6 | +1.96 % |
| P0 + S2 recorrido 24 h ≤ 25 % | 1x | 81 | +1.83 % | +1.23 % | +0.33 % | 15 % | 95 % | 1.1 | +1.73 % |

## 4. Conclusiones (ver también `profit_study_20261117_cartera.md`)

- **La palanca principal es no cerrar antes de tiempo.** Mismas entradas, salida forzada a los 3 d: +0.07 % por
  operación y ~1.1 %/mes; con la regla de salida (hold mediano ~30 d): +1.65 % y ~2.0 %/mes. Los trades reales se
  cerraron con mediana de 3.6 d y entraron con basis negativo (antes de la espera de basis del 10-nov).
- **Ejecución**: ya está cerca del óptimo. Tramos de 100 USD cada 5 min: +0.05 pp (pasa) pero depende de ρ, aún sin
  medir en vivo → no se cambia. 100 USD/min no mejora (el empuje se acumula, más gas); un solo swap −0.39 pp;
  exigir más basis al entrar (θ 0.5–0.75) pierde funding.
- **Tomar ganancia por convergencia del basis destruye el resultado** (−1.5 a −1.7 pp): en tokens Alpha el ingreso
  es el funding; la conclusión de He et al. (convergencia) aplica a pares grandes con desvíos frecuentes.
- **Meter el PnL de precio esperado en los costos para filtrar no ayuda**: las operaciones excluidas siguen
  ganando (+1.1 % de media) porque el hold largo amortiza el costo; filtrarlas baja la rentabilidad mensual.
- **Leverage sin rebalanceo empeora** (eventos de SL 40–51 %). **Con rebalanceo spot → margen** los eventos bajan de
  22 % a 1–4 %, el p5 por operación de −1.16 a 0.00 y 2x llega a ~2.3 %/mes (1.9–2.85 % según el mes de inicio) para
  usuarios de 2k–10k USD; 25k: ~1.8 % (impacto). La media por operación no es significativa (IC cruza 0) → no pasa el
  criterio formal; es la vía más prometedora para 2.5 % y requiere implementar y validar con montos chicos.
- **Meta 2.5–5 % mensual**: a 1x el promedio es ~2.0 %/mes (15–30 % de los meses ≥ 2.5 %). 5 % sostenido no aparece
  en ninguna configuración. Universo del estudio: 25 tokens Alpha con velas 1m (prod ve ~100: más selección).
- **Adoptado en prod**: sugerir cierre al tercer settlement sin cobrar (+0.04 pp, 4/5 trimestres), salida paciente
  basis ≥ 0 hasta 48 h (+0.02 pp, 5/5) y aviso de cierre prematuro en el diálogo de cierre.
- **No corrido**: modelo con label neto de ejecución (M1). Con la ejecución paciente el costo de precio promedia
  −0.03 pp por operación; lo que separa ganadoras de perdedoras es el hold y los eventos de SL, no la ejecución.
- Calibración del motor vs 10 trades reales: |error| mediano de basis 0.32 pp en entradas y ~0.2 pp en salidas
  normales, sin sesgo (errores con signo se compensan).
