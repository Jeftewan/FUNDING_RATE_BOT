# Diagnóstico de rentabilidad del negocio — 2026-10-09

**Fuente:** `user_history` (posiciones cerradas) y `user_positions` (activas), en lectura sobre la DB de producción. También se usa el OOS del modelo (`reports/ml_health_20261009.md`).
**Universo:** 2 usuarios operativos (ids 2 y 4) con capital configurado de 1.900 y 3.300 USD (total 5.200 USD).
**Exclusiones:** 3 posiciones de prueba de auto-ejecución de 18 USD (net −0,13 USD).

## 1. Resultado realizado

| Métrica | Valor |
|---|---|
| Posiciones cerradas reales | 23 (22 spot_perp, 1 cross_exchange) |
| Ingreso bruto por funding | 564,04 USD |
| Fees | 76,02 USD (13,5 % del bruto) |
| **Ganancia neta realizada** | **488,02 USD** |
| Posiciones ganadoras | 87 % (20/23) |
| Hold mediano | 7,2 días |
| Capital medio por posición | 1.679 USD |
| **APR neto sobre capital desplegado** | **39,2 %** (bruto 45,3 %) |
| Fee round-trip efectivo | ≈0,20 % del capital (el modelo asume 0,30 %, conservador) |
| Posiciones activas (2) | 5.016 USD, earned 81,65 USD. Neto estimado tras fees de entrada y salida: ≈67 USD |

**Ganancia neta por mes de cierre (USD):** may 27,33 · jun 99,53 · jul 160,86 · ago 82,91 · sep 117,40 · oct (solo abiertas) ≈67 no realizado.

## 2. Retorno sobre el capital total (lo que importa al negocio)

| Usuario | Periodo | Capital | Neto (real + no realizado) | Retorno | **Anualizado** | Utilización capital·tiempo | Días con posición |
|---|---|---|---|---|---|---|---|
| 2 | 2026-04-20 → hoy (172 d) | 1.900 | 307,5 | 16,2 % | **34,3 %** | 73 % | 90 % |
| 4 | 2026-06-16 → hoy (115 d) | 3.300 | 247,8 | 7,5 % | **23,8 %** | 80 % | 86 % |
| **Conjunto** | ponderado capital·tiempo | 5.200 | 555,3 | — | **≈28,7 %** | ≈77 % | — |

En términos absolutos, con ~5.200 USD el negocio genera **≈100–160 USD/mes**. Esto equivale a 1.200–1.900 USD/año, unos 2.400–3.400 USD/año por cada 10.000 USD si la rentabilidad escala linealmente, lo cual no está garantizado (ver §5).

Como referencia, el rendimiento sin riesgo en USD (T-bills) ronda el 4 % (aproximado). El basis promedio del mercado a inicios de 2026 está en ≈5 % anual según `reports/research_funding_ml_20261009.md`. El bot rinde **5–7× ese nivel** sobre capital total.

## 3. ¿Aporta el modelo? (comparación antes y después del deploy ML, 2026-06-17)

| Cohorte (por fecha de apertura) | n | Neto USD | Win | APR neto desplegado |
|---|---|---|---|---|
| Pre-modelo (heurístico v10.x/v11) | 8 | 60,81 | 75 % | 26,8 % |
| Era modelo ML | 15 | 427,22 | 93 % | **42,0 %** |

La mejora es consistente con el OOS del modelo:
- En la ventana purgada, el top-10 % por modelo rinde 13,8 % APR neto, contra 10,4 % del heurístico, con label realista.
- La media del universo es −26,6 %, así que **la selección es todo el negocio**: entrar "a cualquiera" pierde dinero.

**No es causal:** n es pequeño, el régimen de mercado y el tamaño de posición cambiaron, y el usuario elige manualmente entre las sugeridas. `user_history` no guarda el score ni la predicción de entrada, así que no se puede atribuir el PnL al modelo frente al heurístico.

## 4. Unit economics por operación

- Neto medio: 21,2 USD por posición (mediana de hold 7,2 d, capital 1.679 USD).
- El fee pesa entre 4 % y 103 % del bruto según el hold. En holds de menos de 5 días se come entre el 40 % y el 100 % (BSB, SLX, IN, VELVET). **Los holds largos (>10 d) son donde está la plata:** ESPORTS 26,9 d → 66 USD; STAR 26,9 d → 51 USD; SIREN 13 d → 42 USD.
- Hay outliers que sesgan la media: EVAA rindió 358 % APR en 4,5 d (70 USD, el 14 % de la ganancia total).

## 5. Riesgos y límites que el PnL registrado NO refleja

1. **PnL de precio y basis no registrado.** `user_history` guarda funding y fees, pero no la diferencia de precio spot-perp al entrar y salir. El neto real puede diferir en ambos sentidos.
2. **Capacidad.** Las ganancias vienen de altcoins de baja capitalización (ARIA, SIREN, ESPORTS, EVAA, BULLA) con posiciones de 1,5–3 kUSD. Escalar a decenas de miles aumenta slippage y movería el funding. La rentabilidad **no escala linealmente**.
3. **Compresión del carry.** En el OOS, el top-10 % pasó de ≈25 % a ≈14 % APR neto entre la segunda quincena de septiembre y la primera semana de octubre. La literatura documenta una caída estructural del carry desde 2024.
4. **Riesgo de cola.**
   - ADL o liquidación de la pierna short en rallies (casi todo es leverage 1, salvo GOOGL ×4).
   - Riesgo de contraparte del exchange.
   - Un funding alto precede a crashes (BIS WP 1087).
5. **Capital ocioso.** La utilización es del 73–80 %. Del 13-sep al 22-sep no hubo posiciones: ≈8 días × 5.200 USD × ~30 % APR ≈ **34 USD no ganados**. Además, el scanner es event-driven y **dejó de recolectar datos** en ese periodo. Eso degrada el entrenamiento y no detecta oportunidades.
6. **Muestra pequeña.** 23 operaciones de 2 usuarios en ~5 meses. El intervalo de confianza del APR es amplio.

## 6. Conclusión

- **El negocio es rentable a la escala actual:** ≈29 % anualizado sobre capital total, ≈39 % sobre capital desplegado y 87 % de operaciones ganadoras. Está muy por encima de la tasa libre de riesgo.
- La ventaja viene de **seleccionar bien**, porque el universo promedio pierde, y de **sostener holds largos**, porque el fee castiga los cortos.

**Palancas de mejora, en orden de impacto estimado:**
1. **Subir la utilización del capital** del 77 % hacia el 95 %: rotar sin dejar días sin posición. El scanner debería correr aunque no haya posiciones abiertas.
2. **Evitar holds menores de 5 días,** salvo que el APR esperado cubra el fee con holgura.
3. **Registrar en cada posición la predicción y el score de entrada y el basis de entrada y salida.** Permite medir el PnL real atribuible al modelo.
4. **Validar la capacidad antes de escalar capital:** profundidad del orderbook en los pares típicos.
