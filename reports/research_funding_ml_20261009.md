# Revisión de literatura: predictibilidad de funding rates y validación del modelo ML de ranking

**Fecha:** 2026-10-09 · **Alcance:** modelo HistGradientBoostingRegressor que rankea oportunidades `spot_perp` (target `net_apr` oráculo, 14 features, walk-forward sin purging). · **Método:** búsqueda web estándar + lectura de abstracts/HTML disponibles. Lo no verificado en el texto original se marca con **[NV]**.

---

## Resumen ejecutivo

- **El edge bruto existe pero se está comprimiendo.** La literatura documenta carry alto y Sharpe elevados en 2020–2023, pero con caída sostenida: las desviaciones perp–spot disminuyen ~11 %/año en Binance [He et al., 2022/2024](https://arxiv.org/html/2212.06888v5) y el Sharpe del carry BTC pasa de 6,45 (2020–2025) a 4,06 desde 2024 y a negativo en 2025 [Borri et al., 2025](https://arxiv.org/html/2510.14435). Un modelo de ranking no crea carry; solo selecciona dentro de un pool cuyo retorno medio tiende a cero.
- **La evidencia directa sobre ML para funding es escasa y desfavorable.** El único estudio aplicado encontrado reporta que Ridge y XGBoost no superan a una cartera estática equiponderada (efecto despreciable, Cohen's d < 0,2) por la reversión a la media de las tasas [Rittiplang & Khemapatapan, 2026](https://ph03.tci-thaijo.org/index.php/JSTKU/article/view/4724) **[NV: solo abstract]**.
- **La persistencia que el modelo explota es real pero mayoritariamente "de baseline".** En Q3-2025 la tasa estuvo exactamente en 0,01 % el 78–88 % del tiempo en BitMEX y los extremos son de corta vida [BitMEX, 2025](https://www.dlnews.com/external/bitmex-study-finds-cryptocurrency-funding-rates-positive-92-of-the-time-revealing-a-structural-market-bias) (fuente interesada). `streak` y `pct_positive` (64 % de la importancia) capturan esa persistencia; el rank-IC alto es en parte mecánico y no implica ventaja neta.
- **El carry alto es señal de riesgo, no solo de oportunidad.** Carry elevado precede crashes y coincide con encarecimiento del seguro contra caídas [Schmeling, Schrimpf & Todorov, 2023](https://www.bis.org/publications/working-paper-1087-crypto-carry). El modelo, al premiar yield alto, se carga sistemáticamente hacia ese riesgo.
- **Riesgos de cola no capturados por el label:** liquidación de la pierna short en rallies, slippage en cascadas [Sepper, 2026](https://arxiv.org/abs/2603.09164) y auto-deleveraging (ADL), que en un crash cierra precisamente la pierna ganadora (short perp) y deja el spot descubierto [Chitra, 2025](https://arxiv.org/abs/2512.01112).
- **Sobreestimación #1 — fuga por solapamiento de labels.** Sin purging, las últimas ~14 días de cada train contienen labels que observan el periodo de test; López de Prado prescribe purgar ≥ horizonte del label más embargo [López de Prado, 2018](https://fynance.readthedocs.io/en/latest/features.labels.html) **[NV: libro no consultado directamente]**.
- **Sobreestimación #2 — sesgo de selección.** Elegir 1 de 10 variantes sobre los mismos folds infla la métrica reportada; corresponde deflactar (DSR) y estimar PBO [Bailey & López de Prado, 2014](https://papers.ssrn.com/abstract=2460551); [Bailey et al., 2017](https://papers.ssrn.com/abstract=2326253).
- **Sobreestimación #3 — label oráculo y sin costos de basis.** La salida "sabe" cuándo llega el primer pago ≤ 0 y omite basis/slippage, que según He et al. explican la mayor parte del retorno de la estrategia de arbitraje; el `net_apr` es una cota superior, no un retorno realizable.
- **Señal de alerta empírica:** IC backtest ≈ 0,75 vs. IC en vivo 0,647 (−14 %) es consistente con fuga + selección. Además, el IC en vivo usa el mismo label oráculo, por lo que tampoco mide PnL realizable.
- **Conclusión:** la literatura no respalda que un ranker de este tipo sostenga ventaja **neta** por sí solo; respalda, como mucho, que mejora la selección relativa mientras el carry agregado sea positivo. La prioridad es medir correctamente (label realista, purging, deflactado) antes de seguir iterando el modelo.

---

## 1. Predictibilidad y persistencia de funding rates

**Mecánica y anclaje.** El funding transfiere de longs a shorts cuando el perp cotiza sobre spot; no hay vencimiento que fuerce convergencia. Las desviaciones respecto del precio teórico son mayores que en FX, comueven entre monedas y, en palabras de los autores, "diminish over time" [He et al., 2022/2024](https://arxiv.org/abs/2212.06888). En la versión v5 (Binance, 2020–mar-2024, cinco monedas) la desviación absoluta media cae ~11 %/año (>44 % en la muestra); la estrategia de arbitraje logra Sharpe 1,8–4,8 con costos altos, pero la convergencia de precio aporta la mayor parte del retorno y el componente funding es menor y decreciente. Los retornos pasados de 120 días predicen la brecha futures–spot (R² 0,09–0,33) [He et al., v5](https://arxiv.org/html/2212.06888v5) **[NV: lectura parcial, ~75 % del HTML]**. La fórmula de Binance (premium index promediado + interés con clamp) genera un atractor mecánico en 0,01 %/8h.

**Precio teórico.** El precio del perp equivale a la esperanza risk-neutral del spot muestreado en un tiempo aleatorio cuya intensidad depende del anclaje del funding; existen especificaciones de funding bajo las cuales perp y spot coinciden y el contrato es replicable [Ackerer, Hugonnier & Jermann, 2023/2024](https://arxiv.org/abs/2310.11771). El diseño óptimo del funding vía BSDE se aborda en [Kim & Park, 2025](https://arxiv.org/abs/2506.08573v1). Implicación: el funding es un proceso controlado por la diferencia de precios; su predictor natural es el premium/basis corriente, no solo su propia historia.

**Heterocedasticidad.** Las tasas de BitMEX son heterocedásticas y modelables con GARCH, con causalidad de Granger hacia el contrato [Nimmagadda & Ammanamanchi, 2019](https://arxiv.org/abs/1912.03270). No se hallaron trabajos revisados por pares de 2023–2026 que pronostiquen funding con ML de forma rigurosa (búsqueda negativa, no prueba de inexistencia).

**Magnitud, regímenes y decaimiento del edge.** El carry cripto llegó a ~60 % anual, promedió >10 % (≈7 % abr-2019/jul-2024 según otra medición citada) y apenas se explica por diferenciales de tasas; lo impulsan demanda apalancada minorista y escasez de capital de arbitraje [Schmeling et al., 2023](https://www.bis.org/publ/work1087.pdf) **[NV: cifras de regresión no leídas]**. Christin et al. reportan Sharpe in-sample de 7–10 para el carry trade en 2020–2022 [Christin et al., 2022](https://www.andrew.cmu.edu/user/azj/files/CarryTrade.v1.0.pdf) **[NV: borrador]**. Desde 2024 la compresión es visible: Sharpe 4,06 y negativo en 2025 [Borri et al., 2025](https://arxiv.org/html/2510.14435); prensa con datos de Amberdata ubica el basis anualizado ~4,7–5 % a inicios de 2026, por debajo del rendimiento de Treasuries, por el ingreso de capital vía ETF [Advisor Perspectives/Bloomberg, 2026](https://www.advisorperspectives.com/articles/2026/01/22/wall-street-pulls-back-bitcoin-trade) **[NV: prensa]**. BitMEX reporta caída de ~90 % en funding extremo desde 2016 [BitMEX/DL News](https://www.dlnews.com/external/bitmex-study-reveals-90-drop-in-extreme-bitcoin-perpetual-futures-funding-rates-since-2016-signalling-market-maturation) **[NV: fuente interesada]**.

**ML vs. estático.** En Binance (8 activos, 4 años), Ridge y XGBoost con walk-forward no baten a la estrategia estática equiponderada; el método propuesto (MVO event-driven) reporta Sharpe 30,5 y 43/43 meses positivos [Rittiplang & Khemapatapan, 2026](https://ph03.tci-thaijo.org/index.php/JSTKU/article/view/4724) **[NV: solo abstract; un Sharpe de 30 sugiere omisión de costos de basis o riesgos de cola]**.

**Riesgos de basis, liquidación, exchange y capacidad.**
- Margen y liquidaciones se disparan en drawdowns; el carry alto predice crashes [Schmeling et al., 2023](https://www.bis.org/publications/working-paper-1087-crypto-carry).
- En estrés, las cascadas de liquidación agotan la liquidez y ejecutan muy por debajo del precio teórico; el Slippage-at-Risk basado en microestructura del libro actúa como indicador adelantado [Sepper, 2026](https://arxiv.org/abs/2603.09164).
- El ADL del 10-oct-2025 en Hyperliquid cerró ~US$2,1 mil millones en 12 minutos; ninguna política de ADL satisface a la vez solvencia, ingresos y equidad [Chitra, 2025](https://arxiv.org/abs/2512.01112). Las carteras mejor cubiertas deberían desapalancarse menos bajo una política óptima, pero las implementadas no lo garantizan [Campbell et al., 2026](https://arxiv.org/pdf/2603.15963).
- Los perps reducen dislocaciones extremas y los drawdowns de estrategias de arbitraje frente a futuros trimestrales [Gornall, Rinaldi & Xiao, 2024/2025](https://www.ecgi.global/funding-payments-crisis-proofed-bitcoins-perpetual-futures) **[NV: working paper preliminar]**.
- Algunos exchanges reportan mal el open interest y retrasan mensajes de liquidación [Giagkozis et al., 2023](https://arxiv.org/abs/2310.14973v1): riesgo de dato y de contraparte.

---

## 2. Metodología de validación para ML financiero

**Solapamiento de labels, purging y embargo.** Labels con horizonte de varios periodos comparten ventana futura, no son i.i.d. y contaminan cualquier partición temporal; el remedio es purgar del train toda observación cuyo intervalo de label intersecte el test, añadir embargo y ponderar por unicidad media del label [López de Prado, 2018 — resumen en fynance](https://fynance.readthedocs.io/en/latest/features.labels.html); [Wikipedia: Purged CV](https://en.wikipedia.org/wiki/Purged_cross-validation) **[NV: libro]**. En el diseño actual (test 7 d, label 14 d) el train contiene hasta 14 días de labels que observan el test.

**CPCV y PBO.** Un único camino walk-forward da una sola estimación sin varianza de partición; CPCV genera múltiples caminos OOS y permite estimar la distribución de la métrica [fynance CPCV](https://fynance.readthedocs.io/en/latest/generated/fynance.data.combinatorial_purged_cv.html). El PBO, estimado con CSCV, mide la probabilidad de que la configuración óptima in-sample quede bajo la mediana OOS; el hold-out simple es poco fiable en backtests [Bailey, Borwein, López de Prado & Zhu, 2017](https://papers.ssrn.com/abstract=2326253). Un trabajo de 2024 atribuido a Arian et al. favorece CPCV frente a walk-forward en entorno sintético [citado en arXiv 2512.12924](https://arxiv.org/pdf/2512.12924) **[NV: original no localizado]**.

**Deflactado por selección.** El Deflated Sharpe Ratio corrige por número de pruebas y no-normalidad [Bailey & López de Prado, 2014](https://papers.ssrn.com/abstract=2460551); el haircut de Harvey & Liu penaliza más a los Sharpe marginales que a los muy altos [Harvey & Liu, 2015](https://people.duke.edu/~charvey/Research/Published_Papers/P120_Backtesting.PDF). Un protocolo de backtesting para la era ML exige registrar todas las variantes probadas [Arnott, Harvey & Markowitz, 2019](https://papers.ssrn.com/abstract=3275654). Con 10 variantes, el ganador está sesgado al alza aunque todas fueran equivalentes.

**Label oráculo vs. triple barrera.** La triple barrera etiqueta según qué barrera (ganancia, pérdida, tiempo) se toca primero siguiendo la trayectoria, imitando una regla de salida ejecutable [López de Prado, 2018 — resumen](https://www.luxalgo.com/library/concept/label-definition-and-prediction-horizon.md) **[NV: libro]**. El `net_apr` actual corta en el primer pago ≤ 0, información que en vivo no se tiene al decidir; además, el divisor "días hold" se acorta exactamente cuando el resultado es malo, amplificando la varianza del target.

**Rank-IC y su significancia.** El IC medio de un panel tiene observaciones dependientes: (i) solapamiento temporal de 14 d → autocorrelación de la serie de IC por fecha (usar errores Newey-West/HAC o bloques no solapados); (ii) comovimiento transversal de tasas [He et al.](https://arxiv.org/abs/2212.06888) → N efectivo menor que el número de pares **[NV: Newey-West no consultado; práctica estándar]**. Un IC de 0,75 es atípicamente alto para finanzas y sugiere que parte de la relación es mecánica (`fee_drag_det` y `settlement_avg` comparten componentes con el target). La métrica de decisión debe ser económica: retorno neto realizable del top-k con costos reales.

**Drift en producción.** El PSI es el estándar bancario para detectar desplazamiento de distribuciones, pero sus umbrales fijos (0,1/0,25) carecen de control de error tipo I/II; se recomiendan valores críticos dependientes del tamaño muestral [Yurdakul & Naranjo, s. f.](https://ideas.repec.org/a/rsk/journ5/7725371.html); [Yurdakul, 2018](https://scholarworks.wmich.edu/dissertations/3208/).

---

## 3. Features adicionales con respaldo

| Feature | Respaldo | Mecanismo |
|---|---|---|
| Premium index / basis mark–index corriente y su TWAP intra-periodo | Fórmula Binance en [He et al.](https://arxiv.org/html/2212.06888v5); [Ackerer et al.](https://arxiv.org/abs/2310.11771) | El próximo funding es casi función del premium promediado; además el basis de entrada/salida es costo directo. |
| Retorno pasado 120 d / momentum de precio | [He et al., v5](https://arxiv.org/html/2212.06888v5) | Predice la brecha futures–spot (R² 0,09–0,33). |
| Nivel de carry agregado del mercado | [Schmeling et al., 2023](https://www.bis.org/publ/work1087.pdf) | Carry alto precede crashes → feature de riesgo/régimen, no de retorno. |
| Open interest y su variación | [Giagkozis et al., 2023](https://arxiv.org/abs/2310.14973v1) | Proxy de apalancamiento/crowding; usar con cautela por mala cotización en algunos venues. |
| Dispersión de funding entre exchanges | [He et al.](https://arxiv.org/abs/2212.06888) (tasas similares salvo estrés) | Divergencia = estrés de liquidez o capital de arbitraje escaso. |
| Volatilidad realizada del subyacente | [Schmeling et al., 2023](https://www.bis.org/publications/working-paper-1087-crypto-carry) | Spikes de margen y probabilidad de liquidación de la pierna short. |
| Profundidad/slippage del libro (SaR) y concentración de market makers | [Sepper, 2026](https://arxiv.org/abs/2603.09164) | Costo de ejecución y de unwind en cola. |

El aporte de OI y volatilidad a la **predicción de funding** no está probado en literatura revisada encontrada; su respaldo es como variables de riesgo.

---

## Implicación → cambio concreto en el bot

| Prioridad | Área | Implicación | Cambio concreto |
|---|---|---|---|
| **Alta** | Validación | Fuga por labels de 14 d sin purging | En `scripts/ml_train.py`: purgar del train las muestras con `t > test_start − 14d` y embargo ≥ 1 d; reportar IC con y sin purging. |
| **Alta** | Label | `net_apr` oráculo y sin basis/slippage | Label "política-consistente": salir con la misma regla que usa el bot (p. ej. tras observar 1 pago ≤ 0 o alerta PRE), restar basis de entrada/salida histórico y slippage estimado; horizonte fijo (14 d) en el divisor para no premiar holds cortos. |
| **Alta** | Validación | Selección de 1 de 10 variantes | Registrar todas las variantes; calcular DSR del top-decil y PBO vía CSCV/CPCV; reservar un hold-out final intocado (últimos 21 d) antes de promover. |
| **Alta** | Riesgo | Carry alto precede crashes; ADL elimina la pierna short | Feature/regla de régimen con carry agregado y volatilidad; tope de exposición total cuando el carry de mercado está en percentil alto; monitor de ADL/insurance fund y alerta si la pierna perp se cierra sin la spot. |
| **Alta** | Métrica | IC alto parcialmente mecánico y sin significancia corregida | Métrica de promoción = PnL neto realizable del top-k (con costos reales) con IC por fecha y error HAC/bloques; añadir IC de "solo baseline" (`settlement_avg`) como benchmark mínimo. |
| **Media** | Features | Funding determinado por premium/basis | Añadir premium index TWAP del periodo en curso, basis mark–index, retorno 120 d, OI Δ, dispersión cross-exchange; verificar paridad train/inferencia (requiere histórico en `funding_rate_snapshots`). |
| **Media** | Calibración | Percentiles in-sample inflan el score | Calibrar el score 0–100 con predicciones OOS de los folds (no in-sample). |
| **Media** | Drift | Edge decreciente (He et al., Borri et al.) | PSI por feature con valores críticos según N (Yurdakul & Naranjo) + IC en vivo rodante 14 d; degradar al heurístico si IC en vivo < umbral dos ventanas seguidas. |
| **Media** | Benchmark | ML no bate estático en literatura | Comparar contra cartera equiponderada de todos los pares con funding > fee y contra "top por `settlement_avg`"; si el uplift neto no es significativo, simplificar. |
| **Baja** | Riesgo | Liquidación/slippage en cascadas | Buffer de margen mínimo dinámico según volatilidad y SaR del libro; tope de tamaño por profundidad. |
| **Baja** | Datos | OI mal reportado en algunos venues | Validar OI cruzando volumen antes de usarlo como feature. |
| **Baja** | Scope | cross_exchange/DeFi sin evidencia académica | Mantener heurístico; no extender el modelo sin label con costos de ambas piernas. |

---

## Referencias

1. He, S., Manela, A., Ross, O., von Wachter, V. (2022, rev. 2024–2026). *Fundamentals of Perpetual Futures*. https://arxiv.org/abs/2212.06888 · v5: https://arxiv.org/html/2212.06888v5 — abstract y ~75 % del texto leído.
2. Schmeling, M., Schrimpf, A., Todorov, K. (2023). *Crypto carry*. BIS WP 1087. https://www.bis.org/publications/working-paper-1087-crypto-carry · PDF: https://www.bis.org/publ/work1087.pdf — **[NV: regresiones de crash no leídas]**.
3. Rittiplang, C., Khemapatapan, C. (2026). Event-Driven Portfolio Management Algorithm for Funding Rate Arbitrage. *JSTKU* 15(2). https://ph03.tci-thaijo.org/index.php/JSTKU/article/view/4724 — **[NV: solo abstract]**.
4. Ackerer, D., Hugonnier, J., Jermann, U. (2023, rev. 2024). *Perpetual Futures Pricing*. https://arxiv.org/abs/2310.11771
5. Kim, J., Park, H. (2025). *Designing funding rates for perpetual futures*. https://arxiv.org/abs/2506.08573v1 — solo abstract vía búsqueda.
6. Nimmagadda, S. S., Ammanamanchi, P. S. (2019). *BitMEX Funding Correlation with Bitcoin Exchange Rate*. https://arxiv.org/abs/1912.03270
7. Borri, N., Liu, Y., Tsyvinski, A., Wu, X. (2025, rev. 2026). *Cryptocurrency as an Investable Asset Class: Coming of Age*. https://arxiv.org/html/2510.14435
8. Christin, N., Routledge, B., Soska, K., Zetlin-Jones, A. (2022). *The Crypto Carry Trade* (borrador v1.0). https://www.andrew.cmu.edu/user/azj/files/CarryTrade.v1.0.pdf — **[NV]**.
9. Gornall, W., Rinaldi, M., Xiao, Y. (2024/2025). *Funding Payments Crisis-Proofed Bitcoin's Perpetual Futures* / *Perpetual Futures and Basis Risk*. https://www.ecgi.global/funding-payments-crisis-proofed-bitcoins-perpetual-futures — **[NV: preliminar]**.
10. Sepper, O. (2026). *Slippage-at-Risk*. https://arxiv.org/abs/2603.09164
11. Chitra, T. (2025). *Autodeleveraging: Impossibilities and Optimization*. https://arxiv.org/abs/2512.01112
12. Campbell, Hey, Moallemi, Nutz (2026). *Risk-Based Auto-Deleveraging*. https://arxiv.org/pdf/2603.15963 — **[NV: solo resumen de búsqueda]**.
13. Giagkozis et al. (2023). *Reconciling Open Interest with Traded Volume in Perpetual Swaps*. https://arxiv.org/abs/2310.14973v1 — **[NV: solo resumen]**.
14. BitMEX (2025). Q3-2025 derivatives report (vía DL News). https://www.dlnews.com/external/bitmex-study-finds-cryptocurrency-funding-rates-positive-92-of-the-time-revealing-a-structural-market-bias — fuente interesada.
15. Advisor Perspectives / Bloomberg (2026). *Wall Street pulls back from a money-spinning bitcoin trade*. https://www.advisorperspectives.com/articles/2026/01/22/wall-street-pulls-back-bitcoin-trade — **[NV: prensa]**.
16. Bailey, D., Borwein, J., López de Prado, M., Zhu, Q. (2017). *The Probability of Backtest Overfitting*. J. Computational Finance. https://papers.ssrn.com/abstract=2326253
17. Bailey, D., López de Prado, M. (2014). *The Deflated Sharpe Ratio*. JPM 40(5). https://papers.ssrn.com/abstract=2460551
18. Harvey, C., Liu, Y. (2015). *Backtesting*. JPM. https://people.duke.edu/~charvey/Research/Published_Papers/P120_Backtesting.PDF
19. Arnott, R., Harvey, C., Markowitz, H. (2019). *A Backtesting Protocol in the Era of Machine Learning*. https://papers.ssrn.com/abstract=3275654
20. López de Prado, M. (2018). *Advances in Financial Machine Learning*. Wiley (caps. 3–4, 7). Resúmenes: https://fynance.readthedocs.io/en/latest/features.labels.html · https://en.wikipedia.org/wiki/Purged_cross-validation — **[NV: libro no consultado]**.
21. Arian et al. (2024). Comparación CPCV vs. walk-forward (citado en https://arxiv.org/pdf/2512.12924) — **[NV: original no localizado]**.
22. Yurdakul, B., Naranjo, J. *Statistical properties of the population stability index*. J. Risk Model Validation. https://ideas.repec.org/a/rsk/journ5/7725371.html · tesis 2018: https://scholarworks.wmich.edu/dissertations/3208/
23. Newey, W., West, K. (1987). Errores HAC — **[NV: referencia estándar no consultada]**.
