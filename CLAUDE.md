# CLAUDE.md — Funding Rate Arbitrage Bot

Estado del proyecto al **2026-04-23**, rama activa `claude/optimize-ram-usage-9qlvb`.

---

## Stack

| Capa | Tecnología |
|------|-----------|
| Backend | Python 3 + Flask |
| Servidor | Gunicorn (Procfile) |
| Base de datos | PostgreSQL via SQLAlchemy (flask-sqlalchemy) |
| Migraciones | `core/database.py._run_migrations()` — ALTER TABLE ADD COLUMN IF NOT EXISTS, sin Flask-Migrate |
| Exchange CEX | ccxt (Binance, Bybit, OKX, Bitget) |
| Exchange DeFi | REST APIs propias (Hyperliquid, GMX, Aster, Lighter, Extended) |
| IA / LLM | Groq Llama 3.3 70B (3 API keys rotadas para evitar rate limits) |
| Notificaciones | Telegram Bot API (POST JSON, sin dependencias externas) |
| Cifrado | Fernet simétrico (`core/encryption.py`) para tokens y API keys |
| Frontend | Vanilla JS + CSS3, sin frameworks |

**No hay React, no hay ORM de migraciones, no hay Celery.** Todo el threading es stdlib.

---

## Estructura de directorios

```
app.py                  # Entry point Flask, wire de todos los componentes
config.py               # Variables de entorno → Config object
scanner/
  worker.py             # Monitor de fondo (threading), trigger event-driven
analysis/
  arbitrage.py          # Detección spot-perp y cross-exchange
  scoring.py            # Sistema de puntuación v10.5 (0–100)
  indicators.py         # Momentum, z-score, percentil, régimen
  fees.py               # Estimación fees (CCXT + orderbook + fallback)
  ai_analyzer.py        # Prompts Groq, parse JSON, BUY/HOLD/AVOID
  switch_analyzer.py    # Evalúa conveniencia de cambiar posición
  funding.py            # FundingAggregator: acumulados 3d, APR, ingreso diario
exchanges/
  manager.py            # ExchangeManager CEX (CCXT), historial, spot check
  defi_manager.py       # DefiExchangeManager, historial desde snapshots DB
core/
  db_models.py          # Modelos SQLAlchemy (8 tablas)
  db_persistence.py     # CRUD sobre los modelos
  database.py           # init_db(), _run_migrations()
  state.py              # StateManager thread-safe (datos volátiles del scan)
  encryption.py         # encrypt_value / decrypt_value (Fernet)
  models.py             # Dataclasses: FundingRate, FundingHistory, SpotPerpOpportunity, CrossExchangeOpportunity
  persistence.py        # JSONPersistence (fallback sin DB)
api/
  routes.py             # Todas las rutas Flask (/api/*)
auth/
  routes.py             # Register, login, logout
portfolio/
  manager.py            # Capital summary, abrir/cerrar posiciones, PnL
  actions.py            # Detalles de acción para UI
notifications/
  email.py              # EmailNotifier (nombre legacy) → Telegram Bot API
static/
  app.js                # SPA frontend (~1300 líneas)
  style.css             # Responsive mobile-first
templates/
  index.html            # Tabs: Oportunidades, Posiciones, Config, Cuenta
  login.html            # Login / registro
```

---

## Base de datos — Tablas

| Tabla | Propósito |
|-------|-----------|
| `users` | Cuentas de usuario (email, password_hash) |
| `user_configs` | Config por usuario (capital, thresholds, Telegram encrypted) |
| `user_positions` | Posiciones abiertas con earnings y historial de pagos |
| `user_history` | Posiciones cerradas para PnL histórico |
| `user_exchange_keys` | API keys de exchanges cifradas (infraestructura lista, no conectada a trading) |
| `scan_cache` | Último resultado del scan (opportunities_json, defi_json) |
| `funding_rate_snapshots` | 90 días de tasas históricas, dedup por (symbol, exchange, funding_ts) |
| `score_snapshots` | Evolución del score por oportunidad (ventana rolling 30 entradas) |

Las migraciones se aplican automáticamente al arrancar vía `_run_migrations()`. Para agregar columnas a tablas existentes, añadir `ALTER TABLE ADD COLUMN IF NOT EXISTS` al array `migrations` en `core/database.py`.

---

## Arquitectura del scanner (event-driven, no polling)

El scanner **no usa un intervalo configurable**. Funciona por eventos de pago de funding:

1. `ScannerWorker._monitor_loop()` corre cada 30s — zero llamadas API
2. Compara `next_funding_ts` de posiciones activas contra el tiempo actual
3. Triggers definidos en `scanner/worker.py:22-25`:
   - **Pre-pago** (`PRE_PAYMENT_SCAN_MINS = 10`): verifica que la tasa siga favorable
   - **Post-pago** (`POST_PAYMENT_SCAN_MINS = 1`): refresh de `next_funding_ts`
   - **Force scan**: botón "Escanear" en UI o `POST /api/force`

Esto significa que en periodos sin posiciones abiertas el bot puede estar inactivo.

---

## Sistema de scoring v10.5

Puntaje **0–100**, normalizado. Pesos validados con backtest de 90 días sobre `funding_rate_snapshots`.

| Dimensión | Puntos | Factor más importante |
|-----------|--------|-----------------------|
| Consistencia | 44 | Spearman ρ=0.572 (predictor #1) |
| Estabilidad | 31 | Spearman ρ=0.317 (predictor #2) |
| Yield | 13 | Sweet spot 0.03–0.10%/día |
| Liquidez | 4 | Umbrales por modo (spot_perp > cross > defi) |
| Fee efficiency | 5 | Fee drag < 0.1 = máximo |
| Tendencia | 3 | Momentum + percentil |

**Penalizaciones:** z-score > 0.8 (hasta −28 pts), momentum negativo/decelerado (hasta −8 pts).  
**Hard caps:** z>2.5 → máx 39; streak<3 con percentil≥80 → máx 50; reality penalty → máx 61.  
**Thin history** (<5 muestras): defaults neutros (stability=15, consistency=20) para no penalizar símbolos nuevos/DeFi.

Para **cross_exchange / DeFi**, los indicadores (momentum, z-score, percentile, regime) se calculan sobre `period_diffs` — diferencial por-período emparejado por timestamp — en lugar del `diff_series` diario. Esto garantiza ≥5-15 muestras incluso con pocos días de histórico. Ver `analysis/arbitrage.py:_analyze_differential_history`.

El parámetro `mode` (`spot_perp` / `cross_exchange` / `defi`) ajusta los umbrales de liquidez.

Grades: A ≥85, B ≥70, C ≥55, D <55.

---

## Tipos de oportunidad

| Modo | Estrategia | Archivo clave |
|------|-----------|--------------|
| `spot_perp` | Long spot + Short perp, mismo exchange | `analysis/arbitrage.py:_analyze_spot_perp()` |
| `cross_exchange` | Long perp exchange A + Short perp exchange B | `analysis/arbitrage.py:_analyze_cross_exchange()` |
| `defi` | Short perp en DeFi (sin hedge spot) | `exchanges/defi_manager.py` + cross_exchange path |

El historial DeFi se construye desde `funding_rate_snapshots` (no hay API histórica en DeFi). Ver `exchanges/defi_manager.py:fetch_funding_history()`.

**Posiciones CEX+DeFi**: `api/routes.py:/api/positions` y `/api/positions/ai` combinan `all_data + defi_data` para resolver ambas piernas. `current_fr = short_fr − long_fr` con tasas vigentes de ambos exchanges; `mins_next = min(next_payment_CEX, next_payment_DeFi)`. La actualización de earnings en `_monitor_tick` ya usaba `combined` desde antes.

---

## Notificaciones Telegram

**Cada usuario** gestiona sus propias credenciales (modelo self-service):

1. Crear bot con `@BotFather` → obtener Bot Token
2. Enviar `/start` al bot → obtener Chat ID via `@userinfobot`
3. Pegar ambos en Config del dashboard → "Prueba Telegram"

**Flujo de envío:**
- `scanner/worker.py:_dispatch_alerts_per_user()` — carga credenciales del usuario desde DB, envía, restaura estado
- `scanner/worker.py:_broadcast_alerts_all_users()` — oportunidades excepcionales → todos los usuarios con notificaciones activas
- `notifications/email.py:_send_telegram()` — POST JSON a `api.telegram.org/bot{TOKEN}/sendMessage`

**Tipos de alerta:** `RATE_REVERSAL` (crítica), `RATE_DROP`, `PRE_PAYMENT_UNFAVORABLE`, `SL_TP_REVIEW`, `EXCEPTIONAL_OPPORTUNITY`, `SWITCH_OPPORTUNITY`, `POSITION_CLOSED`.

---

## Config de usuario (campos activos)

| Campo | Efecto real |
|-------|-------------|
| `total_capital` | Valida capital disponible al abrir posición (`portfolio/manager.py:43`) |
| `max_positions` | Bloquea abrir más posiciones (`portfolio/manager.py:68`) |
| `min_volume` | Filtra ambas piernas individualmente en todos los escaneos: spot_perp, cross_exchange CEX, cross DeFi-only y cross CEX+DeFi (`scanner/worker.py`, `analysis/arbitrage.py:scan_cross_exchange_opportunities`) |
| `min_apr` | Filtra oportunidades en `/api/opportunities` |
| `min_score` | Filtra oportunidades en `/api/opportunities` |
| `min_stability_days` | Filtra por `estimated_hold_days` en `/api/opportunities` |
| `alert_minutes_before` | Dispara `PRE_PAYMENT_UNFAVORABLE` N minutos antes del pago |
| `email_enabled` | Master switch de notificaciones Telegram |
| `tg_chat_id` / `tg_bot_token` | Credenciales Telegram (token cifrado con Fernet) |

`scan_interval` existe en la tabla DB pero **no se usa** — el scanner es event-driven.

---

## Estimación de fees (v2)

`analysis/fees.py` calcula entry + exit fees por separado:

1. **CCXT real-time**: carga fees del exchange via `fee_loader.py` (lazy, cacheado)
2. **Orderbook slippage**: VWAP estimado para el tamaño de la posición
3. **Fallback**: tabla hardcoded por exchange si CCXT falla

El usuario puede sobreescribir fees con valores reales una vez que la posición está abierta (campo `entry_fees_real` / `exit_fees_real` en `user_positions`).

---

## IA / Análisis LLM

`analysis/ai_analyzer.py` envía las top 5 oportunidades al scan a Groq (Llama 3.3 70B).

- **Señal:** `BUY` / `HOLD` / `AVOID` + score 0–100 + razonamiento en 40–60 palabras
- **Prompt v10.5:** incluye `mode`, `sym`, indicadores de momentum, z-score, percentil, consistencia, fee drag
- **Rate limiting:** 3 API keys rotadas (`GROQ_API_KEY_1/2/3`)
- El prompt está calibrado para no penalizar dos veces lo que el scoring ya penaliza (z-score, momentum)

---

## Variables de entorno requeridas

```bash
DATABASE_URL          # PostgreSQL URI (Railway: PGSQL_URL)
SECRET_KEY            # Flask session secret
FERNET_KEY            # Cifrado Fernet (generar: python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())")
GROQ_API_KEY_1        # Llama 3.3 70B (gratuito en groq.com)
GROQ_API_KEY_2        # (opcional, rotación)
GROQ_API_KEY_3        # (opcional, rotación)

# CEX API keys (opcional — sin ellas funciona con datos públicos)
BINANCE_API_KEY / BINANCE_API_SECRET
BYBIT_API_KEY / BYBIT_API_SECRET
OKX_API_KEY / OKX_API_SECRET / OKX_PASSPHRASE
BITGET_API_KEY / BITGET_API_SECRET

# Opcionales
COINGLASS_API_KEY     # Datos adicionales de mercado
ENABLED_EXCHANGES     # Default: binance,bybit,okx,bitget
ARBITRAGE_MODES       # Default: spot_perp,cross_exchange
```

---

## Gestión de memoria (proceso long-running en Railway)

El proceso corre indefinidamente — varias estructuras in-memory fueron acotadas para evitar crecimiento continuo:

| Estructura | Archivo | Límite aplicado |
|---|---|---|
| `_notified_alerts` (set) | `scanner/worker.py` | Reset cuando supera 500 entradas (`_cleanup_events`) |
| `_sl_tp_review_sent` (dict) | `scanner/worker.py` | Purga entradas con >24h (`_cleanup_events`) |
| `_switch_results` (dict) | `scanner/worker.py` | Descarta posiciones cerradas al final de `run_switch_analysis` |
| `_sent_cache` (dict) | `notifications/email.py` | Purga entradas con TTL vencido (×10 del cooldown) en cada `send_alert` |
| `_hist_stats_cache` (module dict) | `core/db_persistence.py` | Cap de 300 entradas; elimina la mitad más antigua cuando se supera |

`_scanned_events` ya tenía su propio reset a 200 entradas desde antes.

---

## switch_analyzer.py — notas de implementación

`analysis/switch_analyzer.py` compara la posición actual contra las top 15 oportunidades del último scan. Aspectos importantes para futuras ediciones:

- **Clave de identificación mode-aware**: para `spot_perp` la clave es `symbol_exchange`; para `cross_exchange`/`defi` es `symbol_longExchange_shortExchange`. El helper interno `_opp_key(opp)` construye esta clave. Usar siempre este helper al comparar oportunidades contra la posición actual.
- **Campo de tasa según mode**: `spot_perp` → `funding_rate`; `cross_exchange`/`defi` → `rate_differential`. El mismo patrón aplica a `candidate_risk_factor` para el fallback de `settlement_avg`.
- **`current_market_rate` para cross-exchange**: se recomputa desde las dos piernas (`short_fr − long_fr`) en lugar de buscar por un único campo `"fr"`.
- **`avg_rate` zero-safe**: la comparación usa `if avg_rate is not None and avg_rate != 0` en lugar de `or` para preservar valores legítimamente iguales a cero.
- **Switch analysis es on-demand**: se llama desde `/api/positions/ai`, no desde el loop del monitor.

---

## Pendiente / Lo que falta

### Funcionalidad incompleta (infraestructura lista, falta conectar)

- **Auto-trading**: `user_exchange_keys` almacena API keys cifradas por usuario, la UI tiene skeleton, pero la ejecución automática de órdenes no está implementada. Todo el flujo de abrir/cerrar es manual.
- **Gráfica de earnings**: DOM preparado en templates, Chart.js disponible, la vinculación de datos al canvas no está terminada.
- **Coinglass**: cliente existe en `coinglass/client.py`, no integrado en el flujo principal.

### Mejoras identificadas

- **DeFi history fiabilidad**: `fetch_funding_history` en `defi_manager.py` depende de que los snapshots existan en DB. En deploys nuevos o símbolos nuevos, thin-history (<5 muestras) da scoring neutro — correcto pero subóptimo para toma de decisiones. Nota: GMX, Aster, Lighter y Extended reportan `volume_24h=0`; con el filtro `min_volume` ahora aplicado a cada pierna, estos pares quedan excluidos a menos que el usuario baje `min_volume` a 0.
- **Alertas sin posiciones activas**: el scanner solo corre cuando hay posiciones abiertas o se fuerza manualmente. Si no hay posiciones, las oportunidades excepcionales no se escanean automáticamente.
- **Tests**: no hay suite de tests automatizados. El proyecto depende del syntax check via `python -c "import ast; ast.parse(...)"`.
- **Rate limits DeFi**: los adaptadores DeFi no tienen retry exponencial ante errores HTTP 429/503.
- **Legacy columns**: `user_configs` tiene `wa_phone`, `wa_apikey_encrypted`, `scan_interval` — columnas de versiones anteriores. No se usan, pueden limpiarse con una migración DROP COLUMN cuando sea conveniente.

### Deuda técnica menor

- `notifications/email.py` se llama `EmailNotifier` por compatibilidad histórica (era CallMeBot WhatsApp). Sin urgencia de renombrar ya que el nombre interno no afecta al usuario.
- `scripts/migrate_json_to_db.py` referencia campos legacy (`wa_phone`, `scan_interval`). No afecta producción.
- `portfolio/actions.py` tiene lógica de display parcialmente duplicada con `portfolio/manager.py`.

---

## Comandos útiles

```bash
# Arrancar en desarrollo
python app.py

# Arrancar en producción (Railway usa Procfile)
gunicorn app:app --bind 0.0.0.0:$PORT

# Verificar sintaxis de archivos Python
python -c "import ast, sys; [ast.parse(open(f).read()) for f in sys.argv[1:]]" **/*.py

# Generar FERNET_KEY
python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"

# Force scan manual
curl -X POST http://localhost:5000/api/force
```

---

## Últimos commits relevantes

| Hash | Cambio |
|------|--------|
| `f410ecb` | RAM: acotar caches in-memory; fix switch_analyzer cross-exchange (opp_rate, current_score, market_rate) |
| `f6ae4a7` | Fix min_volume por pierna en DeFi/CEX+DeFi, indicadores cross, current_fr en posiciones |
| `a344317` | Quitar campo `scan_interval` (muerto) del frontend y API |
| `310bbca` | Migrar notificaciones de CallMeBot WhatsApp a Telegram Bot API |
| `7d853dd` | Scoring v10.5: normalizar a 100, mode-aware, historial DeFi desde snapshots |
| `0f5229a` | Fix hora del último scan (epoch → HH:MM:SS local) + auditoría prompt IA |
| `b6a69cc` | Scoring v10.4 + rebalanceo prompt IA (evitar sesgo hacia EVITAR) |
| `d760348` | Estimación de fees realista + edición de fees reales por posición |
| `7ecf2dd` | Fix observabilidad rate snapshots + guards timestamps stale |

---

# CLAUDE.md

Behavioral guidelines to reduce common LLM coding mistakes. Merge with project-specific instructions as needed.

**Tradeoff:** These guidelines bias toward caution over speed. For trivial tasks, use judgment.

## 1. Think Before Coding

**Don't assume. Don't hide confusion. Surface tradeoffs.**

Before implementing:
- State your assumptions explicitly. If uncertain, ask.
- If multiple interpretations exist, present them - don't pick silently.
- If a simpler approach exists, say so. Push back when warranted.
- If something is unclear, stop. Name what's confusing. Ask.

## 2. Simplicity First

**Minimum code that solves the problem. Nothing speculative.**

- No features beyond what was asked.
- No abstractions for single-use code.
- No "flexibility" or "configurability" that wasn't requested.
- No error handling for impossible scenarios.
- If you write 200 lines and it could be 50, rewrite it.

Ask yourself: "Would a senior engineer say this is overcomplicated?" If yes, simplify.

## 3. Surgical Changes

**Touch only what you must. Clean up only your own mess.**

When editing existing code:
- Don't "improve" adjacent code, comments, or formatting.
- Don't refactor things that aren't broken.
- Match existing style, even if you'd do it differently.
- If you notice unrelated dead code, mention it - don't delete it.

When your changes create orphans:
- Remove imports/variables/functions that YOUR changes made unused.
- Don't remove pre-existing dead code unless asked.

The test: Every changed line should trace directly to the user's request.

## 4. Goal-Driven Execution

**Define success criteria. Loop until verified.**

Transform tasks into verifiable goals:
- "Add validation" → "Write tests for invalid inputs, then make them pass"
- "Fix the bug" → "Write a test that reproduces it, then make it pass"
- "Refactor X" → "Ensure tests pass before and after"

For multi-step tasks, state a brief plan:
```
1. [Step] → verify: [check]
2. [Step] → verify: [check]
3. [Step] → verify: [check]
```

Strong success criteria let you loop independently. Weak criteria ("make it work") require constant clarification.

---

**These guidelines are working if:** fewer unnecessary changes in diffs, fewer rewrites due to overcomplication, and clarifying questions come before implementation rather than after mistakes.
