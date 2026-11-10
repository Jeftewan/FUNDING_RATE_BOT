# Límites pareados on-chain + perp — 2026-11-10

Nocional 2,100 USD, 1x, intentos cada 6 h con funding ≥ 0.05 %/día, hold 7 d, 21 tokens Alpha (2025-10 → 2026-09). Mismo motor que `tranche_study`: velas planas fuera, comisión del pool + impacto por token, funding liquidado. Resultado por intento = ejecución ida y vuelta + funding (% del nocional); entrada que no llena = 0.

Criterio (usuario): media ≥ `prod actual`, p5 ≥ p5 de prod − 1 pp y ≥ 90 % de intentos con entrada.

## Barrido inicial (orden única por pierna)

| Política | n | Entra | Media | Mediana | p5 | > 0 | Ejecución | Funding | min sola p95 | adverso p95 | pasa |
|---|---|---|---|---|---|---|---|---|---|---|---|
| prod actual | 13886 | 76 % | +0.18 | +0.00 | -0.91 | 46 % | -0.49 | +0.67 | 0 | 0.00 % |  |
| piloto: límite on-chain anclada al perp + perp taker | 13779 | 81 % | -0.01 | +0.00 | -2.57 | 42 % | -0.74 | +0.74 | 2 | 0.00 % |  |
| ejecutar ahora | 13955 | 88 % | -0.77 | +0.00 | -1.74 | 45 % | -1.57 | +0.81 | 0 | 0.00 % |  |
| mid θ0.6 R15 W24h now | 14003 | 94 % | -1.49 | +0.00 | -2.80 | 47 % | -2.33 | +0.84 | 2 | 0.64 % |  |
| mid θ0.6 R15 W6h now | 13986 | 93 % | -1.50 | +0.00 | -2.79 | 46 % | -2.33 | +0.83 | 2 | 0.64 % |  |
| mid θ0.6 R60 W24h now | 14002 | 93 % | -1.51 | +0.00 | -2.81 | 45 % | -2.35 | +0.84 | 2 | 0.65 % |  |
| mid θ0.6 R60 W6h now | 13973 | 92 % | -1.51 | +0.00 | -2.78 | 45 % | -2.34 | +0.83 | 2 | 0.65 % |  |
| mid θ0.4 R15 W24h now | 14003 | 94 % | -1.54 | +0.00 | -2.86 | 45 % | -2.38 | +0.84 | 2 | 0.63 % |  |
| mid θ0.4 R60 W24h now | 14002 | 94 % | -1.54 | +0.00 | -2.85 | 44 % | -2.38 | +0.84 | 2 | 0.64 % |  |
| mid θ0.4 R15 W6h now | 13987 | 93 % | -1.54 | +0.00 | -2.85 | 44 % | -2.37 | +0.83 | 2 | 0.63 % |  |
| mid θ0.4 R60 W6h now | 13980 | 93 % | -1.55 | +0.00 | -2.84 | 43 % | -2.38 | +0.83 | 2 | 0.64 % |  |
| mid θ0.2 R15 W24h now | 14003 | 94 % | -1.58 | -0.03 | -2.92 | 43 % | -2.42 | +0.85 | 2 | 0.62 % |  |
| mid θ0.2 R60 W24h now | 14003 | 94 % | -1.58 | -0.04 | -2.87 | 43 % | -2.42 | +0.84 | 2 | 0.63 % |  |
| mid θ0.2 R15 W6h now | 13992 | 93 % | -1.58 | -0.02 | -2.89 | 43 % | -2.41 | +0.83 | 2 | 0.62 % |  |
| mid θ0.2 R60 W6h now | 13991 | 93 % | -1.58 | -0.02 | -2.86 | 42 % | -2.41 | +0.83 | 2 | 0.62 % |  |
| perp θ0.6 R60 W24h now | 14003 | 94 % | -1.60 | +0.00 | -3.11 | 44 % | -2.45 | +0.85 | 2 | 0.66 % |  |
| perp θ0.6 R60 W6h now | 13980 | 93 % | -1.60 | +0.00 | -3.12 | 43 % | -2.44 | +0.84 | 2 | 0.66 % |  |
| mid θ0.0 R15 W24h now | 14003 | 94 % | -1.61 | -0.07 | -2.98 | 42 % | -2.46 | +0.85 | 2 | 0.59 % |  |
| mid θ0.0 R60 W24h now | 14003 | 94 % | -1.62 | -0.07 | -2.96 | 41 % | -2.46 | +0.84 | 2 | 0.59 % |  |
| mid θ0.0 R15 W6h now | 13993 | 93 % | -1.62 | -0.06 | -2.97 | 41 % | -2.45 | +0.83 | 2 | 0.59 % |  |
| mid θ0.0 R60 W6h now | 13992 | 93 % | -1.62 | -0.06 | -2.94 | 41 % | -2.45 | +0.83 | 2 | 0.59 % |  |
| perp θ0.6 R15 W6h now | 13989 | 94 % | -1.63 | -0.00 | -3.24 | 43 % | -2.48 | +0.85 | 2 | 0.65 % |  |
| perp θ0.6 R15 W24h now | 14003 | 95 % | -1.64 | -0.02 | -3.26 | 44 % | -2.50 | +0.86 | 2 | 0.65 % |  |
| perp θ0.4 R60 W24h now | 14003 | 94 % | -1.65 | -0.06 | -3.22 | 42 % | -2.51 | +0.86 | 2 | 0.63 % |  |
| perp θ0.4 R60 W6h now | 13987 | 93 % | -1.66 | -0.05 | -3.21 | 42 % | -2.50 | +0.85 | 2 | 0.62 % |  |

## Refinamiento del control, gate por swap y sensibilidad del edge

| Política | n | Entra | Media | Mediana | p5 | > 0 | Ejecución | Funding | min sola p95 | adverso p95 | pasa |
|---|---|---|---|---|---|---|---|---|---|---|---|
| prod actual | 13886 | 76 % | +0.18 | +0.00 | -0.91 | 46 % | -0.49 | +0.67 | 0 | 0.00 % |  |
| ejecutar ahora | 13955 | 88 % | -0.77 | +0.00 | -1.74 | 45 % | -1.57 | +0.81 | 0 | 0.00 % |  |
| mid θ0.6 R15 W24h now +gate swap | 14003 | 94 % | -1.47 | +0.00 | -2.79 | 48 % | -2.32 | +0.84 | 2 | 0.60 % |  |
| mid θ0.6 R15 W6h now +gate swap | 13986 | 93 % | -1.49 | +0.00 | -2.78 | 47 % | -2.31 | +0.83 | 2 | 0.59 % |  |
| mid θ0.6 R60 W24h now +gate swap | 14002 | 94 % | -1.49 | +0.00 | -2.79 | 46 % | -2.33 | +0.84 | 2 | 0.61 % |  |
| mid θ0.6 R15 W24h now edge 0.3 | 14003 | 94 % | -1.50 | +0.00 | -2.81 | 46 % | -2.34 | +0.84 | 2 | 0.64 % |  |
| mid θ0.6 R60 W6h now +gate swap | 13974 | 92 % | -1.50 | +0.00 | -2.76 | 46 % | -2.33 | +0.83 | 2 | 0.61 % |  |
| mid θ0.6 R15 W24h now edge 0.7 | 14003 | 94 % | -1.51 | +0.00 | -2.86 | 47 % | -2.35 | +0.84 | 2 | 0.65 % |  |
| mid θ0.6 R15 W6h now edge 0.3 | 13986 | 93 % | -1.51 | +0.00 | -2.80 | 45 % | -2.34 | +0.83 | 2 | 0.64 % |  |
| mid θ0.6 R15 W6h now edge 0.7 | 13986 | 93 % | -1.52 | +0.00 | -2.85 | 46 % | -2.34 | +0.83 | 2 | 0.65 % |  |
| mid θ0.6 R60 W24h now edge 0.3 | 14002 | 94 % | -1.53 | +0.00 | -2.82 | 45 % | -2.37 | +0.84 | 2 | 0.66 % |  |
| mid θ0.6 R60 W6h now edge 0.3 | 13973 | 92 % | -1.53 | +0.00 | -2.82 | 44 % | -2.36 | +0.83 | 2 | 0.65 % |  |
| mid θ0.6 R60 W24h now edge 0.7 | 14002 | 93 % | -1.56 | +0.00 | -2.91 | 44 % | -2.41 | +0.84 | 2 | 0.66 % |  |
| mid θ0.6 R60 W6h now edge 0.7 | 13973 | 92 % | -1.57 | +0.00 | -2.90 | 43 % | -2.40 | +0.82 | 2 | 0.66 % |  |
| mid θ0.6 R15 W24h keep T5 x0.5 | 14003 | 94 % | -1.61 | -0.04 | -2.98 | 42 % | -2.45 | +0.84 | 12 | 1.04 % |  |
| mid θ0.6 R15 W24h keep T5 x2.0 | 14003 | 94 % | -1.62 | -0.01 | -3.14 | 43 % | -2.46 | +0.84 | 12 | 1.46 % |  |
| mid θ0.6 R15 W24h keep T5 x1.0 | 14003 | 94 % | -1.62 | -0.04 | -3.03 | 43 % | -2.46 | +0.84 | 12 | 1.17 % |  |
| mid θ0.6 R60 W24h keep T5 x2.0 | 14001 | 93 % | -1.62 | -0.02 | -3.15 | 43 % | -2.46 | +0.84 | 12 | 1.47 % |  |
| mid θ0.6 R60 W24h keep T5 x0.5 | 14001 | 93 % | -1.62 | -0.05 | -2.95 | 41 % | -2.46 | +0.84 | 12 | 1.05 % |  |
| mid θ0.6 R60 W24h keep T5 x1.0 | 14001 | 93 % | -1.62 | -0.05 | -3.00 | 42 % | -2.46 | +0.84 | 12 | 1.18 % |  |
| mid θ0.6 R15 W6h keep T5 x0.5 | 13982 | 92 % | -1.63 | -0.02 | -2.97 | 42 % | -2.45 | +0.82 | 12 | 1.04 % |  |
| mid θ0.6 R15 W6h keep T5 x2.0 | 13982 | 92 % | -1.63 | +0.00 | -3.12 | 42 % | -2.45 | +0.82 | 12 | 1.45 % |  |
| mid θ0.6 R15 W6h keep T5 x1.0 | 13982 | 92 % | -1.63 | -0.02 | -3.01 | 42 % | -2.45 | +0.82 | 12 | 1.17 % |  |
| mid θ0.6 R60 W6h keep T5 x2.0 | 13970 | 92 % | -1.63 | +0.00 | -3.11 | 42 % | -2.45 | +0.82 | 12 | 1.47 % |  |
| mid θ0.6 R60 W6h keep T5 x0.5 | 13970 | 92 % | -1.63 | -0.04 | -2.93 | 41 % | -2.45 | +0.82 | 12 | 1.04 % |  |
| mid θ0.6 R60 W6h keep T5 x1.0 | 13970 | 92 % | -1.63 | -0.03 | -2.99 | 41 % | -2.45 | +0.82 | 12 | 1.17 % |  |
| mid θ0.6 R15 W24h chase T5 x1.0 | 14003 | 94 % | -1.66 | -0.05 | -3.09 | 42 % | -2.50 | +0.84 | 12 | 1.28 % |  |
| mid θ0.6 R15 W24h chase T5 x2.0 | 14003 | 94 % | -1.66 | -0.02 | -3.14 | 43 % | -2.50 | +0.84 | 12 | 1.46 % |  |
| mid θ0.6 R15 W24h chase T5 x0.5 | 14003 | 94 % | -1.66 | -0.07 | -3.03 | 41 % | -2.50 | +0.84 | 12 | 1.14 % |  |
| mid θ0.6 R60 W24h chase T5 x2.0 | 14001 | 93 % | -1.66 | -0.03 | -3.15 | 42 % | -2.50 | +0.84 | 12 | 1.48 % |  |

## Trimestres ≥ prod (media por trimestre)

- piloto: límite on-chain anclada al perp + perp taker: 0/5
- mid θ0.6 R15 W24h now +gate swap: 0/5
- mid θ0.6 R15 W6h now +gate swap: 0/5
- mid θ0.6 R15 W24h now: 0/5
- mid θ0.6 R60 W24h now +gate swap: 0/5
- mid θ0.6 R15 W24h now edge 0.3: 0/5
- mid θ0.6 R60 W6h now +gate swap: 0/5
- mid θ0.6 R15 W6h now: 0/5
- mid θ0.6 R60 W24h now: 0/5
- mid θ0.6 R15 W24h now edge 0.7: 0/5
- mid θ0.6 R15 W6h now edge 0.3: 0/5
- mid θ0.6 R60 W6h now: 0/5
- mid θ0.6 R15 W6h now edge 0.7: 0/5
- mid θ0.6 R60 W24h now edge 0.3: 0/5
- mid θ0.6 R60 W6h now edge 0.3: 0/5

## Walk-forward (elige la mejor media de los trimestres anteriores)

- Trimestre 224: piloto: límite on-chain anclada al perp + perp taker
- Trimestre 225: piloto: límite on-chain anclada al perp + perp taker
- Trimestre 226: piloto: límite on-chain anclada al perp + perp taker
- Trimestre 227: piloto: límite on-chain anclada al perp + perp taker

Fuera de muestra: media +0.00, p5 -2.51 (n 12966); prod en los mismos trimestres: media +0.19, p5 -0.88.

## Fees: una orden vs tramos

- Límite Kyber: quien pone la orden no paga comisión ni impacto; el arbitrajista que la llena paga el pool y el gas. Una orden única (todo el nocional) no cuesta más que en tramos: llena por partes a medida que el pool pasa el precio (ARIA: 13 llenados de una orden).
- Swap: la comisión del pool es un % del nocional (igual en 1 o 6 tramos); trocear solo suma gas (~0.05–0.1 USD por tx) y reduce el impacto, que crece con el tamaño de cada swap.
- Perp: maker 0.02 % vs taker 0.05 %: la límite ahorra 0.03 pp por pierna.

## Diferencias con `execution_study_20261108` ("dos límites mismo precio" −0.73 %)

- Allí el precio era el punto medio sin margen a favor (mejor caso −0.04 %); aquí θ ∈ {0, 0.2, 0.4, 0.6} %.
- Allí la pierna que faltaba se cubría a mercado con el impacto del nocional entero y se excluían los intentos sin llenado; aquí hay controles (keep/chase con T/x), el no-llenado cuenta como 0 y el spot llena por capacidad del pool.
- Allí las velas planas (sin trades) contaban como llenados; aquí se descartan y la límite Kyber exige el edge calibrado con órdenes reales.

## Diagnóstico (6 tokens, `mid θ0.4 now`, desglose por caso)

| Pierna | Ambas llenan a la vez | Ejecución | Una llena primero | Ejecución | Basis al intentar (mediana) |
|---|---|---|---|---|---|
| Entrada | 6 % | +1.10 % | 94 % | −0.32 % | +0.29 % |
| Salida | 1 % | +1.02 % | 99 % | −0.84 % | +0.29 % |

- **Cuando ambas llenan juntas el resultado es muy bueno (+1.1 %), pero pasa en 1–6 % de los intentos.** Es lo que se
  recuerda de la operación manual. En el resto llena primero una pierna y la otra termina a mercado o persiguiendo.
- **Selección adversa de la límite Kyber**: la llena un arbitrajista solo cuando el pool la atraviesa. En ese momento el
  mercado ya está más allá del precio de la orden. Calibración con las dos órdenes reales llenadas: JCT +0.10 %
  (1,067 USD), ARIA +0.68 % (3,724 USD, 13 llenados). La pierna perp se cubre a ese mercado → se pierde ese exceso.
- **Salida**: el perp suele seguir sobre el spot (+0.29 % mediana). El empate exige que el basis se comprima y casi
  siempre una pierna se adelanta: −0.84 % por salida.
- Esperar con la otra pierna (keep/chase, T 5–120 min, x 0.5–2 %) empeora: exposición descubierta p95 de 1–2 %.
- Limitación: el modelo cubre la pierna perp al completar la límite on-chain, no por cada llenado parcial. El exceso
  medido en las órdenes reales coincide con el modelo de capacidad (JCT: pendiente × nocional ≈ 0.10 %).

## Veredicto

- **Ninguna política pareada pasa el criterio**: media −1.47 a −1.66 % vs prod +0.18 %, p5 ≈ −2.8 vs −0.91, 0/5 trimestres.
  → No se implementa `PairedLimitLeg`. **Se mantiene "Esperar"**: swap paciente que dispara con el basis
  *ejecutable* al tamaño del tramo, ambas piernas a la vez. Ya es la política "según libro": usa la cotización real del
  pool al tamaño (comisión + impacto) y no deja una pierna sola.
- **Piloto `BscLimitLeg`** (límite on-chain anclada al perp + perp taker): media −0.01 %, p5 −2.57, 0/5 trimestres frente
  a prod → recomendación: **apagarlo** sin esperar las 10 órdenes.
- **Fees**: una orden límite Kyber no paga comisión aunque sea grande, pero su costo real es el exceso con que la
  llenan (0.1–0.7 %). En swaps, trocear no sube la comisión (es % del nocional); solo suma gas (~0.05–0.1 USD por
  tramo) y baja el impacto (2,100 USD de una vez ≈ 0.33 % ≈ 7 USD vs < 1 USD de gas en 6 tramos).
