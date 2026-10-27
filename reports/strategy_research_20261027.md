# Investigación de la estrategia: viabilidad a largo plazo, optimizaciones e innovaciones — 2026-10-27

Alcance: solo la estrategia de trading (carry spot_perp delta-neutral en Binance, universo CEX ∪ Alpha). No cubre basyo como producto. Complementa `reports/research_funding_ml_20261009.md` (predictibilidad y validación ML), que no se repite aquí. Cifras internas en `reports/strategy_viability_20261027.md` (`scripts/strategy_viability.py`). **[NV]** = fuente no verificada a fondo (prensa, blog, fuente interesada o lectura parcial).

## Resumen ejecutivo

- **Veredicto: rentable a largo plazo como estrategia de nicho y de capacidad limitada, no como negocio escalable.** El carry de majors converge a la tasa libre de riesgo (Sharpe del carry BTC negativo en 2025; basis de majors ≈ 4–6 % en 2026) porque lo arbitran Ethena, ETF y vaults delta-neutral. El edge del bot está en la **cola de altcoins / tokens Alpha**, donde ese capital no llega (sin spot CEX, sin colateral en Portfolio Margin, pools on-chain chicos).
- **El edge del nicho no muestra decaimiento.** En la simulación por trimestre (cota inferior), CEX ∪ Alpha rinde 13–23 % anualizado entre 2025Q3 y 2026Q3 mientras CEX solo queda en ≈ 0 o negativo desde 2025Q2. El walk-forward de 13 meses da 29,3 % anual, 14/14 meses positivos, y sigue en 20,4 % sin los 10 tokens de mayor PnL. La literatura apoya que la dispersión transversal del funding **crece** (contrato-días en el ancla: 58 % en 2020 → 7 % en 2026) aunque la media se comprima.
- **El límite real es la capacidad on-chain, no la compresión ni la fee.** El impacto medido en los pools BSC (cotizaciones KyberSwap de hoy, 30 tokens operados) sube de 0,25 % (1.000 USD) a 0,92 % (5.000 USD) y 4,8 % (26.000 USD) por vía. Con ese costo el rendimiento simulado cae de 29 % (5–10 k USD) a 24 % (26 k), 20 % (52 k), 14 % (104 k) y 4 % (260 k). La ganancia absoluta toca techo cerca de 100 k USD de capital (~15 k USD/año) y luego baja.
- **Prioridades:** (1) dimensionar cada entrada Alpha por la curva de impacto del token (no por slots iguales) y llevar el excedente a CEX 3x / Simple Earn; (2) ejecución BSC por tramos (TWAP) para subir la capacidad; (3) salidas anticipadas ante desbloqueos y deslistados Alpha; (4) monitoreo de régimen. No aportan al nicho: Portfolio Margin, Boros como cobertura, fijar con trimestrales, más leverage, sizing Kelly.

---

## 1. ¿Qué sostiene el carry y por qué se comprime?

- **Origen de la prima.** El funding positivo existe porque la demanda apalancada de largos (minorista) supera al capital de arbitraje disponible para cubrirla; las desviaciones perp–spot se explican por segmentación (sin margen cruzado entre venues ni entre spot y derivados) y por límites al arbitraje [He et al., v7](https://arxiv.org/html/2212.06888v7); [Schmeling et al., 2023](https://www.bis.org/publ/work1087.pdf).
- **Tendencia estructural a la baja.** En He et al. (v7, Binance, 5 monedas) la desviación perp–spot cae ≈0,20–0,23 pp por año (t ≈ −5 a −8) y sube con el retorno pasado de 120 días; el componente funding del retorno de su estrategia de convergencia es menor y decreciente frente al de convergencia de precio. El carry BTC pasa de Sharpe 6,45 (2020–2025) a 4,06 desde 2024 y a negativo en 2025 [Borri et al., 2025](https://arxiv.org/html/2510.14435v2).
- **2026.** Fuentes de mercado ubican el carry de majors en ≈4–7 % anual (BTC basis 7 d ≈ 6,3 %, 90 d ≈ 4,7 % a inicios de 2026; alts como SOL/DOGE casi en cero) [Kaiko/Amberdata, ene-2026](https://www.kaiko.com/resources/digital-asset-snapshot-2026-rally-etf-inflows-positioning-shifts) **[NV]**; BitMEX reporta que el rendimiento "fácil" del arbitraje de funding cayó por debajo del 4 % tras el 10-oct-2025 por la entrada de imitadores [CoinDesk/BitMEX, ene-2026](https://www.coindesk.com/markets/2026/01/08/october-s-crypto-crash-left-market-makers-stuffed-with-coins-slowing-trading-bitmex) **[NV]**; sUSDe (Ethena) rinde ≈3,5–4,8 % en 2026 con solo una fracción del respaldo en el basis trade [Aavescan](https://aavescan.com/rates/ethena-susde) **[NV]**. Interno: funding BTC 11,9 % (2024) → 5,1 % (2025) → 3,0 % (2026 YTD).
- **Quién comprime.** Ethena (USDe, pico ~14,8 B USD), ETF/basis institucional y vaults delta-neutral son arbitrajistas masivos **en majors**; su capital no llega a tokens de baja capitalización porque necesita liquidez, colateral aceptado y escala. Ethena reduce su exposición al basis cuando el funding cae (≈1–20 % del respaldo en 2026) **[NV]**, lo que deja de presionar a la baja en malos regímenes.

**Implicación.** El carry de majors es ya un rendimiento de "tasa libre de riesgo + poco" y no compensa el riesgo operativo de este bot. La tesis de largo plazo depende de que la **prima en la cola de altcoins** persista porque está fuera del alcance del capital institucional.

## 2. El nicho: altcoins y tokens Alpha

- **Dispersión creciente, no decreciente.** Un estudio sin sesgo de supervivencia (791 contratos USDT de Binance incluidos deslistados, 2,43 M liquidaciones) muestra que la fracción de contrato-días con funding exactamente en el ancla de interés (0,01 %/8 h) cae de 58 % (2020) a 7 % (2026); el funding solo tiene información transversal cuando deja de ser constante, y desde 2024 el nivel de funding predice retornos a ≤ 8 h [OctopusTakopi, funding-rate-alpha](https://github.com/OctopusTakopi/funding-rate-alpha) **[NV: estudio de practicante, no revisado por pares]**. Es decir: mientras la media se comprime, la **cola** se ensancha — justo donde opera el bot.
- **El carry de la cola se cobra, el precio es ruido.** En ese estudio el carry del lado short es monótono en la tasa (−11 a +4,9 pb/día por quintil) y el retorno de precio a 24 h no es monótono; la estrategia neta de costos en swing (rebalanceo cada 3 días) da ≈+32 % anual pero concentrada en 2020–21 (sin esos años, Sharpe 0,77, t = 1,63). Advertencia directa para nosotros: los rendimientos altos del carry tienden a estar concentrados en pocos años y pocos nombres.
- **Carry transversal en la literatura.** Fan, Jiao, Lu & Tong encuentran 43,4 % anual (Sharpe 0,74) en un carry largo-corto entre monedas no explicado por factores conocidos [SSRN 4666425](https://papers.ssrn.com/sol3/papers.cfm?abstract_id=4666425) **[NV: solo resumen]**; Cao et al. explican 63 estrategias rentables en perps con dos factores, uno de ellos el **log basis** [SSRN 6365329](https://papers.ssrn.com/sol3/papers.cfm?abstract_id=6365329) **[NV: solo resumen]** — consistente con que el basis aportó IC sobre el residuo del modelo (`reports/new_signals_probe_20261025.md`). Un trabajo con 10 perps de Binance no encuentra alfa transversal a 8 h [SSRN 6701738](https://papers.ssrn.com/sol3/papers.cfm?abstract_id=6701738) **[NV]**: el edge no está en majors.
- **Por qué existe la prima en Alpha.** (i) Los tokens Alpha no tienen spot en el CEX → el arbitrajista necesita una wallet on-chain, gas, un agregador y tolerar riesgo de pool; (ii) no sirven como colateral en Portfolio Margin → no hay margen cruzado y el capital rinde la mitad para un arbitrajista grande; (iii) airdrops y desbloqueos generan oferta spot y demanda de cobertura corta de holders, pero también **demanda especulativa larga** en el perp de tokens recién listados. Los fondos que cubren desbloqueos con shorts empujan el funding a negativo (TIA llegó a −400 % anual) [Galaxy](https://galaxy.com/insights/perspectives/bitcoin-futures-basis-and-funding-rates-remain-relatively-muted-amidst) **[NV]**: un desbloqueo cercano es señal de salida, no de entrada.
- **Riesgos propios del nicho.** Deslistado masivo de Alpha (12 tokens en mayo-2026, caídas > 50 % en horas) [Hokanews](https://www.hokanews.com/2026/05/binance-alpha-just-executed-12-tokens.html) **[NV]**; pocos tokens listados en Binance siguen sobre su precio de listado a 6 meses [FXStreet, 2024](https://www.fxstreet.com/cryptocurrencies/news/binance-effect-fades-less-than-20-tokens-are-profitable-six-months-after-listing-202405201006) **[NV]** (irrelevante con cobertura, pero eleva la frecuencia de disparos del SL y del caso A de los seguros); y Binance cambia el intervalo de funding a **1 h** cuando la liquidación toca el cap/floor (regla 8.1, vuelve a 4 h tras 36 periodos con |tasa| ≤ 0,002 %) — los tokens Alpha de carry extremo son justamente los candidatos a ese cambio [Binance, anuncio de listado](https://www.binance.com/support/announcement/0ffd2dbcc12e4a968fbe7ccd2b4cc9f8) **[NV]**.

## 3. Datos propios: ¿se sostiene el edge?

Detalle y supuestos en `reports/strategy_viability_20261027.md` (salida H30, 1x, 5 slots, 5.200 USD, fee CEX 0,30 % / Alpha 0,41 %).

**Edge por trimestre** (ranking por tasa vigente, sin modelo → sin fuga in-sample; cada trimestre se simula aislado, así que es cota inferior):

| trimestre | CEX solo, anual % | CEX ∪ Alpha, anual % |
| --- | --- | --- |
| 2025Q1 | 2,1 | 2,5 |
| 2025Q2 | −1,9 | 5,9 |
| 2025Q3 | 3,9 | 13,4 |
| 2025Q4 | −3,3 | 21,7 |
| 2026Q1 | −3,1 | 14,4 |
| 2026Q2 | −1,6 | 23,2 |
| 2026Q3 | 1,6 | 19,3 |
| 2026Q4 (22 d) | −7,0 | 1,0 |

Lectura: el carry CEX "normal" ya no paga fees desde 2025Q2. El de Alpha crece con el número de tokens listados (Alpha arranca en el universo el 2024-12-18) y se mantiene. El último trimestre está incompleto y casi no tiene cierres, así que no es señal.

**Walk-forward 2025-09-22 → 2026-10-22, modelo vigente:** 29,3 % anual, 86 trades, ocupación de slots 99,8 %. Es ≈ 25 pp sobre la referencia libre de riesgo (4 %). Retorno mensual de +1,4 % a +4,4 % sobre el capital, sin meses negativos.

**Concentración:** 44 símbolos operados; el top-5 es LYN, BULLA, SIREN, GUA y JELLYJELLY, y el top-3 suma el 26 % del PnL. Si se excluyen del universo los 1/3/5/10 mejores, el rendimiento queda en 27,6 / 24,6 / 21,9 / 20,4 %. A diferencia del cross perp (que dependía de 3 símbolos), el edge está repartido.

**Fee:** con ×0,5 rinde 30,9 %, con ×1 29,3 % y con ×2 26,0 %. Bajar la fee de entrada/salida mejora poco: los holds largos (~20 d) la diluyen.

**Capacidad (impacto on-chain medido):**

| capital USD | slots | N/pierna USD | impacto 1 vía % | fee Alpha efectiva % | anual % | USD/año |
| --- | --- | --- | --- | --- | --- | --- |
| 5.200 | 5 | 520 | 0,25 | 0,41 | 29,3 | ≈ 1.500 |
| 10.400 | 5 | 1.040 | 0,26 | 0,44 | 29,1 | ≈ 3.000 |
| 26.000 | 5 | 2.600 | 0,56 | 1,04 | 24,3 | ≈ 6.300 |
| 52.000 | 10 | 2.600 | 0,56 | 1,04 | 20,5 | ≈ 10.700 |
| 104.000 | 10 | 5.200 | 0,99 | 1,91 | 14,0 | ≈ 14.600 |
| 260.000 | 20 | 6.500 | 1,44 | 2,81 | 3,8 | ≈ 9.900 |

Por token, el impacto de 2.600 USD va de 0,10 % (BTW, SKYAI) a 5,7 % (SLX), y no siempre sigue a la liquidez declarada. En el token mediano, 5.000 USD de compra mueven el pool ≈ 0,9 %. Con más slots el capital se reparte entre peores candidatos (52 k a 10 slots rinde menos que 26 k a 5). Sesgos: la liquidez es la de hoy y se asume una entrada de un solo tramo, así que el TWAP mejoraría la tabla; la lista Alpha es la actual (supervivencia).

## 4. Optimización de capital

- **Portfolio Margin / PM Pro no aplica al edge.** PM permite usar el spot como colateral del short (la razón por la que los grandes arbitrajistas comprimen el carry de majors), pero (a) los tokens Alpha viven en la wallet BSC, no en la cuenta Binance, (b) los altcoins tienen ratios de colateral de 10–30 % que Binance recorta seguido (20 activos a 10 % en 2026) [CryptoSlate](https://cryptoslate.com/exchanges-lower-token-risk-values-leaving-leveraged-traders-with-less-breathing-room/) **[NV]** y (c) PM Pro es por invitación [Tread docs](https://docs.tread.fi/account-creation-and-api-key-connection/connecting-to-exchanges/binance/binance-portfolio-margin-pro) **[NV]**. Solo mejoraría la pierna CEX (3–5 % anual): **no prioritario**.
- **Leverage del perp = la palanca de capital que sí existe.** Ya calibrado: Alpha 2x (36–44 % anual simulado), CEX 3x (`reports/leverage_study_20261025_*.md`). Subir más aumenta disparos del SL (57 % de entradas a 3x en Alpha) y pierde.
- **Capital ocioso.** El USDT libre (margen de reserva y slots vacíos) puede rendir en Simple Earn flexible (rango publicado ≈2,5–5 %, varía a diario) [Datawallet](https://www.datawallet.com/de/krypto/best-tether-usdt-yield-opportunities) **[NV]**. Con la ocupación medida en §3 el aporte es marginal pero gratis; requiere redimir antes de abrir (latencia) y no puede ser el margen del perp.
- **Sizing.** Con 5 slots iguales el capital se reparte sin mirar la convicción; la literatura de cartera sugiere Kelly fraccional / media-varianza, pero con el IC disponible y la concentración observada (§3) el beneficio esperado es menor que el riesgo de sobreajuste. Recomendación: mantener slots iguales y limitar el peso por token.

## 5. Optimización de costos

- **Fees de Binance.** Perp: 0,02 % maker / 0,05 % taker en VIP0, −10 % pagando con BNB **en la cuenta USDⓈ-M** [Binance FAQ](https://www.binance.com/es/support/articles/360033544231); spot CEX 0,10 % (−25 % con BNB) [Cryptopotato](https://cryptopotato.com/binance-fees/) **[NV]**. Los tiers VIP de futuros exigen 5× el volumen de spot → fuera de alcance con el capital actual.
- **El costo dominante es el on-chain.** La fee Alpha real (mediana 0,41 % del nocional por trade, swap + gas + slippage del pool) es ~1,4× la CEX. La sensibilidad de §3 dice cuánto vale reducirla; las palancas son ruta del agregador, tamaño por tramo, horario de menor gas/volatilidad y la venta límite Kyber (maker) en la salida cuando no hay urgencia.
- **Fijar el carry con futuros trimestrales** solo existe para majors (curva BTC casi plana, 4,7–5,6 %) **[NV]** → no aplica al nicho.

## 6. Innovaciones del campo

- **Mercados de tasas de funding — Pendle Boros** (ago-2025, Arbitrum): swaps fijo-por-flotante liquidados en efectivo contra el funding de Binance/Hyperliquid (Yield Units: largo YU = paga fijo, recibe flotante) [Pendle docs](https://pendle.gitbook.io/boros/advanced-strategies/hedging-funding-rates-payment); [Chaos Labs](https://chaoslabs.xyz/posts/defi-rate-swaps-building-risk-infrastructure-for-boros). Permite a un carry trader **vender** su funding flotante y fijar la tasa. Escala aún pequeña (OI ≈ 91 M USD a fin de 2025, mercados BTC/ETH/BNB/SOL y algunos equity perps) [Blockworks](https://blockworks.com/news/boros-sleeper-perps) **[NV]**. **No cubre altcoins Alpha** → hoy inútil como cobertura del bot, pero su tasa implícita BTC/ETH es un **termómetro gratuito** del carry esperado por el mercado (régimen).
- **Vaults delta-neutral y dólares sintéticos** (Ethena, Hyperliquid HLP ≈15 % a 1 año a jun-2026 con drawdowns [Trading Strategy](https://tradingstrategy.ai/vaults/hyperliquidity-provider-hlp) **[NV]**): son el benchmark de costo de oportunidad pasivo. Si el bot no supera con holgura a sUSDe/HLP neto de su riesgo operativo, el capital rinde más ahí.
- **Diseño de funding y riesgo de cola.** Kim & Park proponen fórmulas de funding que anclan mejor el perp [arXiv 2506.08573](https://arxiv.org/abs/2506.08573): menos desviación ⇒ menos carry. KuCoin ya re-escaló el funding por intervalo en jul-2026 [KuCoin](https://www.kucoin.com/announcement/en-revision-of-the-funding-rate-formula-for-kucoin-perpetual-futures-2026-07-24) **[NV]**; un cambio análogo en Binance es un riesgo de régimen para el bot. Modelos de cola de liquidación con funding persistente (AR(1) 0,5–0,7) [MDPI Forecasting 2026](https://www.mdpi.com/2571-9394/8/5/76) **[NV]**.
- **Predicción.** No aparecieron trabajos 2025–2026 con deep learning transversal sobre funding de altcoins que superen modelos tabulares; lo nuevo con respaldo es usar **basis** y **open interest** como features (ya en recolección, `market_feature_snapshots`).

## 7. Riesgos estructurales

1. **ADL**: el 10-oct-2025 los exchanges cerraron por ADL shorts rentables de market makers que cubrían spot largo, dejándolos con spot desnudo en caída [CoinDesk/BitMEX](https://www.coindesk.com/markets/2026/01/08/october-s-crypto-crash-left-market-makers-stuffed-with-coins-slowing-trading-bitmex) **[NV]**; Hyperliquid cerró 2,1 B USD en 12 min [Chitra, 2025](https://arxiv.org/abs/2512.01112). El vigilante (`NAKED_SPOT`, `AUTO_UNWIND_ON_LIQ`) cubre el caso; el riesgo residual es el precio de salida del spot on-chain en estrés.
2. **Contraparte y jurisdicción**: todo el capital en un exchange y una wallet; geobloqueo de la salida de Railway ya ocurrido (proxy VPS). Diversificar exchange no es posible sin perder el edge (Alpha es exclusivo de Binance).
3. **Cambios de reglas**: intervalo/cap de funding, deslistado de Alpha, ratios de colateral.
4. **Muestra**: 29 cierres reales, 6 meses; el universo Alpha simulado tiene sesgo de supervivencia (lista actual).

## 8. Optimizaciones priorizadas

| # | Optimización | Impacto estimado | Esfuerzo | Riesgo | Base |
| --- | --- | --- | --- | --- | --- |
| 1 | **Sizing por liquidez**: tope de nocional por token = tamaño con impacto de 1 vía ≤ ~0,5 % según la cotización del momento (la de `BscSpotLeg` ya existe). El excedente va a la pierna CEX 3x o a Simple Earn. | Alto si el capital > ~10 k USD: evita pasar de 29 % a 14–20 % | Bajo | Bajo | §3 capacidad |
| 2 | **TWAP on-chain BSC**: N tramos espaciados (minutos) reusando el TWAP pareado del CEX, con el perp cubierto por fill. | Alto: sube el techo de capacidad si el pool se re-arbitra entre tramos (a validar con un piloto) | Medio | Medio (más exposición temporal; se mitiga con la cobertura por fill) | §3, §5 |
| 3 | **Registrar el impacto realizado vs cotizado** por trade Alpha (ya se guardan `entry_slippage` y `exit_slippage`) y medir la curva de impacto por token en `market_feature_snapshots` (`alpha_liquidity_usd` ya se recolecta). | Habilita 1–2 con datos propios | Bajo | Nulo | §3 |
| 4 | **Salida/veto por eventos del token**: desbloqueo grande cercano o anuncio de deslistado Alpha → no entrar o salir. | Medio, en la cola (evita funding negativo y spot sin salida) | Medio (fuente de calendario de desbloqueos) | Bajo | §2 |
| 5 | **USDT ocioso → Simple Earn flexible** (margen de reserva fuera del perp y saldo entre trades). | Bajo: +~0,1–0,5 pp/año sobre el capital | Bajo (manual o API Earn) | Bajo (latencia de redención) | §4 |
| 6 | **Termómetro de régimen**: tasa implícita BTC/ETH de Boros, mediana del funding del universo, nº de candidatos con pred > 0. Se muestra y se alerta. | Gestión de riesgo, no rendimiento | Bajo | Nulo | §6, §9 |
| 7 | **Re-probar el modelo con basis/OI** (ene-2027, ≥ 3 meses de señales). | Incierto (+IC en el residuo, pero peor top-5 con 1 semana de datos) | Medio | Sobreajuste | `new_signals_probe_20261025.md` |

**Descartado para el nicho:** Portfolio Margin (los tokens Alpha están en la wallet BSC y no son colateral), cobertura con Boros (no tiene mercados de altcoins), fijar carry con trimestrales (solo majors), leverage > 2x en Alpha (ya estudiado), sizing Kelly o media-varianza (IC y muestra insuficientes frente al riesgo de sobreajuste), reactivar cross perp (`cross_vs_spotperp_20261025.md`).

## 9. Qué monitorear (señales de que el edge muere)

1. **Rendimiento realizado a 90 d < 10 % anual durante dos trimestres seguidos** (sobre capital total) → revisar la tesis. Es el nivel en que el exceso sobre sUSDe/T-bills ya no paga el riesgo operativo.
2. **Oferta del nicho**: tokens Alpha nuevos con perp por mes y candidatos con pred > 0 por scan. Si caen, el nicho se agota. Un cambio de política de Binance Alpha (listados, deslistados masivos) es el riesgo de régimen principal.
3. **Costo de ejecución**: fee Alpha real por trade > 0,8 % del nocional, o impacto realizado sistemáticamente mayor que el cotizado.
4. **Entrada de capital grande en alts**: carry Alpha convergiendo al de majors, vaults/sintéticos que acepten altcoins, o Alpha como colateral en PM.
5. **Reglas de Binance**: cap/intervalo de funding (regla 8.1), fórmula (como KuCoin en jul-2026) y ratios de colateral.
6. **Cola**: frecuencia del caso A de los seguros (spot desnudo on-chain), eventos ADL y alertas `NAKED_SPOT`.

## Referencias

- He, Manela, Ross & von Wachter. *Fundamentals of Perpetual Futures*, arXiv 2212.06888 v7. https://arxiv.org/html/2212.06888v7
- Borri et al. (2025). *Cryptocurrency as an Investable Asset Class: Coming of Age*, arXiv 2510.14435. https://arxiv.org/html/2510.14435v2
- Schmeling, Schrimpf & Todorov (2023). *Crypto carry*, BIS WP 1087. https://www.bis.org/publ/work1087.pdf
- Fan, Jiao, Lu & Tong. *The Risk and Return of Cryptocurrency Carry Trade*, SSRN 4666425 **[NV]**.
- Cao, Luo, Cheng & Dong. *Anatomy of Cryptocurrency Perpetual Futures Returns*, SSRN 6365329 **[NV]**.
- Fayez Junior. *Failure of Cross-Sectional Alpha Screening on Cryptocurrency Perpetual Futures*, SSRN 6701738 **[NV]**.
- OctopusTakopi. *Funding Rates as a Cross-Sectional Factor in Perpetual Futures* (GitHub) **[NV]**.
- Kim & Park (2025). *Designing funding rates for perpetual futures in cryptocurrency markets*, arXiv 2506.08573.
- Chitra (2025). *Autodeleveraging: Impossibilities and Optimization*, arXiv 2512.01112.
- Pendle Boros docs; Chaos Labs; Blockworks (Boros); CoinDesk (BitMEX annual report); Kaiko/Amberdata snapshot ene-2026; Aavescan (sUSDe); Binance FAQ de fees de futuros; CryptoSlate (colateral PM); Trading Strategy (HLP) — enlaces en el texto **[NV]**.
