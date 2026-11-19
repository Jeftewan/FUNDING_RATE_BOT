# Reglas deterministas del piloto LLM — 20261118

Candidatos del modelo (`p_prod` > 0) en 25 tokens solo-Alpha con velas 1m, 2025-10-08 → 2026-10-17. Base = política de producción (salida al 3er pago ≤ 0, salida paciente θx 0.00 / 48 h, θe 0.25 / 6 h, tramos cada 15 min, 1x). Reglas en `analysis/signals.py` (piso del funding 0.03 %/día + 0.005).

**Criterio (fijado antes de correr):** X4 (salida) pasa si (a) `gate()` por operación a 1,000 y 2,100 USD (IC 95 % símbolo×semana > 0, Δp5 ≥ −0.25 pp, ≥ 80 % trimestres) o (b) cartera de 5 cupos a 5k y 10k USD (6 trayectorias): %/mes medio > base, p10 mensual ≥ base − 0.05 pp, utilización ≥ base − 1 pp, y (b) se sostiene sin operaciones > +20 %. E5 (filtro de entrada) se evalúa con (b).

## 1. Salida: por operación

| política | USD | n | media | mediana | p5 | salida | días | Δ media [IC 95 %] | Δp5 | trim. | pasa (a) |
|---|---|---|---|---|---|---|---|---|---|---|---|
| P0 prod (3 pagos ≤ 0, θx 0.00/48 h) | 1,000 | 32555 | +1.73 | +1.75 | -1.18 | -0.19 | +30.0 | — | — | — | base |
| P0 prod (3 pagos ≤ 0, θx 0.00/48 h) | 2,100 | 32550 | +1.58 | +1.65 | -1.26 | -0.26 | +30.0 | — | — | — | base |
| X4 basis venta ≥ +0.2 y funding en el piso (1 pago) | 1,000 | 32656 | +0.15 | -0.01 | -0.80 | -0.43 | +1.6 | -1.59 [-1.85, -1.35] | +0.37 | 0/5 | no |
| X4 basis venta ≥ +0.2 y funding en el piso (1 pago) | 2,100 | 32656 | +0.09 | -0.10 | -0.87 | -0.49 | +1.9 | -1.49 [-1.73, -1.26] | +0.38 | 0/5 | no |
| X4 basis venta ≥ +0.2 y funding en el piso (3 pagos) | 1,000 | 32656 | +0.45 | +0.20 | -0.79 | -0.38 | +3.5 | -1.29 [-1.55, -1.06] | +0.39 | 0/5 | no |
| X4 basis venta ≥ +0.2 y funding en el piso (3 pagos) | 2,100 | 32655 | +0.41 | +0.14 | -0.86 | -0.42 | +3.9 | -1.17 [-1.42, -0.95] | +0.39 | 0/5 | no |
| X4 basis venta ≥ +0.3 y funding en el piso (1 pago) | 1,000 | 32656 | +0.23 | +0.06 | -0.79 | -0.42 | +2.2 | -1.50 [-1.77, -1.27] | +0.38 | 0/5 | no |
| X4 basis venta ≥ +0.3 y funding en el piso (1 pago) | 2,100 | 32655 | +0.17 | -0.03 | -0.85 | -0.48 | +2.5 | -1.41 [-1.65, -1.19] | +0.40 | 0/5 | no |
| X4 basis venta ≥ +0.3 y funding en el piso (3 pagos) | 1,000 | 32655 | +0.71 | +0.30 | -0.78 | -0.15 | +4.3 | -1.02 [-1.20, -0.86] | +0.40 | 0/5 | no |
| X4 basis venta ≥ +0.3 y funding en el piso (3 pagos) | 2,100 | 32655 | +0.61 | +0.21 | -0.89 | -0.23 | +4.6 | -0.97 [-1.14, -0.81] | +0.36 | 0/5 | no |
| X4 basis venta ≥ +0.5 y funding en el piso (1 pago) | 1,000 | 32653 | +0.60 | +0.26 | -0.77 | -0.20 | +3.8 | -1.14 [-1.33, -0.96] | +0.41 | 0/5 | no |
| X4 basis venta ≥ +0.5 y funding en el piso (1 pago) | 2,100 | 32653 | +0.53 | +0.18 | -0.87 | -0.28 | +4.2 | -1.05 [-1.24, -0.89] | +0.39 | 0/5 | no |
| X4 basis venta ≥ +0.5 y funding en el piso (3 pagos) | 1,000 | 32653 | +0.91 | +0.52 | -0.79 | -0.16 | +6.6 | -0.83 [-1.01, -0.67] | +0.39 | 0/5 | no |
| X4 basis venta ≥ +0.5 y funding en el piso (3 pagos) | 2,100 | 32650 | +0.82 | +0.47 | -0.92 | -0.24 | +7.1 | -0.77 [-0.94, -0.62] | +0.34 | 0/5 | no |

## 2. Salida: cartera (5 cupos, 1x, media de 6 trayectorias)

| política | capital | sin > +20 % | ops | %/mes | p10 mes | utilización | DD % | vs base |
|---|---|---|---|---|---|---|---|---|
| P0 prod (3 pagos ≤ 0, θx 0.00/48 h) | 5,000 | no | 69 | +2.13 % | +0.30 % | 95 % | 1.9 | base |
| P0 prod (3 pagos ≤ 0, θx 0.00/48 h) | 5,000 | sí | 67 | +1.08 % | +0.14 % | 95 % | 1.9 | base |
| P0 prod (3 pagos ≤ 0, θx 0.00/48 h) | 10,000 | no | 68 | +2.21 % | +0.19 % | 94 % | 1.5 | base |
| P0 prod (3 pagos ≤ 0, θx 0.00/48 h) | 10,000 | sí | 68 | +1.16 % | +0.18 % | 95 % | 1.5 | base |
| X4 basis venta ≥ +0.2 y funding en el piso (1 pago) | 5,000 | no | 493 | +1.30 % | +0.28 % | 83 % | 0.8 | no |
| X4 basis venta ≥ +0.2 y funding en el piso (1 pago) | 5,000 | sí | 493 | +1.30 % | +0.28 % | 83 % | 0.8 | no |
| X4 basis venta ≥ +0.2 y funding en el piso (1 pago) | 10,000 | no | 472 | +0.73 % | -0.13 % | 83 % | 1.5 | no |
| X4 basis venta ≥ +0.2 y funding en el piso (1 pago) | 10,000 | sí | 472 | +0.73 % | -0.13 % | 83 % | 1.5 | no |
| X4 basis venta ≥ +0.2 y funding en el piso (3 pagos) | 5,000 | no | 257 | +1.89 % | +0.23 % | 90 % | 1.8 | no |
| X4 basis venta ≥ +0.2 y funding en el piso (3 pagos) | 5,000 | sí | 256 | +1.49 % | +0.23 % | 90 % | 1.8 | no |
| X4 basis venta ≥ +0.2 y funding en el piso (3 pagos) | 10,000 | no | 254 | +1.66 % | +0.37 % | 90 % | 1.9 | no |
| X4 basis venta ≥ +0.2 y funding en el piso (3 pagos) | 10,000 | sí | 253 | +1.28 % | +0.37 % | 90 % | 1.9 | no |
| X4 basis venta ≥ +0.3 y funding en el piso (1 pago) | 5,000 | no | 398 | +1.24 % | +0.34 % | 86 % | 0.7 | no |
| X4 basis venta ≥ +0.3 y funding en el piso (1 pago) | 5,000 | sí | 398 | +1.24 % | +0.34 % | 86 % | 0.7 | no |
| X4 basis venta ≥ +0.3 y funding en el piso (1 pago) | 10,000 | no | 384 | +1.04 % | +0.25 % | 85 % | 1.6 | no |
| X4 basis venta ≥ +0.3 y funding en el piso (1 pago) | 10,000 | sí | 384 | +1.04 % | +0.25 % | 85 % | 1.6 | no |
| X4 basis venta ≥ +0.3 y funding en el piso (3 pagos) | 5,000 | no | 229 | +2.13 % | +0.63 % | 92 % | 1.9 | no |
| X4 basis venta ≥ +0.3 y funding en el piso (3 pagos) | 5,000 | sí | 228 | +1.35 % | +0.29 % | 92 % | 1.9 | no |
| X4 basis venta ≥ +0.3 y funding en el piso (3 pagos) | 10,000 | no | 217 | +2.02 % | +0.45 % | 91 % | 2.1 | no |
| X4 basis venta ≥ +0.3 y funding en el piso (3 pagos) | 10,000 | sí | 213 | +1.13 % | +0.29 % | 91 % | 2.1 | no |
| X4 basis venta ≥ +0.5 y funding en el piso (1 pago) | 5,000 | no | 255 | +2.18 % | +0.60 % | 89 % | 0.7 | no |
| X4 basis venta ≥ +0.5 y funding en el piso (1 pago) | 5,000 | sí | 256 | +1.52 % | +0.60 % | 89 % | 0.7 | no |
| X4 basis venta ≥ +0.5 y funding en el piso (1 pago) | 10,000 | no | 249 | +2.11 % | +0.91 % | 89 % | 1.4 | no |
| X4 basis venta ≥ +0.5 y funding en el piso (1 pago) | 10,000 | sí | 254 | +1.42 % | +0.57 % | 89 % | 1.4 | no |
| X4 basis venta ≥ +0.5 y funding en el piso (3 pagos) | 5,000 | no | 164 | +2.46 % | +0.81 % | 92 % | 1.9 | no |
| X4 basis venta ≥ +0.5 y funding en el piso (3 pagos) | 5,000 | sí | 165 | +1.42 % | +0.81 % | 92 % | 1.9 | no |
| X4 basis venta ≥ +0.5 y funding en el piso (3 pagos) | 10,000 | no | 160 | +2.11 % | +0.83 % | 92 % | 1.9 | no |
| X4 basis venta ≥ +0.5 y funding en el piso (3 pagos) | 10,000 | sí | 161 | +1.26 % | +0.83 % | 92 % | 1.9 | no |

## 3. Entrada: filtro por fracción de las 24 h con basis ≥ θ (sobre P0 prod)

Paridad muestreo horario (prod) vs velas 1m: ρ Spearman 0.87 (n 30066).

| filtro | medición | % candidatos | entra % (kept) | entra % (excl.) | media kept | media excl. |
|---|---|---|---|---|---|---|
| ≥ 30% | horaria | 36 | 96 | 69 | +2.45 | +1.32 |
| ≥ 50% | horaria | 25 | 97 | 73 | +2.45 | +1.49 |
| ≥ 70% | horaria | 13 | 97 | 76 | +2.82 | +1.57 |
| ≥ 30% | 1m | 40 | 97 | 68 | +2.39 | +1.30 |
| ≥ 50% | 1m | 22 | 97 | 74 | +2.54 | +1.50 |
| ≥ 70% | 1m | 10 | 99 | 77 | +3.11 | +1.58 |

| filtro (horaria) | capital | sin > +20 % | ops | %/mes | p10 mes | utilización | DD % | vs base |
|---|---|---|---|---|---|---|---|---|
| E5 fill ≥ 30% | 5,000 | no | 62 | +2.09 % | +0.42 % | 99 % | 0.9 | no |
| E5 fill ≥ 30% | 5,000 | sí | 61 | +1.29 % | +0.41 % | 99 % | 1.0 | ok |
| E5 fill ≥ 30% | 10,000 | no | 64 | +2.18 % | +0.03 % | 99 % | 1.2 | no |
| E5 fill ≥ 30% | 10,000 | sí | 66 | +1.25 % | +0.02 % | 99 % | 1.2 | no |
| E5 fill ≥ 50% | 5,000 | no | 60 | +1.96 % | +0.07 % | 99 % | 1.4 | no |
| E5 fill ≥ 50% | 5,000 | sí | 60 | +0.95 % | +0.07 % | 99 % | 1.4 | no |
| E5 fill ≥ 50% | 10,000 | no | 60 | +1.95 % | +0.17 % | 99 % | 2.5 | no |
| E5 fill ≥ 50% | 10,000 | sí | 60 | +1.10 % | +0.24 % | 99 % | 2.5 | no |
| E5 fill ≥ 70% | 5,000 | no | 61 | +1.33 % | +0.00 % | 96 % | 2.8 | no |
| E5 fill ≥ 70% | 5,000 | sí | 61 | +1.05 % | +0.00 % | 96 % | 2.8 | no |
| E5 fill ≥ 70% | 10,000 | no | 59 | +1.42 % | +0.00 % | 96 % | 1.1 | no |
| E5 fill ≥ 70% | 10,000 | sí | 59 | +1.16 % | +0.00 % | 96 % | 1.1 | no |

## 5. Veredicto

- X4 basis venta ≥ +0.2 y funding en el piso (1 pago): **no pasa**
- X4 basis venta ≥ +0.2 y funding en el piso (3 pagos): **no pasa**
- X4 basis venta ≥ +0.3 y funding en el piso (1 pago): **no pasa**
- X4 basis venta ≥ +0.3 y funding en el piso (3 pagos): **no pasa**
- X4 basis venta ≥ +0.5 y funding en el piso (1 pago): **no pasa**
- X4 basis venta ≥ +0.5 y funding en el piso (3 pagos): **no pasa**
- E5 fill ≥ 30% (horaria): **no pasa**
- E5 fill ≥ 50% (horaria): **no pasa**
- E5 fill ≥ 70% (horaria): **no pasa**

## 6. Lectura

- **Salida (X4): no.** Salir cuando el basis de venta paga y el funding cae al piso corta posiciones que, en la
  base, siguen cobrando hasta el tope de 30 d (mediana 30 d → 1.6–7 d). Por operación pierde 0.8–1.6 pp en 0/5
  trimestres; la rotación extra (160–490 operaciones contra 68) no lo compensa en cartera. La única que supera la base
  (≥ +0.5 con 3 pagos, +2.46 % vs +2.13 % a 5k) lo hace por las mechas: sin operaciones > +20 % queda en +1.42 % vs
  +1.08 %, pero a 10k cae a +1.26 % vs +1.16 % con p10 mejor; falla por utilización (92 % vs 95 %) y no es robusta.
- **Entrada (E5): no como filtro.** La fracción de las 24 h con basis ≥ θ sí separa candidatos (entran 96–97 % vs
  69–76 %; media +2.45 vs +1.32–1.57 %), y la medición horaria de prod tiene paridad con la de 1m (ρ 0.87). Pero
  filtrar achica el universo y la cartera no mejora (utilización de la base ya en 95 %). Usarla para **ordenar**
  candidatos en vez de filtrar es otra hipótesis: requiere su propio criterio antes de correr.
- **Piloto LLM:** con la base de producción (3 pagos, θx 0.00/48 h) las reglas que imitan al LLM no capturan ventaja;
  su +0.1–0.4 pp/mes sin la mecha de TA es consistente con ruido. No se implementan señales EXIT_NOW / ENTER_NOW.
