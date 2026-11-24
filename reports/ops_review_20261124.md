# Revisión de la operación real — 2026-11-24

Fuente: `scripts/ops_review.py` (solo lectura: DB de prod, fills e income de Binance con las keys de cada usuario, transferencias BEP-20 de las wallets manuales) y `scripts/ops_review_report.py`. Los usuarios 2 y 4 comparten la misma cuenta Binance (income idéntico); el 5 es caoviedor.

## Resumen

- **Q1 — USDT igual vs cantidad igual: la diferencia es ruido.** En los 10 trades manuales reconstruidos, el delta residual de cubrir con el mismo USDT sumó -11.8 USD; con cantidad igual el neto habría sido +496.8 vs +484.7 USD reales. En 13 meses de backtest (25,855 operaciones del motor) la diferencia media es +0.010 pp por operación (IC +0.001/+0.017) y gana 3/5 trimestres → **no pasa el criterio**. Ambas reglas pagan igual la basis de entrada y la de salida; el bot de funding de Binance también cubre por cantidad.
- **La basis sí afectaba en la era manual, solo que no se medía** (`price_pnl` vacío). Reconstruido: +103.3 USD de precio, pero +189.5 viene de un solo trade (ESPORTS #33: el spot se vendió 41 min después de recomprar el short, en plena subida). Sin él, el precio fue -86.1 USD = 24 % del funding (358.3). El funding manual de la DB era estimado: 411.3 vs 391.2 USD reales (income).
- **caoviedor (19 d, capital 9,293 USD)**: funding +1.54 %/mes, comisiones perp -0.14, precio (cerrado + a mercado) -0.87 → **+0.53 %/mes**. El precio se come 56 % del funding; 62 % de esa pérdida es la salida por SL de ARIA a 2x. Aun con precio y fees en cero el funding da ~1.5 %/mes: **3 %/mes no está al alcance a 1x en este mercado** (igual que `profit_study_20261117`: ~2.0 %/mes esperado a 1x).
- **La espera de basis no aparece en los llenados reales**: entre la lectura que dispara y el llenado la basis cae -0.37 pp en promedio (n=5). Entradas con espera: -0.08 % realizado; sin espera -0.01 %. En el mercado horario la basis revierte: una hora ≥ 0.25 % (media 0.65) promedia 0.38 la siguiente. Las salidas esperaron 15–25 h y aun así pagaron basis.
- **El costo dominante es la basis de salida**, en manual y en auto (ida y vuelta mediana 0.43 pp manual / 0.66 pp auto). Lo que lo diluía en la era manual era la **tenencia**: mediana 17 d manual vs 5 d auto. ROC mensual ponderado por capital-tiempo: manual +3.62 % (sin #33 +2.18 %), auto -0.65 %.

## Manual vs auto: qué cambió

| Grupo | n | funding %/día del nocional | precio % del nocional | fees % | días (mediana) | lev | ROC %/mes |
|---|---|---|---|---|---|---|---|
| Manual | 10 | 0.194 | +0.87 | 0.08 | 17.1 | 1.0 | +3.62 |
| Manual sin ESPORTS #33 | 9 | 0.197 | -0.82 | 0.08 | 20.9 | 1.0 | +2.18 |
| Auto (todos) | 10 | 0.113 | -1.17 | 0.10 | 4.8 | 1.3 | -0.65 |
| Auto ≥ 500 USD | 6 | 0.111 | -0.60 | 0.10 | 10.3 | 1.5 | +0.59 |

El costo de precio por operación es del mismo orden en ambas eras (~0.6–0.8 % del nocional). La era manual rindió más por **funding ~1.7× mayor** (julio–octubre, otro régimen y otra selección) y por **tenencias el doble de largas**, que reparten el mismo costo de salida entre más días de cobro. No por la regla de cobertura.

## Q1 — Cobertura por USDT igual vs por cantidad igual

Con X USD por pierna, b = P/S − 1 y r = P₁/P₀:

- USDT igual: PnL/X = (S₁/S₀)·(b₀ − b₁)/(1 + b₀)
- Cantidad igual: PnL/X = b₀ − (S₁/S₀)·b₁
- **Cantidad − USDT = −b₀·(r − 1)** (+ b₀·funding, porque el short es b₀ más grande): un delta largo residual de b₀ del nocional. Con b₀ ±0.3 % y el precio ±20 % son ±0.06 % del nocional, de signo aleatorio.

Descomposición exacta (USD): precio = qs·(P₀ − S₀) **[entrada]** − qs·(P₁ − S₁) **[salida]** + (qs − qp)·(P₁ − P₀) **[delta]**. P y S son precios de ejecución (incluyen impacto y spread).

### Trades manuales reconstruidos (swaps on-chain + fills del perp)

| user | # | token | días | USD spot | b₀ % | b₁ % | lag spot entrada min | lag spot salida min | entrada | salida | delta | precio | funding | fees perp | neto | neto con cantidad igual |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| 2 | 28 | ESPORTS | 7.0 | 766 | +0.33 | +0.39 | -2 | +2 | +2.54 | -3.10 | +0.10 | -0.47 | +19.18 | +0.39 | +18.32 | +18.29 |
| 2 | 31 | VELVET | 3.8 | 782 | +0.60 | +0.08 | -0 | +1 | +4.66 | -0.16 | -4.47 | +0.04 | +3.78 | +0.49 | +3.33 | +7.83 |
| 4 | 33 | ESPORTS | 13.2 | 1,455 | -0.71 | -7.43 | -1 | +41 | -10.38 | +208.83 | -9.00 | +189.45 | +32.88 | +1.60 | +220.73 | +229.47 |
| 4 | 35 | BULLA | 6.1 | 1,566 | -0.29 | +1.57 | -1 | +9 | -4.48 | -25.07 | -0.03 | -29.58 | +15.18 | +1.11 | -15.51 | -15.49 |
| 4 | 36 | HANA | 20.9 | 1,559 | -0.08 | +0.45 | -0 | +1 | -1.32 | -7.64 | +0.09 | -8.87 | +52.42 | +1.12 | +42.43 | +42.38 |
| 2 | 38 | BTW | 9.5 | 841 | +0.06 | -1.35 | -0 | +0 | +0.48 | +20.77 | +1.95 | +23.20 | +49.21 | +1.18 | +71.23 | +69.42 |
| 2 | 39 | ESPORTS | 26.9 | 876 | +0.25 | +2.33 | +0 | +2 | +2.15 | -12.91 | -1.28 | -12.04 | +66.85 | +0.48 | +54.34 | +55.89 |
| 4 | 40 | STAR | 22.0 | 1,580 | -0.17 | +0.23 | +0 | +0 | -2.75 | -2.11 | +1.14 | -3.72 | +54.14 | +0.78 | +49.64 | +48.41 |
| 2 | 41 | KOMA | 25.9 | 903 | -0.03 | +0.43 | +0 | +0 | -0.28 | -3.22 | -0.33 | -3.83 | +24.08 | +0.83 | +19.42 | +19.81 |
| 4 | 42 | SIREN | 25.9 | 1,606 | -0.31 | +2.35 | +0 | -25 | -4.93 | -45.92 | +0.01 | -50.84 | +73.44 | +1.80 | +20.81 | +20.80 |

Sin fills: las manuales de marzo–junio (la API da 6 meses de `userTrades`; las de junio de slavado no aparecen en esa cuenta). ZIL #37 se excluye (no es short perp + spot largo). Fees perp = comisión real; el costo del swap está dentro del precio.

### Trades auto (exec_json) y contrafactual con USDT igual

| user | # | token | lev | días | USD spot | b₀ % | b₁ % | entrada | salida | delta | precio | funding | fees | neto | neto con USDT igual |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| 2 | 44 | AGT | 1x | 5.1 | 251 | +0.16 | +13.00 | +0.41 | -49.43 | +0.03 | -48.99 | +1.94 | +0.38 | -47.43 | -47.18 |
| 2 | 47 | IRYS | 1x | 1.5 | 235 | -0.05 | +0.02 | -0.11 | -0.01 | -0.01 | -0.13 | -0.76 | +0.18 | -1.08 | -1.00 |
| 2 | 50 | KOMA | 1x | 0.9 | 246 | -0.50 | +0.59 | -1.22 | -1.41 | -0.00 | -2.63 | +0.10 | +0.29 | -2.83 | -2.81 |
| 4 | 48 | AGT | 1x | 4.5 | 1,610 | +0.37 | +0.32 | +6.02 | -2.36 | -0.04 | +3.61 | +6.08 | +1.24 | +8.45 | +5.22 |
| 2 | 51 | AIN | 1x | 3.6 | 240 | +0.71 | +4.55 | +1.71 | -18.37 | +0.02 | -16.64 | +4.01 | +0.36 | -13.00 | -11.77 |
| 4 | 52 | ZEST | 1x | 9.0 | 1,611 | -0.63 | +0.46 | -10.11 | -6.39 | -0.00 | -16.50 | +12.20 | +1.55 | -5.86 | -4.44 |
| 5 | 56 | ARIA | 2x | 2.9 | 2,780 | +0.23 | +0.83 | +6.38 | -31.07 | +0.01 | -24.68 | +22.84 | +3.32 | -5.16 | -3.00 |
| 2 | 49 | TAC | 1x | 20.7 | 662 | -0.33 | +0.34 | -2.16 | -2.05 | -0.00 | -4.22 | +16.14 | +0.69 | +11.24 | +11.46 |
| 5 | 54 | STAR | 2x | 11.5 | 2,012 | -0.02 | +0.64 | -0.43 | -13.14 | +0.00 | -13.57 | +25.39 | +2.07 | +9.75 | +9.74 |
| 5 | 55 | CLO | 2x | 17.6 | 1,343 | -0.25 | +0.17 | -3.32 | -1.36 | -0.01 | -4.69 | +17.45 | +1.13 | +11.63 | +13.05 |

Σ neto auto -34.3 vs -30.7 USD con USDT igual (casi todo por funding: con b₀ < 0 el short de USDT igual es más grande).

### Backtest sistemático (motor de `profit_study`, política vigente P0, 1,000 USD)

- 25,855 operaciones de 25 tokens, 2025-10 → 2026-10; r con el cierre 1h del perp. Δ (USDT − cantidad) +0.0097 pp, IC 95 % +0.0013/+0.0173, Δp5 -0.041 pp, 3/5 trimestres → **no pasa**. |Δ| medio 0.095 pp; correlación de b₀ con el retorno posterior -0.011 (b₀ no anticipa la dirección).
- El motor ya valuaba como USDT igual (spot/SE − perp/PE) y prod opera cantidad igual: la diferencia no cambia ninguna conclusión de los estudios previos.

## Q2 — Por qué caoviedor rinde ~0.5 %/mes

| Concepto | USD | %/mes sobre capital |
|---|---|---|
| Funding (income real) | +88.57 | +1.54 |
| Comisiones perp | -8.17 | -0.14 |
| Precio cerrado (ARIA, STAR, CLO) | -42.94 | -0.75 |
| Precio a mercado (JCT, TAC) | -6.97 | -0.12 |
| **Neto** | +30.49 | +0.53 |

Capital promedio 9,293 USD, desplegado 88 %, 18.6 d desde la primera apertura. Referencia slavado (auto, 32 d): funding +1.11, fees -0.09, precio -0.51 → +0.51 %/mes.

Causas, en orden:

1. **Nivel de funding**: ~33 % APR sobre el nocional; a 1x cobra la mitad del capital → ~1.5 %/mes bruto. Para 3 %/mes neto haría falta ~75 % APR sostenido o leverage con rebalanceo (estudio previo: 2x ≈ 2.3 %/mes).
2. **ARIA a 2x cerró por SL** a los 3 d: salida −31.1 USD (35 % de todo el funding del periodo). Las tres primeras se abrieron a 2x y sin espera (antes del 2026-11-10); desde entonces el usuario abre a 1x.
3. **Basis de salida** en STAR (+0.64 % de costo, cerrada con «Ejecutar ya» tras 22 h de espera) y CLO (+0.17 % tras 14.7 h). La espera de salida no consiguió basis ≥ 0 en ningún cierre.
4. **Tenencias cortas**: 3, 11.5 y 17.6 d. Con ~0.1 %/día de funding sobre el nocional, un costo de ida y vuelta de 0.6 pp necesita ≥ 30 d para quedar bajo el 20 % del funding.

## ¿Funciona la espera de basis?

| job | user | tipo | token | estado | θ | espera h | total h | lectura al disparar | antes del 1er tramo | realizado | tramos | funding en la espera |
|---|---|---|---|---|---|---|---|---|---|---|---|---|
| 27 | 5 | open | STAR | done | — | 0.01 | 0.01 | — | — | -0.02 | — | — |
| 28 | 5 | open | CLO | done | — | 0.01 | 0.01 | — | — | -0.25 | — | — |
| 30 | 5 | open | ARIA | done | — | 0.01 | 0.01 | — | — | +0.23 | — | — |
| 32 | 4 | close | ZEST | done | — | 0.01 | 0.01 | — | — | -0.46 | — | +0.00 |
| 35 | 4 | open | 4 | done | +0.25 | 0.19 | 0.27 | — | — | -0.19 | — | — |
| 37 | 5 | open | JCT | done | +0.25 | 0.34 | 0.35 | +0.34 | — | -0.35 | — | — |
| 38 | 2 | close | TAC | done | -0.15 | 0.02 | 0.88 | +0.33 | -0.29 | -0.34 | 4 | +0.00 |
| 39 | 5 | close | STAR | cancelled | -0.15 | 22.22 | 22.22 | — | — | — | — | -1.36 |
| 40 | 5 | close | STAR | done | -0.15 | 2.22 | 2.80 | — | -0.54 | -0.64 | 3 | +0.00 |
| 41 | 5 | open | TAC | done | +0.25 | 0.04 | 0.33 | +0.29 | +0.21 | +0.05 | 2 | — |
| 42 | 5 | close | CLO | done | +0.00 | 14.42 | 14.71 | +0.01 | -0.17 | -0.17 | 2 | +0.14 |
| 43 | 2 | open | BULLA | done | +0.25 | 0.00 | 0.29 | +0.26 | +0.24 | +0.18 | 3 | — |
| 44 | 5 | close | JCT | armed | +0.00 | 12.82 | 12.82 | — | — | — | — | +0.14 |

Basis en %, convención de la lectura (+ = ganancia; en cierres = −`exit_basis_pct`).

Mercado horario (`market_feature_snapshots`, 51,291 horas, 87 tokens Alpha, basis de mids): mediana +0.127 %. Si una hora marca ≥ 0.25 % (media 0.65), la siguiente promedia 0.38 y solo 51 % sigue ≥ 0.25; con ≥ 0.5 (1.07) → 0.58. La regla «primera hora ≥ 0.25 en 6 h» lee 0.34 % vs 0.08 % al pedir, pero una hora después de esa lectura la basis está en 0.13 %: los picos que disparan duran menos que la ejecución (2–4 tramos cada 15 min).

Lectura: el disparo selecciona picos ruidosos (sesgo del ganador) y los tramos de 15 min llegan cuando el pico ya revirtió. El motor (`exec_study`) daba +0.16 pp a la espera; con estas pocas ejecuciones reales no se confirma ni se refuta, pero la dirección del sesgo es clara. ρ (recuperación del pool) sigue sin medirse: ningún tramo guardó `recovery` utilizable.

## Técnicas evaluadas

| Técnica | Resultado | Decisión |
|---|---|---|
| Cubrir con USDT igual | Δ +0.010 pp, 3/5 trimestres | No pasa; no cambiar |
| Disparar según el reloj del settlement | basis vs su media del día por minutos al pago: 0–60 min +0.003, 60–120 min -0.006, 120–240 min +0.001 pp | Sin efecto; descartar |
| Exigir persistencia (2 lecturas ≥ θ separadas ≥ 5 min) | El exceso sobre la mediana revierte ~50 % en 1 h | Hipótesis: requiere registrar cada lectura del probe para medirla |
| Perp maker (post-only) | Ahorro ≤ 0.06 pp por ida y vuelta (0.05 → 0.02 % por lado) con riesgo de quedar sin cubrir | Menor que el costo de basis; no prioritario |
| Tope 1x en Alpha | ARIA 2x: −31 USD por SL | Ya recomendado; evaluar bloquear > 1x en BSC |
| Tenencia larga / no cerrar por señal débil | Manual (mediana 17 d) diluía el mismo costo de salida | Ya adoptado (3.er settlement sin cobrar); vigilar cierres manuales tempranos |

## Recomendaciones

1. **No cambiar la regla de cobertura** (cantidad igual). Es la del bot de Binance y la diferencia es ruido.
2. **Registrar cada lectura del probe** (`progress_json` se pisa en cada lectura): basis, hora y tamaño, y la basis realizada por tramo. Con ~20 ejecuciones se decide si la espera aporta o si conviene ejecutar al pedir y evitar 15–25 h de espera en las salidas.
3. **Revisar el margen del guard**: `FIRE_BASIS_MARGIN` deja ejecutar hasta θ − 0.25 (entradas desde 0 %, salidas hasta −0.25 %). Con la reversión medida, el guard debería comparar contra el umbral completo o re-armar; probarlo con los datos del punto 2.
4. **Alpha a 1x siempre** (el SL a 2x fue la mayor pérdida del usuario).
5. **Expectativa**: en el régimen actual el neto realista a 1x es ~1–2 %/mes; 3 %/mes requiere funding > 70 % APR o leverage con rebalanceo validado (`feat/signals-telegram`).

Fuentes externas: He, Manela, Ross, von Wachter, *Fundamentals of Perpetual Futures* (arXiv 2212.06888), bandas de no arbitraje con costos; Binance, *Funding Rate Arbitrage Bot* (FAQ), el bot iguala la cantidad del futuro a la del spot ejecutado.
