# CLAUDE.md — Funding Rate Arbitrage Bot

Estado del proyecto al **2026-04-28**, rama activa `claude/integrate-landing-auth-QfCwW`.

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
| Frontend (dashboard) | Vanilla JS + CSS3, sin frameworks (`static/app.js`, `static/style.css`) |
| Frontend (landing) | React + Vite — compilado en `static/landing/assets/`. Source original en [`Jeftewan/basyo`](https://github.com/Jeftewan/basyo) (Lovable, **deprecado**). Editar via recompilación en este repo. |

**El dashboard SPA no usa React.** Todo el threading del backend es stdlib. La landing es un bundle Vite estático servido por Flask — el contenido visible vive en `static/landing/assets/index-*.js`, no en `templates/landing.html`.

---

## Estructura de directorios

```
app.py                  # Entry point Flask, wire de todos los componentes
config.py               # Variables de entorno → Config object
scanner/
  worker.py             # Monitor de fondo (threading), trigger event-driven
analysis/
  arbitrage.py          # Detección spot-perp y cross-exchange
  scoring.py            # Heurístico v11.0 (0–100) — fallback del modelo ML
  ml_features.py        # Feature-builder compartido (paridad train/inferencia)
  ml_scorer.py          # Prod: carga el .joblib y rankea (modelo manda)
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
  app.js                # SPA frontend (~1500 líneas)
  style.css             # Responsive mobile-first
  landing/              # Assets Vite de la landing (dist/assets/) — generados, no editar a mano
templates/
  index.html            # Tabs: Oportunidades, Posiciones, Config, Cuenta (ruta /app)
  landing.html          # Landing pública (ruta /) — actualmente placeholder, se reemplaza con dist/index.html del repo basyo
  login.html            # DEPRECADO — sin uso, pendiente de borrar
```

---

## Base de datos — Tablas

| Tabla | Propósito |
|-------|-----------|
| `users` | Cuentas de usuario (email, password_hash, terms_accepted_at) |
| `user_configs` | Config por usuario (capital, thresholds, Telegram encrypted) |
| `user_positions` | Posiciones abiertas con earnings y historial de pagos |
| `user_history` | Posiciones cerradas para PnL histórico (columna `notes` para auditoría de ediciones manuales) |
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
3. Triggers definidos en `scanner/worker.py:22-30` (2 triggers, consolidados de 3):
   - **Pre-pago** (`PRE_PAYMENT_SCAN_MINS = 10`): verifica que la tasa siga favorable, genera alertas
   - **Post-settlement** (`POST_SETTLEMENT_DELAY_SECS = 180`): se dispara cuando el settlement ocurrió hace ≥3 min. En ese momento el exchange ya expone la tasa exacta en `fetch_funding_rate_history` y `nextFundingTime` ya apunta al siguiente período. Un solo scan captura la tasa real del pago y refresca `next_funding_ts` (reemplaza el antiguo POST de 1 min + REFRESH de 5 min).
   - **Force scan**: botón "Escanear" en UI o `POST /api/force`
4. Para **cross_exchange**, cada pierna (long y short) se evalúa independientemente para PRE y POST según su propio `next_funding_ts` e intervalo.

Esto significa que en periodos sin posiciones abiertas el bot puede estar inactivo.

### Captura exacta de la tasa al pago

Antes del cambio, `_record_earnings` grababa `cfr` (la tasa "vigente" al scan) y `time.time()` (el momento del scan) con un desfase de 5-15 s respecto al settlement real. Ahora:

- `_resolve_settlement_rate(exchange, symbol, settlement_ts, fallback)` en `scanner/worker.py` obtiene la tasa histórica del exchange para el timestamp exacto del settlement:
  - **CEX**: `ExchangeManager.fetch_settlement_rate()` → CCXT `fetch_funding_rate_history`, busca la entrada más cercana dentro de `SETTLEMENT_RATE_TOLERANCE_SECS = 120s`.
  - **DeFi**: `DefiExchangeManager.fetch_settlement_rate()` → query sobre `funding_rate_snapshots` filtrando por `funding_ts` dentro de tolerancia.
  - Si no hay match, usa `fallback=cfr` con un `log.warning`.
- `_record_earnings` acepta `payment_ts` (float, segundos) y lo usa como `ts` del payment record en `payments_json`. `last_earn_update` sigue siendo `now` (es la cota para `_count_payments_since`).
- El log `Settled <sym>@<ex> ts=...: cfr=X% historic=Y%` permite observar la divergencia corregida en producción.

---

## Sistema de scoring v11.0

Puntaje **0–100**. Re-optimizado con Optuna (600 trials) sobre 90 días de
`funding_rate_snapshots` vía `scripts/scoring_optimizer.py`. **Cambio de objetivo
vs v10.6:** el optimizer ahora maximiza **APR-neto (velocidad de capital)** neto de
fees, ajustado a riesgo y predictivo — no la durabilidad. Motivo: v10.6 premiaba la
estabilidad e **invertía el yield** (daba más score a 0.05%/día que a 0.18%/día). En
validación el bucket de score alto pasa de Net APR −10.9 a **+34.3** y el decil top
deja de empatar con el tier 2. Ver `reports/optimizer_20260617.md` y
`reports/profit_{diagnosis,simulation}_20260616.md`.

| Dimensión | Puntos (v10.6→v11.0) | Cambio |
|-----------|--------|-----------------------|
| Consistencia | 46 → **50** | streak/pct_positive (predictor real, ρ≈0.4) |
| Yield | 17 → **30** | **monótono** ahora (más yield = más score) |
| Fee efficiency | 8 → **16** | clave para el neto |
| Estabilidad | 21 → **4** | cv es peso muerto (ρ≈0.05 vs net) |
| Liquidez | 6 → **0** (spot_perp) | sin poder predictivo; cross/defi conservan 6 |
| Tendencia | 1 → **0** | irrelevante |

**Yield monótono con saturación:** `yield_day_pct` → factor creciente
(0.25/0.40/0.60/0.90/1.0) en umbrales 0.025/0.08/0.19/0.44 %/día × peso 30. El
reality-guard ya no es hard-cap sino un **multiplicador suave** (×0.90 si
`current_rate > 4× settlement_avg`).  
**Penalizaciones:** momentum accel −1 / decel −8 / neg −4; z-score (umbrales
3.3/2.6/1.8/1.5/0.9, hasta −23).  
**Hard caps:** z>2.0 → máx 47; streak<3 con percentil≥85 → máx 36 (el reality
hard-cap desaparece, vive en el multiplicador de yield).  
**Thin history** (<5 muestras): defaults neutros **re-escalados** a v11.0
(stability=3, consistency=22) para no sobre-acreditar símbolos nuevos/DeFi.

> v11.0 se re-optimizó solo sobre `spot_perp` (liquidez→0). Para
`cross_exchange`/`defi` se **conserva** la estructura de liquidez de v10.6 (6 pts),
sin re-validar — pendiente de un tuning propio. El optimizador nunca aplica cambios
solo — esta adopción fue manual tras revisar el reporte. **Pendiente de sync:**
`scripts/scoring_optimizer.py:BASELINE_PARAMS` sigue en v10.6; actualizarlo a v11.0
antes de la próxima re-optimización.

Para **cross_exchange / DeFi**, los indicadores (momentum, z-score, percentile, regime) se calculan sobre `period_diffs` — diferencial por-período emparejado por timestamp — en lugar del `diff_series` diario. Esto garantiza ≥5-15 muestras incluso con pocos días de histórico. Ver `analysis/arbitrage.py:_analyze_differential_history`.

El parámetro `mode` (`spot_perp` / `cross_exchange` / `defi`) ajusta los umbrales de liquidez.

Grades: A ≥85, B ≥70, C ≥55, D <55.

---

## Scoring ML en producción (el modelo manda el ranking)

Desde Etapa 3 (diagnóstico ML, PR #87) se confirmó que un GradientBoosting bate
al heurístico v11.0 de forma **estable y material** (uplift rank-IC +0.115 medio,
σ=0.021, positivo en los 5 folds walk-forward; Δ net_apr top-1% ≈ +24 anualizado).
La adopción (Opción B): **entrenar local, servir en prod**. El modelo **manda el
ranking**; el heurístico v11.0 queda como **fallback** si el modelo no carga o
falla.

### Riesgo #1 y su solución — PARIDAD de features

El modelo se entrena offline y predice online; si los features difieren, se rompe
en silencio. Solución: un **feature-builder compartido**
(`analysis/ml_features.py:build_feature_vector`) usado por entrenamiento E
inferencia. Garantías de paridad por construcción:

- **Indicadores** (`z`, `momentum`, `percentile`): salen de
  `compute_all_indicators`, que internamente toma `abs()` de todo → el signo del
  rate no afecta; prod y offline dan el mismo valor.
- **`fee_drag`**: NO se toma del orderbook (no reconstruible offline). Se computa
  **determinista** desde `settlement_avg` y `ppd`
  (`fee_drag_deterministic`, hold 30d, fee round-trip 0.30%), idéntico en ambos
  lados. El `fee_drag` real del orderbook que trae prod **se ignora** para el
  modelo.

`FEATURE_NAMES` (orden FIJO, 14): `cv, min_ratio, streak, pct, volume,
settlement_avg, ppd, fee_drag_det, current_rate_abs, reality_ratio, z_value,
mom_points, pctl_percentile, pctl_points`. **No reordenar sin re-entrenar** (el
`.joblib` asume posiciones).

### Componentes

| Archivo | Rol |
|---------|-----|
| `analysis/ml_features.py` | Feature-builder compartido (stdlib, sin sklearn). Única fuente del vector. |
| `analysis/ml_scorer.py` | **Prod**: `load_model()` (singleton al startup), `predict_score(params, indicators) → (score 0–100, pred) | None`. Import perezoso de joblib; cualquier fallo → `None` → heurístico. |
| `scripts/ml_train.py` | **Local**: entrena/valida/exporta. Corre cada ~15 días. |
| `models/scoring_model.joblib` | Artefacto commiteado (`{model, calibration_pcts, feature_names, model_version, train_window, val_metrics}`). Viaja con el deploy (no en `.gitignore`). |

### Flujo en el scan (`analysis/arbitrage.py`)

En `_analyze_spot_perp` y `_analyze_cross_exchange`, tras `opportunity_score`
(que deja los indicadores en `params["_indicators"]`),
`ArbitrageScanner._resolve_score(sc, params)` consulta el modelo: si predice,
`score = model_score` (calibrado 0–100 vía percentiles de train),
`score_heuristic = sc`, `model_prediction = pred`. Si no, los tres caen al
heurístico. El scan ya ordena por `score` → rankea por modelo. `grade`,
filtros `min_score` y todo lo demás operan sobre `score` (sin cambios). Campos
nuevos en los dataclasses `SpotPerpOpportunity`/`CrossExchangeOpportunity`:
`score_heuristic`, `model_prediction` (expuestos en `to_dict`).

### Logging para validación en vivo

`score_snapshots.model_prediction` (FLOAT) + `model_version` (VARCHAR) — escritos
en `scanner/worker.py:_store_score_snapshots` desde `opp["model_prediction"]` y
`ml_scorer.model_version`. Migración en `core/database.py`. Alimentan la
validación de predicciones previas de `ml_train.py`.

### Loop operativo de re-entreno (~cada 15 días, lo corre el usuario)

1. `pip install -r requirements-dev.txt` (local).
2. `python scripts/ml_train.py` → valida el modelo vivo contra el net_apr real de
   sus predicciones de ≥14d atrás, entrena uno nuevo (GBR sobre 90d), walk-forward
   vs heurístico v11.0, calibra el score, exporta `models/scoring_model.joblib`.
3. Revisar `reports/ml_train_YYYYMMDD.md`: ¿el IC en vivo se sostuvo? ¿el nuevo
   modelo bate al heurístico en walk-forward (uplift>0 en todos los folds, medio
   ≥0.05, σ≤0.05)?
4. Si convence: `git add models/scoring_model.joblib && commit && push` → Railway
   redeploya. Si no: investigar drift antes de promover.

### ⚠️ Pinning crítico de scikit-learn

El `.joblib` **debe cargarse con la MISMA versión** de scikit-learn que lo creó.
`scikit-learn==1.9.0` (+ `numpy==2.4.6`, `joblib==1.5.3`) están pinneados
**idénticos** en `requirements.txt` (prod) y `requirements-dev.txt` (local). Un
mismatch rompe `joblib.load` en Railway → `load_model` devuelve `False` y prod cae
al heurístico (degradación segura, pero se pierde el modelo). Re-pinnear ambos si
se cambia de versión al re-entrenar.

### Guardrails

- Modelo no carga / `predict` lanza → `None` → heurístico v11.0 (degradación
  segura, el scan nunca cae).
- `model_score` clampeado a [0,100] por la calibración por percentiles.
- `score_heuristic` se conserva y loguea → comparar modelo vs heurístico en vivo.
- Sin auto-trading nuevo: el modelo solo cambia el **ranking/score** mostrado;
  ejecutar órdenes sigue siendo decisión del usuario.

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

## Dashboard de earnings (Posiciones tab)

Encima del listado de posiciones, la tab "Posiciones" muestra:

- **Capital bar** (existente): Total, En uso, Disponible, Ganancia, contador de posiciones, botón IA.
- **KPI widgets** (`#earnings-kpis`): Hoy (con delta vs ayer), 7 días (con APR realizado), 30 días, Total (activas + cerradas all-time). Verde/rojo según signo. `static/app.js:loadDailyEarnings()` y `renderEarningsKpis()`.
- **Gráfica con toggle** (`#earnings-chart`): 
  - **Cumulativo activas** (default): líneas multi-color por posición sobre `payments_json[].cumulative`, ignora entries con `kind:"manual_adjust"` para no romper la curva. 
  - **Diario total**: bar chart sobre `series[]` del endpoint, una barra por día (verde si neto≥0, rojo si <0). Incluye cerradas.

Datos provistos por `GET /api/earnings/daily?days=30` → `DBPersistence.aggregate_daily_earnings()` en `core/db_persistence.py`. Bucketing por día local del servidor. Limitación documentada: `realized_apr_7d` usa `capital_in_use` actual como aproximación (no series histórica de capital).

## Edición manual de earnings

**Semántica: valor absoluto reseteable.** El usuario fija un total; el scanner sigue sumando los próximos settlements encima de ese valor. Sin flag de "freeze".

**Posiciones activas** (`PATCH /api/positions/<id>/earnings`, body `{earned}`):
- Setea `earned_real = nuevo_total`, `last_earn_update = now()`. 
- `_count_payments_since` ya usa `last_earn_update` como cota inferior, así que los próximos pagos solo cuentan settlements posteriores al ajuste manual — **cero cambios en `_record_earnings`**.
- Audita appendeando una entrada a `payments_json` con `kind:"manual_adjust"`, `earned=delta`, `cumulative=nuevo_total`. El renderer del chart cumulativo la filtra; el scoring lo ignora (no es una tasa de funding).

**Posiciones cerradas** (`PATCH /api/history/<id>/earnings`, body `{earned?, fees?}`):
- Edita `earned`/`fees` directamente en `user_history`, recalcula `net_earned = earned - fees`.
- Cada edición se appendea a `user_history.notes` con timestamp para auditoría.
- En el UI, las filas editadas muestran un mark `✎`.

UI: `editPosEarnings(posId)` / `editHistEarnings(histId)` en `static/app.js`, mirror del patrón existente `editPosFees`.

---

## Auto-ejecución de órdenes (CEX)

Coloca y ejecuta las órdenes reales de ambas piernas usando las API keys del usuario. **Trigger por botón** (semi-automático), no autónomo: el scanner sigue siendo event-driven.

**Alcance: solo CEX** (binance/bybit/okx/bitget). DeFi y spot on-chain (Binance Alpha, Bitget Onchain, Web3 wallets) **no** son operables por CCXT → siguen en flujo manual.

### Módulo `exchanges/trade_executor.py`
Única capa que coloca órdenes reales, **aislada** del `ExchangeManager` global de solo-lectura. Cada llamada construye un cliente CCXT autenticado fresco desde las keys *del usuario* (`build_user_client`, importación perezosa de ccxt).
- `test_connection(ex, creds)` → `fetch_balance` (botón "Probar conexión").
- `spot_tradeable(ex, creds, symbol)` → gate: el símbolo debe ser mercado **spot centralizado** operable (`spot=True, active`). Excluye Alpha/Onchain/Web3.
- `execute_open(creds_by_exchange, opp, capital, leverage, dry_run)` — metodología de `build_entry_strategy`:
  - `spot_perp`: **Limit BUY spot (mid, 60s) → Market SHORT perp** al llenar. Si el perp falla, **unwind** del spot a market.
  - `cross_exchange`: **Limit en ambas piernas (90s)**; si solo una llena, cancela la otra + **unwind** a market.
- `execute_close(...)` — revierte ambas piernas a market (`reduceOnly` en perps).
- `dry_run=True` → simula sin enviar (alimenta el modo "Simular" de la UI). Sizing/precisión vía `amount_to_precision` + `contractSize` (OKX/Bitget usan contratos).
- Env `TRADE_SANDBOX=1` → `set_sandbox_mode(True)` en todos los clientes (testnet).

### Rutas (`api/routes.py`)
`/api/account/exchange_keys/test`, `/api/execute_open`, `/api/execute_close`. `execute_open` reusa el lookup de oportunidad y la validación de capital de `open_position` (bookkeeping), luego **sobreescribe con fills reales** (`entry_price`, `entry_fees_real`), marca `auto_executed=True` y audita `order_ids` en `payments_json` (`kind:"auto_open"`). Fallo con pierna llena → alerta crítica `EXEC_FAILURE` vía `_dispatch_alerts_per_user` (`_exec_alert`).

### Persistencia
- `DBPersistence.load_user_exchange_keys(user_id, exchange)` descifra las keys (Fernet) para el executor.
- Columna nueva `user_positions.auto_executed BOOLEAN` (migración en `core/database.py`, modelo en `db_models.py`, expuesta en `_pos_to_dict`).

### Frontend (`static/app.js`, `style.css`, `templates/index.html`)
- Opp card: botón **"Ejecutar"** (`.btn-exec`) junto a "Entrar". Deshabilitado con tooltip si: DeFi/no-CEX, `has_spot=false`, o faltan keys (`_cexKeys` cargado vía `loadUserKeys`).
- Positions: botón **"Cerrar (auto)"** (solo si `posIsCex`) + badge **AUTO**.
- Modal de confirmación de riesgo (`showExecConfirm`) con 3 acciones: Cancelar / **Simular** (dry-run) / **Ejecutar órdenes reales**. Modal de resultado (`showExecResult`) con tabla de fills; desde una simulación se puede promover a ejecución real.
- Cuenta: botón **"Probar conexión"** por exchange (`testExchangeKey`).

### Precondiciones / límites v1
- Fondos ya en la wallet correcta (spot wallet para la pierna spot, futures para márgenes). **Sin auto-transferencia entre wallets.**
- `set_leverage` best-effort (no-fatal). Margin mode se deja en el default de la cuenta.
- Slippage real asumido (ya estimado por el sistema).

---

## Notificaciones Telegram

**Cada usuario** gestiona sus propias credenciales (modelo self-service):

1. Crear bot con `@BotFather` → obtener Bot Token
2. Enviar `/start` al bot → obtener Chat ID via `@userinfobot`
3. Pegar ambos en Config del dashboard → "Prueba Telegram"

**Flujo de envío:**
- `scanner/worker.py:_dispatch_alerts_per_user()` — agrupa alertas por `user_id`, carga credenciales de cada usuario desde DB y las pasa **explícitamente** a `email_notifier.send_alerts(alerts, chat_id=..., token=...)`. No muta el estado compartido (elimina carrera entre usuarios concurrentes). Alertas huérfanas (sin `user_id`) se descartan con `log.warning` en vez de routearse al último chat en memoria.
- `scanner/worker.py:_broadcast_alerts_all_users()` — oportunidades excepcionales → todos los usuarios con notificaciones activas (mismo mecanismo de credenciales explícitas).
- `notifications/email.py:_send_telegram(text, chat_id, token)` — POST JSON a `api.telegram.org/bot{TOKEN}/sendMessage`. Acepta credenciales por parámetro; cae a las del state como fallback (path de test).
- `notifications/email.py:build_alert_dedup_key(alert)` — única fuente de verdad para la dedup_key: `f"{type}:{user_id}:{symbol}:{exchange}:{bucket}"` donde `bucket` es `funding_ts` (ms o s) para alertas window-bound, o `_exc_bucket` (`YYYYMMDD`) para broadcasts diarios.
- `notifications/email.py:valid_telegram_creds(chat_id, token)` — validación de formato barata antes de golpear la API (chat_id numérico ≥5 chars, token con `:` y ≥20 chars). Si falla, log único por usuario y skip.

**Tipos de alerta:** `RATE_REVERSAL` (crítica), `RATE_DROP`, `PRE_PAYMENT_UNFAVORABLE`, `SL_TP_REVIEW`, `EXCEPTIONAL_OPPORTUNITY`, `SWITCH_OPPORTUNITY`, `POSITION_CLOSED`.

---

## Config de usuario (campos activos)

| Campo | Efecto real |
|-------|-------------|
| `total_capital` | Valida capital disponible al abrir posición (`portfolio/manager.py:43`) |
| `max_positions` | Bloquea abrir más posiciones (`portfolio/manager.py:68`) |
| `min_volume` | **Filtro de display** (panel Filtros, client-side). Persistido para recordarlo entre sesiones; ya **no** gatea el scan. El scan usa un piso bajo y fijo `SCAN_MIN_VOLUME` (`scanner/worker.py`). |
| `min_apr` | **Filtro de display** (panel Filtros, client-side). Ya no se filtra en `/api/opportunities`. |
| `min_score` | **Filtro de display** (panel Filtros, client-side). Ya no se filtra en `/api/opportunities`. |
| `min_stability_days` | **Filtro de display** (panel Filtros, client-side, sobre `estimated_hold_days`). Ya no se filtra en `/api/opportunities`. |
| `allowed_exchanges` | **Filtro de display** (panel Filtros, client-side): CSV de exchanges a mostrar; `""` = todos. Matchea `exchange`/`long_exchange`/`short_exchange`. |
| `alert_minutes_before` | Dispara `PRE_PAYMENT_UNFAVORABLE` N minutos antes del pago |
| `email_enabled` | Master switch de notificaciones Telegram |
| `tg_chat_id` / `tg_bot_token` | Credenciales Telegram (token cifrado con Fernet) |

`scan_interval` existe en la tabla DB pero **no se usa** — el scanner es event-driven.

### Filtros unificados de oportunidades (panel Filtros)

Todos los filtros de la lista viven en **un solo panel "Filtros"** en la pestaña
Oportunidades (`templates/index.html`, `static/app.js`), aplicados **en vivo
client-side** sobre la lista completa que devuelve el backend, válidos tanto para
CEX como DeFi. Antes estaban dispersos: los inputs APR/Score del toolbar estaban
muertos (disparaban un reload pero su valor nunca se leía) y los reales vivían en
la pestaña Config filtrando server-side.

- `static/app.js:sortAndFilter()` filtra por `apr` / `score` / `estimated_hold_days`
  / `volume_24h` / exchange + búsqueda por símbolo. El volumen `0`/desconocido
  (DeFi) **no oculta** la oportunidad.
- Los valores se persisten en `user_configs` vía `/api/config` (debounced) y se
  cargan con `loadFilters()` al iniciar. La pestaña Config solo conserva
  `total_capital`, `max_positions`, `alert_minutes_before` y Telegram.
- `/api/opportunities` y `/api/defi_opportunities` devuelven la lista **completa**
  (sin filtrar para display); el panel es la única fuente de verdad.
- El scan corre con `SCAN_MIN_VOLUME` (piso bajo fijo) para un universo amplio; en
  el gate cross-exchange, `volume_24h == 0` (desconocido en DeFi) **pasa** en vez
  de excluir la pierna (`analysis/arbitrage.py`).

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
TRADE_SANDBOX         # =1 → auto-ejecución usa testnet CCXT (set_sandbox_mode)
```

> Nota: las API keys globales de CEX (arriba) son para datos públicos del scanner. La **auto-ejecución** usa las keys *por usuario* guardadas en `user_exchange_keys` (tab Cuenta), no estas.

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

## Arquitectura de rutas

| URL | Auth | Template / handler |
|-----|------|--------------------|
| `GET /` | Pública | `templates/landing.html` (landing + modal login/registro) |
| `GET /app` | `@auth_required` | `templates/index.html` (dashboard SPA) |
| `GET /terms`, `/privacy` | Pública | `templates/landing.html` — react-router monta `<TermsOfService />` o `<PrivacyPolicy />` en cliente |
| `GET /<otra-ruta-SPA>` | Pública | catch-all en `api/routes.py:landing_catchall` → `landing.html` (futuras rutas SPA) |
| `POST /auth/login` | Pública | JSON `{ok, msg}` — Flask-Login cookie |
| `POST /auth/register` | Pública | JSON — requiere `terms_accepted: true` en body; setea `users.terms_accepted_at` |
| `POST /auth/logout` | Autenticado | JSON — redirigir a `/` tras logout |
| `GET /auth/me` | Autenticado | JSON `{ok, user}` — devuelve 401 JSON si no auth |
| `GET /auth/page` | Pública | `301 → /?login=1` (compat deep-links) |
| `GET /health` | Pública | JSON (healthcheck Railway) |
| `GET /api/*` | `@auth_required` | JSON — 401 JSON si no auth |
| `GET /api/opportunities` | `@auth_required` | Lista **completa** (sin filtrar para display); los filtros se aplican client-side en el panel Filtros. Recalcula `mins_to_next` y enriquece con `score_trend`. |
| `GET /api/earnings/daily?days=N` | `@auth_required` | Rollup diario combinando activas + cerradas: `{today, yesterday, last_7d, last_30d, all_time, realized_apr_7d, series:[{date, earned, fees, net}]}` |
| `PATCH /api/positions/<id>/earnings` | `@auth_required` | Body `{earned: float}` — override absoluto reseteable: setea `earned_real` + `last_earn_update=now`, agrega entry `kind:"manual_adjust"` a `payments_json`. Próximos settlements se acumulan encima. |
| `PATCH /api/history/<id>/earnings` | `@auth_required` | Body `{earned?, fees?}` — edita posición cerrada, recalcula `net_earned`, registra en `notes`. |
| `POST /api/account/exchange_keys/test` | `@auth_required` | Body `{exchange}` — valida las API keys guardadas vía `fetch_balance`. Devuelve `{ok, msg, usdt_balance?}`. |
| `POST /api/execute_open` | `@auth_required` | Body `{opportunity_id, capital, leverage?, dry_run?}` — coloca las órdenes reales (CEX only) vía `trade_executor`, guarda la posición con `auto_executed=True` y fills reales. `dry_run:true` simula sin enviar. |
| `POST /api/execute_close` | `@auth_required` | Body `{position_id, dry_run?}` — coloca las órdenes inversas a market y cierra con `exit_fees_real` reales. |

**Flujo unauthenticated:** `GET /app` → `unauthorized_handler` → `302 /?login=1` → landing abre modal login.  
**Flujo logout:** `POST /auth/logout` → `window.location = '/'` (en `static/app.js:doLogout`).  
**Flujo delete-account:** `DELETE /api/account` → éxito → `window.location = '/'`.

### Páginas legales (Términos y Privacidad)

`/terms` y `/privacy` son rutas del SPA React (no de Flask). Flask las atrapa con `@app.route("/<path:_spa_path>")` en `api/routes.py` y sirve `landing.html`; react-router monta `<TermsOfService />` o `<PrivacyPolicy />` desde `src/pages/`. El layout legal compartido vive en `src/components/legal/LegalLayout.tsx`.

El registro en `/auth/register` exige `terms_accepted: true` en el body o devuelve 400. Persistimos la marca de tiempo en `users.terms_accepted_at` para auditoría legal. El frontend muestra un checkbox en `AuthModal.tsx` (tab Registro) con links a `/terms` y `/privacy` en `target="_blank"`.

Para editar los textos legales: modificar `src/pages/TermsOfService.tsx` y `src/pages/PrivacyPolicy.tsx` en basyo, recompilar.

### Cómo editar la landing

El `templates/landing.html` es un shell de 28 líneas — solo meta tags y el `<div id="root">`. El contenido visible está en el bundle JS (`static/landing/assets/index-*.js`).

**Cambios editables directamente:**
- Meta tags SEO (título, description, og:image) → `templates/landing.html`
- Favicon → reemplazar `static/landing/favicon.png`

**Cambios de contenido (textos, secciones, colores, CTAs)** requieren recompilar el source React:
1. El source está en `/home/user/basyo` (clonado en el sandbox, rama `claude/integrate-landing-auth`).
2. Editar los `.tsx` correspondientes.
3. `cd /home/user/basyo && npm run build`
4. Copiar output:
   - `dist/index.html` → `templates/landing.html`
   - `dist/assets/*` → `static/landing/assets/`
5. Commit + push en `funding_rate_bot` → Railway redeploy.

El repo [`Jeftewan/basyo`](https://github.com/Jeftewan/basyo) en GitHub está **deprecado** — ya no se usa para deploy. El source de referencia vive en el sandbox.

---

## Pendiente / Lo que falta

### Funcionalidad incompleta (infraestructura lista, falta conectar)

- **Auto-trading CEX**: ✅ implementado (ver sección "Auto-ejecución de órdenes" abajo). Botón "Ejecutar" por oportunidad + "Cerrar (auto)" por posición colocan órdenes reales vía las API keys del usuario. **Pendiente**: auto-trading DeFi (Hyperliquid/GMX/Aster/Lighter/Extended) y spot on-chain (Binance Alpha, Bitget Onchain) — esos no exponen API de órdenes usable por CCXT y siguen en flujo manual.
- **Coinglass**: cliente existe en `coinglass/client.py`, no integrado en el flujo principal.

### Mejoras identificadas

- **DeFi history fiabilidad**: `fetch_funding_history` en `defi_manager.py` depende de que los snapshots existan en DB. En deploys nuevos o símbolos nuevos, thin-history (<5 muestras) da scoring neutro — correcto pero subóptimo para toma de decisiones. Nota: GMX, Aster, Lighter y Extended reportan `volume_24h=0`; el gate de volumen del scan trata ese `0` (desconocido) como **pasa**, así que estos pares ya no quedan excluidos (el filtro de volumen real es de display, client-side).
- **Scoring DeFi bajo (pendiente)**: los modos `cross_exchange`/`defi` puntúan bajo de forma estructural — consistency (46) + stability (21) = 67% del score se calculan sobre el *diferencial* (`period_diffs`), que cambia de signo mucho más que una tasa única → streak/consistencia bajos, CV alto. A esto se suma thin-history y `volume_24h=0`. **Pendiente** (otro plan): `defi_opportunity_score()` separado con umbrales heurísticos, tras verificar cuántos snapshots/muestras hay acumulados.
- **Alertas sin posiciones activas**: el scanner solo corre cuando hay posiciones abiertas o se fuerza manualmente. Si no hay posiciones, las oportunidades excepcionales no se escanean automáticamente.
- **Tests**: no hay suite de tests automatizados. El proyecto depende del syntax check via `python -c "import ast; ast.parse(...)"`.
- **Rate limits DeFi**: los adaptadores DeFi no tienen retry exponencial ante errores HTTP 429/503.
- **Legacy columns**: `user_configs` tiene `wa_phone`, `wa_apikey_encrypted`, `scan_interval` — columnas de versiones anteriores. No se usan, pueden limpiarse con una migración DROP COLUMN cuando sea conveniente.

### Deuda técnica menor

- `notifications/email.py` se llama `EmailNotifier` por compatibilidad histórica (era CallMeBot WhatsApp). Sin urgencia de renombrar ya que el nombre interno no afecta al usuario.
- `scripts/migrate_json_to_db.py` referencia campos legacy (`wa_phone`, `scan_interval`). No afecta producción.
- `portfolio/actions.py` tiene lógica de display parcialmente duplicada con `portfolio/manager.py`.

---

## Evaluación y re-optimización del scoring (local, manual)

El scoring se revisa periódicamente porque los pesos óptimos cambian con el
régimen de mercado. Dos herramientas en `scripts/`, **ambas locales** (no corren
en Railway; leen su DB en **solo lectura** vía `DATABASE_URL`). Dependencias en
`requirements-dev.txt` (`optuna`, `pandas`, `sqlalchemy`, `numpy`) — **no** van en
el `requirements.txt` de producción. Capa de datos compartida en
`scripts/_scoring_data.py` (carga + caché de `funding_rate_snapshots`, features
crudos por ventana), reusada por ambos scripts.

| Script | Qué hace |
|--------|----------|
| `scripts/scoring_backtest.py` | **Evalúa** el scoring v10.5 actual: correlación score↔forward returns por componente, decay, mean-reversion z, valor del accel bonus. Importa el `opportunity_score` real (sin duplicar). Output: `reports/backtest_YYYYMMDD.md`. |
| `scripts/scoring_optimizer.py` | **Re-optimiza** los pesos vía Optuna sobre un scoring paramétrico que espeja v10.5. Split train/val temporal, gate de monotonicity, guard de overfitting. **Solo genera candidato + reporte; nunca toca `analysis/scoring.py`.** Alcance v1: spot_perp. Output: `reports/optimizer_YYYYMMDD.md`, `reports/scoring_candidate_YYYYMMDD.py`, `_trials.csv`. |
| `scripts/ml_diagnostic.py` / `scripts/ml_stability.py` | **Diagnóstico** (Etapa 3): ¿el ML supera el techo del heurístico? IC y walk-forward. Soporte de decisión, no producción. |
| `scripts/ml_train.py` | **Entrena/valida/exporta el modelo de producción** (loop de ~15d): valida predicciones previas, entrena GBR, walk-forward, calibra, exporta `models/scoring_model.joblib`. NO despliega (imprime las instrucciones git). Ver sección "Scoring ML en producción". |

**Flujo de adopción:** correr el optimizer → leer el veredicto del reporte
(ADOPTAR / REVISAR / MANTENER) → si convence, copiar a mano la función del
candidato sobre `analysis/scoring.py:opportunity_score`. El optimizer nunca
aplica cambios automáticamente.

Lanzador local: `run_optimizer.ps1` (carga `DATABASE_URL` desde `.env`, valida
deps, reenvía flags al script).

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

# Evaluar el scoring actual (local, read-only sobre la DB)
pip install -r requirements-dev.txt
python scripts/scoring_backtest.py

# Re-optimizar pesos del scoring (genera candidato + reporte, no aplica)
.\run_optimizer.ps1 --trials 20          # prueba rápida
.\run_optimizer.ps1                       # run completo (600 trials)
python scripts/scoring_optimizer.py --trials 20   # equivalente sin wrapper
```

---

## Últimos commits relevantes

| Hash | Cambio |
|------|--------|
| `29cfc04` | feat(filters): unificar filtros de oportunidades en un panel (APR/score/días/volumen + filtro por exchange) aplicado en vivo client-side; `/api/opportunities` deja de filtrar para display; scan con piso `SCAN_MIN_VOLUME`; DeFi `volume_24h=0` ya no se excluye; nueva columna `user_configs.allowed_exchanges` |
| `pending` | feat(positions): minidashboard de earnings (KPIs hoy/7d/30d/total + chart toggle cumulativo/diario), edición manual de earnings en activas y cerradas, limpieza flujo Telegram (credenciales explícitas sin mutar state, dedup_key consolidada, validación de formato, drop orphan alerts, fix logs legacy WhatsApp) |
| `ec872cd` | feat(routing): landing pública en `/`, dashboard SPA en `/app`, login modal en landing |
| `85b4f6a` | Fix timing captura tasa al pago: fetch_settlement_rate CEX/DeFi, triggers 3→2 |
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
