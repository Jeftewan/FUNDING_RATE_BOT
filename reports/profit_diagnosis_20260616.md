# Diagnóstico de scoring vs ganancia neta — 2026-06-16

**Fuente:** `reports/backtest_20260615_features.csv` (823,299 filas) | **Universo:** `positive` (solo funding>0, enterable sin ambiguedad de direccion — vale para todo modo)
**Score:** v10.6 (adoptado 2026-06-11; el CSV es de 2026-06-15 → score actual).
**Net:** `realizado − fee`, realizado direccion-ajustada (sign(rate)×fwd).
**Fee primario:** 0.30% round-trip (= 0.003 fracción). Horizonte primario: 24i (~8d, dentro de 7–15d).

> Recordatorio de unidades: `fwd_total` y `net` están en **puntos porcentuales**.
> Un net de 0.10 = 0.10% de retorno sobre el notional en la ventana.

---

## 1. Rentabilidad neta por horizonte y sensibilidad al fee

¿Qué fracción de oportunidades es rentable neto de fees, según el fee asumido?

| horizonte | bruto_medio | %rent@0.05 | %rent@0.10 | %rent@0.20 | %rent@0.30 |
| --- | --- | --- | --- | --- | --- |
| 8i (~3d) | 0.0222 | 15.7 | 8.1 | 4.5 | 2.7 |
| 24i (~8d) | 0.047 | 46.9 | 37.2 | 13.6 | 8.6 |
| 72i (~24d) | 0.096 | 71.8 | 48.5 | 40.6 | 33.0 |

**Lectura:** si `%rent` sube fuerte con el horizonte, el fee se amortiza con holds más
largos → favorece "ganancia baja sostenida". Si baja el fee, la ventana corta 7–15d
se vuelve viable. Aquí está el cruce que decide corto-alto vs largo-bajo.

---

## 2. Poder predictivo de cada driver vs net a 8/24/72 intervalos

Spearman de cada **driver crudo** (independiente de la versión del scoring) contra el net.
`(inv)` = driver invertido para que correlación positiva signifique "ayuda a ganar".

| dimension | peso_v106 | driver | sp_net8 | sp_net24 | sp_net72 | pe_net24 |
| --- | --- | --- | --- | --- | --- | --- |
| consistency | 46 | streak | 0.339 | 0.398 | 0.41 | 0.181 |
| consistency | 46 | pct_positive | 0.353 | 0.422 | 0.441 | 0.194 |
| stability | 21 | cv(inv) | -0.001 | 0.055 | 0.087 | 0.045 |
| stability | 21 | min_ratio | 0.13 | 0.182 | 0.2 | 0.036 |
| yield | 17 | settlement_avg | 0.365 | 0.288 | 0.229 | -0.021 |
| yield | 17 | abs_current_rate | 0.533 | 0.457 | 0.388 | 0.178 |
| fee | 8 | fee_drag(inv) | 0.21 | 0.141 | 0.09 | 0.028 |
| liquidity | 6 | volume | 0.112 | 0.072 | 0.042 | 0.002 |
| trend | 1 | mom_roc | 0.033 | 0.03 | 0.027 | -0.001 |
| trend | 1 | percentile | -0.029 | -0.076 | -0.095 | 0.142 |

**Tesis a validar:** los drivers de **consistencia** (streak, pct_positive, peso 46) y
**estabilidad** (cv, min_ratio, peso 21) deberían rankear BAJO en `sp_net24`, mientras
**yield** (settlement_avg, peso 17) rankea ALTO. Eso probaría el sobre-premio.

---

## 3. Brecha peso↔poder predictivo (horizonte ~8d, fee 0.30%)

Compara cuánto PESO da v10.6 a cada dimensión vs cuánto PREDICE realmente la ganancia.

| dimension | peso_v106 | peso_% | mejor_driver | sp_vs_net24 | sesgo |
| --- | --- | --- | --- | --- | --- |
| consistency | 46 | 46.5 | pct_positive | 0.422 | — |
| stability | 21 | 21.2 | min_ratio | 0.182 | — |
| yield | 17 | 17.2 | abs_current_rate | 0.457 | SUB-premiado |
| fee | 8 | 8.1 | fee_drag | 0.141 | — |
| liquidity | 6 | 6.1 | volume | 0.072 | — |
| trend | 1 | 1.0 | percentile | -0.076 | — |

`SOBRE-premiado` = ≥20% del score pero |Spearman| < 0.10 vs net a 8d.
`SUB-premiado` = <20% del score pero |Spearman| ≥ 0.15.

---

## 4. Corto-alto vs largo-bajo (neto de fees)

Responde directo a tu pregunta: ¿conviene yield alto en ventana corta, o estable y bajo
sostenido un mes?

| grupo | horizonte | n | mean_net | median_net | %rent | sharpe |
| --- | --- | --- | --- | --- | --- | --- |
| corto-alto (yield top 20%) | 24i (~8d) | 164664 | -0.1992 | -0.18 | 32.9 | -0.185 |
| corto-alto (yield top 20%) | 72i (~24d) | 164664 | -0.1113 | 0.0118 | 50.5 | -0.041 |
| largo-bajo (estable, yield bajo) | 24i (~8d) | 259957 | -0.2517 | -0.27 | 0.8 | -1.089 |
| largo-bajo (estable, yield bajo) | 72i (~24d) | 259957 | -0.1781 | -0.21 | 27.9 | -0.235 |

---

## 5. ¿El score v10.6 rankea bien la ganancia neta? (monotonicidad por decil)

Decil 1 = peor score, decil 10 = mejor. Si el score predice ganancia, `mean_net` debe
crecer con el decil.

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

- **Spearman(decil, mean_net):** `0.673` (1.0 = monotonicidad perfecta).
- **Spearman(score, net_24) a nivel fila:** `0.360`.
- **Net del decil 10 vs decil 1:** `-0.2228` vs `-0.4666`.

---

## Veredicto rápido

- Monotonicidad score↔net a 8d: **DÉBIL** (ρ=0.67).
- Correlación fila score↔net: **0.360** (referencia: el optimizer apunta a maximizar esto).
- Revisar la tabla §3: toda dimensión marcada `SOBRE-premiado` es candidata a bajar peso
  en la Etapa 2 (re-objetivo Sharpe), y toda `SUB-premiado` a subir.
