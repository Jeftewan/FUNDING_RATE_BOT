# Piloto: Claude como decisor vs modelo ML (P0) — 20261118

Ventana 2026-07-01 → 2026-10-22 (posterior al corte de conocimiento del LLM); entradas hasta 2026-09-22. Cartera 10,000 USD, 5 cupos, 1x (1,000 USD de nocional por cupo). Universo: candidatos del modelo (`p_prod` > 0) en 24 tokens solo-Alpha con velas 1m. Motor `profit_study.lifecycle` con la política `variants()[0]` (espera de basis +0.25/−0.15, tramos cada 15 min, SL 80 %, salida B = 2 pagos ≤ 0, tope 30 d).

**Criterio (fijado antes de correr):** el LLM mejora si su rentabilidad mensual media supera a P0 sin empeorar el p10 mensual ni el p5 por operación. Con ~4 meses y ~100 decisiones el resultado es **direccional**, no concluyente.

**Corte:** se detuvo por límites de uso de la suscripción tras 64 días de decisiones; todos los brazos se comparan con las entradas hasta 2026-09-03 (lo abierto al corte sale por la regla B, igual que P0d). %/mes = PnL de esas entradas / días de la ventana de entradas; el p10 del criterio es el de la semana (en %/mes) porque no hay meses completos suficientes.

Brazos: **P0** = `profit_study.portfolio()` (decide en cada liquidación); **P0d** = mismo modelo y salida B pero decide una vez al día a las 00:04 UTC (mismo reloj que el LLM); **RND** = azar entre los candidatos del día, salida B (5 semillas); **LLM** = Claude decide entradas, tamaño (0.5–1.5 cupos) y salidas sin la predicción; **LLM+ML** = igual con `p_prod` visible. El SL, el tope de 30 d y la ejecución los manda el motor.

## 1. Cartera (rentabilidad sobre capital total)

| brazo | ops | %/mes (normalizado) | 2026-07 | 2026-08 | 2026-09 | 2026-10 | p10 semana | USD | DD % | utilización | tamaño medio |
|---|---|---|---|---|---|---|---|---|---|---|---|
| P0 | 20 | +1.85 % | +1.01 | +1.84 | +0.96 | +0.07 | +0.00 | +388 | 0.0 | 94 % | 1.00 |
| P0d_s0 | 18 | +1.25 % | +0.89 | +0.43 | +0.97 | +0.34 | -0.48 | +263 | 0.3 | 94 % | 1.00 |
| RND_s0 | 16 | +1.06 % | +0.67 | +1.03 | -0.02 | +0.55 | -0.05 | +222 | 0.9 | 96 % | 1.00 |
| RND_s1 | 14 | +1.47 % | +0.67 | +1.16 | +0.70 | +0.56 | +0.00 | +308 | 0.0 | 96 % | 1.00 |
| RND_s2 | 16 | +1.27 % | +0.93 | +0.57 | +1.07 | +0.09 | -0.02 | +267 | 0.2 | 96 % | 1.00 |
| RND_s3 | 15 | +1.22 % | +0.70 | +1.44 | +0.11 | +0.32 | -0.06 | +257 | 0.1 | 95 % | 1.00 |
| RND_s4 | 17 | +1.21 % | +0.51 | +1.21 | +0.50 | +0.33 | +0.00 | +255 | 0.0 | 96 % | 1.00 |
| LLM+ML_claude-opus-5-5_r1 | 31 | +3.78 % | +4.19 | +1.96 | +1.80 | +0.00 | +0.00 | +795 | 0.2 | 94 % | 0.99 |
| LLM_claude-opus-5-5_r1 | 31 | +3.72 % | +4.25 | +2.10 | +1.14 | +0.33 | +0.00 | +781 | 0.1 | 92 % | 0.97 |
| RND (media de 5) | 16 | +1.25 % | +0.69 | +1.08 | +0.47 | +0.37 | -0.03 | +262 | 0.2 | 96 % | 1.00 |

## 2. Por operación (% del nocional de cada operación)

| brazo | entradas pedidas | llenadas | media | mediana | p5 | eventos SL | SL % | días (mediana) |
|---|---|---|---|---|---|---|---|---|
| P0 | 76 | 20 | +1.94 | +1.33 | -0.31 | 6 | 30.0 | 22.7 |
| P0d_s0 | 26 | 18 | +1.46 | +1.06 | -1.24 | 5 | 27.8 | 20.4 |
| RND_s0 | 19 | 16 | +1.39 | +1.51 | -2.64 | 4 | 25.0 | 30.0 |
| RND_s1 | 17 | 14 | +2.20 | +1.92 | +0.91 | 1 | 7.1 | 30.0 |
| RND_s2 | 18 | 16 | +1.67 | +1.63 | -1.03 | 5 | 31.2 | 30.0 |
| RND_s3 | 21 | 15 | +1.71 | +1.50 | -0.88 | 4 | 26.7 | 30.0 |
| RND_s4 | 21 | 17 | +1.50 | +1.53 | -0.50 | 5 | 29.4 | 30.0 |
| LLM+ML_claude-opus-5-5_r1 | 31 | 31 | +2.57 | +1.00 | -1.05 | 5 | 16.1 | 7.4 |
| LLM_claude-opus-5-5_r1 | 31 | 31 | +2.54 | +0.92 | -0.10 | 4 | 12.9 | 8.0 |

## 3. Contra P0: bootstrap por semana (PnL semanal por cierre, 5,000 remuestreos)

| brazo | Δ %/mes | IC 95 % | P(Δ > 0) | Δ p10 semana | Δ p5 op | ¿mejora? (criterio) |
|---|---|---|---|---|---|---|
| P0d_s0 | -0.29 | [-0.76, +0.14] | 10 % | -0.48 | -0.93 | no |
| RND_s0 | -0.38 | [-0.97, +0.18] | 10 % | -0.05 | -2.33 | no |
| RND_s1 | -0.18 | [-0.64, +0.27] | 20 % | +0.00 | +1.22 | no |
| RND_s2 | -0.28 | [-0.82, +0.39] | 18 % | -0.02 | -0.72 | no |
| RND_s3 | -0.30 | [-0.68, +0.04] | 4 % | -0.06 | -0.57 | no |
| RND_s4 | -0.31 | [-0.74, +0.10] | 8 % | +0.00 | -0.19 | no |
| LLM+ML_claude-opus-5-5_r1 | +0.93 | [-0.52, +3.04] | 83 % | +0.00 | -0.74 | no |
| LLM_claude-opus-5-5_r1 | +0.90 | [-0.36, +2.91] | 82 % | +0.00 | +0.21 | sí |

## 4. Uso del LLM (suscripción vía `claude -p`; costo = equivalente API que reporta el CLI)

| brazo | llamadas | s/llamada | tokens entrada (incl. caché) | tokens salida | USD equiv. API / llamada | USD equiv. total |
|---|---|---|---|---|---|---|
| LLM+ML_claude-opus-5-5_r1 | 64 | 9 | 7,694 | 586 | 0.0627 | 4.01 |
| LLM_claude-opus-5-5_r1 | 64 | 9 | 7,391 | 581 | 0.0604 | 3.86 |

## 5. Robustez: sin la mecha de TA (07-03)

En ambos brazos LLM, el 45 % del PnL sale de una sola operación: TA, entrada el día 1, **+34.8 % (+348 USD, caso 3)**. El 07-03 a las 18:40 el spot on-chain tocó exactamente 0.15 durante 2 minutos, +43 % sobre el perp; la venta límite Kyber se llenó en la mecha y el short quedó suelto. P0 y RND no tenían TA ese día. El LLM tampoco pudo preverlo: no ve precios futuros y entró por funding y basis. Es suerte, no decisión, y es un caso dudoso del motor: se llena con el máximo de la vela de 1 minuto on-chain.

| brazo (sin operaciones > +20 %) | ops | %/mes | USD | media op | p5 op | p10 semana | Δ vs P0 [IC 95 %] | P(Δ > 0) |
|---|---|---|---|---|---|---|---|---|
| P0 | 20 | +1.85 % | +388 | +1.94 | −0.31 | +0.00 | — | — |
| P0d | 18 | +1.25 % | +263 | +1.46 | −1.24 | −0.48 | −0.29 [−0.76, +0.14] | 10 % |
| RND (media de 5) | 16 | +1.25 % | +262 | +1.69 | −0.83 | −0.03 | −0.29 | — |
| LLM (Opus 5.5) | 30 | +2.06 % | +433 | +1.47 | −0.11 | +0.00 | +0.10 [−0.41, +0.57] | 68 % |
| LLM+ML (Opus 5.5) | 30 | +2.12 % | +447 | +1.49 | −1.06 | +0.00 | +0.13 [−0.59, +0.82] | 66 % |

Contra P0d, que usa el mismo reloj diario, la mejora es LLM +0.39 [−0.37, +1.14] (P 85 %) y LLM+ML +0.42 [−0.55, +1.32] (P 82 %).

## 6. Veredicto

- **Con la mecha:** LLM +3.72 y LLM+ML +3.78 %/mes, contra +1.85 de P0. Sin ella, la ventaja se reduce a **+0.1–0.3 pp/mes** y el IC cruza 0.
- **Criterio (sin la mecha):** el brazo **LLM** lo cumple de forma direccional: más media, el mismo p10 semanal y un p5 por operación de −0.11 contra −0.31. **LLM+ML no lo cumple**: su p5 es −1.06. Ver la predicción del modelo no aportó nada.
- **De dónde sale la diferencia:** la comparación limpia es contra P0d, con el mismo reloj diario.
  - **Rota más:** 30–31 operaciones contra 18, con 8 días de mediana contra 20.
  - **Cierra cuando el basis de venta es favorable** (p. ej., "+0.34 %, asegura +2.07 %") **o el funding queda en el piso**, y evita entrar si el basis no llega al umbral. Todas sus entradas se llenaron (31/31); P0d llenó 18 de 26 y P0, 20 de 76.
  - Es la misma palanca que el estudio del 11-17 (salida paciente con basis ≥ 0), aplicada con criterio en vez de con una regla fija.
- **¿Vale la pena?**
  - **Todavía no como decisor en producción**: 64 días, un solo recorrido por brazo y un IC que cruza 0.
  - **Sí vale una prueba barata y determinista** de lo que hace el LLM: salir cuando el basis de venta es ≥ +0.3 % y el funding cayó al piso, y no entrar si el basis lleva < 50 % del tiempo sobre θ. Se puede backtestear en `profit_study` sin LLM y con 13 meses.
  - Si esa regla captura la ventaja, el LLM sobra. Si no, repetir con 2–3 recorridos por brazo en otra ventana.

## 7. Costo de usar Claude por API (estimado)

- **Por decisión (Opus 5.5):** ~7.5 k tokens de entrada y ~0.6 k de salida → **~0.06 USD**, según el equivalente API que reporta el CLI.
- **Producción, 1 decisión diaria por usuario:** ~1.8 USD/mes.
  - Decidir en cada liquidación (3 al día) lleva a ~5.5 USD/mes.
  - Con reintentos y análisis de posición, el rango está en **3–10 USD/mes por usuario**.
  - Con Sonnet sería ~⅕ de ese costo, pero no se probó.
- **Backtest completo** (13 meses ≈ 395 decisiones por recorrido): ~24 USD por recorrido. Dos brazos × 3 repeticiones ≈ **145 USD**.
- **Suscripción Pro:** 128 llamadas de Opus (2 brazos × 64 días en paralelo) agotaron la ventana de uso. El backtest completo por suscripción toma varios días.

## 8. Limitaciones

- **Corte en el día 64:** el piloto se detuvo por límites de la suscripción, y lo que quedaba abierto al corte salió por la regla B. Hay un solo recorrido por brazo (`rep 1`), así que no se mide la variabilidad del LLM. **El resultado es direccional.**
- **Política de salida de P0:** P0 y P0d usan la salida B con k = 2 (`variants()[0]`). Producción adoptó k = 3 el 2026-11-17 (tercer settlement sin cobrar), pero no se re-corrió aquí: la comparación es contra la política de `profit_study`, no exactamente contra la de producción.
- **Universo prefiltrado:** son los candidatos con `p_prod` > 0, así que el LLM solo eligió dentro de lo que el modelo ya aprueba. RND ≈ P0d indica que, dentro de ese universo, el orden del modelo aporta poco en esta ventana.
- **Motor:** las ventas límite on-chain se llenan con el máximo de la vela de 1 minuto. Mechas de 1–2 minutos como la de TA pueden sobrestimar el caso 3 en todos los estudios con `_exec_engine`.

### Ejemplos de decisiones — LLM+ML_claude-opus-5-5_r1

- día 1: entra ['T15', 'T02', 'T13', 'T05', 'T10'] sale [] — Abro cinco posiciones de 1 cupo cada una. Elegí tokens con funding medio sólido y basis que podría llegar al 0.25 %: T15, T02 y T13; T05 por su basis alto y T10 por su funding reciente. Descarto T11 y T03 porque su basis
- día 13: entra [] sale [] — No hay cupos libres. Todas las posiciones siguen cobrando funding positivo (≥0.03 %/día), sin pagos negativos recientes y lejos del stop-loss. Cerrar ahora cuesta basis de venta de −0.4 a −0.7 %. Los candidatos con más f
- día 26: entra [] sale ['P10'] — P10: funding negativo en 4 de los últimos 6 pagos y basis de venta favorable (+0.34 %); se cierra para asegurar un neto de ~2.07 %. P11, P15, P16 y P17 siguen cobrando funding positivo. Sin entradas: no hay cupos libres.
- día 38: entra [] sale [] — Cartera llena; las 5 posiciones cobran funding positivo (0,15-0,25 %/día) sin pagos negativos. P19 está algo más débil, pero cerrar con basis -0,585 % sale caro. T07 atrae pero es costoso (0,8 % ida y vuelta) y su basis 
- día 51: entra ['T06'] sale [] — Abro T06 en el cupo libre: funding estable (media 0.153), costo bajo y el basis superó el umbral el 63 % del tiempo, así que es probable que entre. Descarto T14, sin basis válido; T11, con basis bajo el umbral, y T07/T15
- día 64: entra [] sale [] — Los 5 cupos están ocupados y las cinco posiciones cobran funding positivo de forma constante, sin pagos negativos. El riesgo de stop-loss es bajo. Ningún candidato justifica una rotación: T03 y T14 tienen el basis por de

### Ejemplos de decisiones — LLM_claude-opus-5-5_r1

- día 1: entra ['T15', 'T13', 'T02', 'T05', 'T10'] sale [] — Abro cinco posiciones de 1 cupo. Elegí funding positivo y estable con un basis que puede llegar al 0.25 %. T15, T13 y T02 tienen buen funding medio; T05 tiene basis alto y costo bajo; T10 sube últimamente. Descarto T11 y
- día 13: entra [] sale ['P11'] — Cierro P11: neto +1,08 %, funding en el piso de 0,03 y basis de venta -0,11 (por encima del umbral). Mantengo P7, que cobra bien. P9, P10 y P13 tienen basis muy negativo y salir ahora fijaría pérdidas. Sin cupos libres, 
- día 26: entra [] sale ['P10'] — Cierro P10: el funding lleva 4 pagos negativos, el basis de venta es favorable (+0.34 %) y deja +2.07 % neto. Mantengo P7, P15, P16 y P17 porque su funding sigue positivo. No abro nada: no hay cupos libres y T01, aunque 
- día 38: entra [] sale [] — Los 5 cupos están ocupados. Todas las posiciones cobran funding positivo y siguen lejos del stop-loss. Cerrar ahora costaría entre 0.3 % y 1.2 % de basis de venta. T07 paga más, pero tiene costos altos (0.8 %) y su basis
- día 51: entra ['T06'] sale ['P23'] — T06: funding estable (media 0.153), costo bajo, basis ejecutable 63% del tiempo, vol moderada. Cierro P23: funding estancado en el piso 0.03 y basis de venta aceptable (−0.139); libera cupo para rotar. Evito T07/T15 por 
- día 64: entra [] sale [] — Mantengo todo: los 5 cupos están ocupados y todas las posiciones cobran funding positivo sin pagos negativos. Ninguna está cerca del stop-loss. P31 cobra poco ahora, pero su media de 30 días (0.139) supera la de T16 (0.1
