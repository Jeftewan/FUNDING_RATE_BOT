# CLAUDE.md — Funding Rate Arbitrage Bot

Estado del proyecto al **2026-10-25** (auditoría de rentabilidad, rama `feat/profit-research`).

---

## Stack

| Capa | Tecnología |
|------|-----------|
| Backend | Python 3 + Flask |
| Servidor | Gunicorn (Procfile, `--bind [::]:$PORT`, 1 worker × 2 threads) |
| Base de datos | PostgreSQL via SQLAlchemy (flask-sqlalchemy). **SQLAlchemy pinneado `<2.1`** y URL forzada a `postgresql+psycopg2://` (2.1 usa psycopg v3 por defecto → tumbó el login el 2026-10-09) |
| Migraciones | `core/database.py._run_migrations()` — ALTER TABLE ADD COLUMN IF NOT EXISTS, sin Flask-Migrate |
| Exchange CEX | ccxt (Binance, Bybit, OKX, Bitget) |
| Exchange DeFi | REST APIs propias (Hyperliquid, GMX, Aster, Lighter, Extended) |
| IA / LLM | Gemini 3.1 Flash-Lite (`google-genai`, 3 API keys rotadas para evitar rate limits) |
| Notificaciones | Telegram Bot API (POST JSON, sin dependencias externas) |
| Cifrado | Fernet simétrico (`core/encryption.py`) para tokens y API keys |
| Frontend (servicio `web`) | Caddy (`frontend/Caddyfile`) sirve estáticos y hace proxy de `/api`, `/auth`, `/health` al backend por red privada |
| Frontend (dashboard) | Vanilla JS + CSS3, sin frameworks (`frontend/site/static/app.js`, `style.css`) |
| Frontend (landing) | React + Vite — compilado en `frontend/site/static/landing/assets/`. Source original en [`Jeftewan/basyo`](https://github.com/Jeftewan/basyo) (Lovable, **deprecado**). Editar via recompilación en este repo. |

**El dashboard SPA no usa React.** Todo el threading del backend es stdlib. La landing es un bundle Vite estático servido por Caddy — el contenido visible vive en `frontend/site/static/landing/assets/index-*.js`, no en `frontend/site/index.html`. **Flask ya no sirve HTML** (`static_folder=None`): es API pura.

---

## Auditoría 2026-10-25 — de dónde sale la ganancia

- **El edge está en tokens Alpha** (spot solo on-chain en Binance Alpha/BSC): los 17 trades Binance
  reales lo son. El simulador viejo (`ml_binance.py`, `exit_policy_study.py`) filtraba `has_spot` (spot
  CEX) → daba 3.7 % anual. Con CEX ∪ Alpha (`scripts/attribution_study.py`) da 29.9 % (12 m) y 26.4 %
  en el periodo real vs ROC realizado 32 % (Binance) / 52 % (Bitget). Lista Alpha pública con
  `listingTime` (`cache/binance/alpha_tokens.json`); sesgo de supervivencia (tokens borrados no salen).
- **Modelo** (`scripts/ml_v2.py`, `reports/ml_v2_20261025.md`): en ese universo el vigente rinde 33.2 %
  WF / 14.9 % hold-out; 7 variantes (ventanas, régimen, q25, P(ganancia), E×P) no lo superan de forma
  consistente → no se exportó. Rankear por probabilidad empeora. Mensual a 1x: 1.7–6.7 %, típico ~3 %.
- **Leverage** (`scripts/leverage_study.py`, seguros simulados con velas 1m): Alpha 1x 33 %, **2x
  36–44 %**, 3x 24–28 % (SL en 57 % de entradas); CEX mejor a 3x pero 3–5 %. → `RECOMMENDED_LEV`.
- **Rotación**: ver `rotation_gain_ok` en switch_analyzer (margen 2 % → +18 % vs no rotar).
- **Mercado 2026**: funding BTC 11.9 % (2024) → 5.1 % (2025) → 3.0 % (2026 YTD); BTC/ETH a 10x no
  sirve (≈3–5 % anual sobre capital). 5–10 %/mes sostenido no es realista; 3–4 %/mes en meses buenos.
- **JCT `user_history.id=46`**: registraba `price_pnl=+438` (faltaba la recompra manual del short); real
  +57.5. Fix en el código (ver "Lecciones de JCT #46"); la fila se corrige con
  `scripts/fix_external_buyback.py --hist 46 --apply` (lo corre el usuario tras el merge).

## Auditoría 2026-10-22 — foco Binance spot_perp

**Decisión:** solo Binance + solo spot_perp (`ENABLED_EXCHANGES=binance`, `ARBITRAGE_MODES=spot_perp`,
`DEFI_ENABLED=0`, reversibles por env). Binance = única ejecución automática con seguros, 76 % del PnL
realizado y más pares de carry alto con liquidez. cross/DeFi: 2 trades en 6 meses, IC en vivo −0.15.

**Ejecución (tests `tests/test_exec_safety.py`):** el cierre lee el short ANTES de operar y aborta si
`fetch_positions` falla (`_open_perp_size` → `None`); reconciliación de cierre con signo correcto;
órdenes idempotentes (`_create_idempotent`, búsqueda por `origClientOrderId`); `order_fills` cae al
order si los trades van atrasados; estados de seguros nunca se asumen "cancelado" (`_algo_state`,
`_limit_state`, llenado parcial = disparado); si un seguro no se cancela se aborta el cierre; vigilante
por posición (aunque haya otro job), `NAKED_SPOT` y `LIQUIDATION_RISK`; `submit_exclusive` contra el
doble clic; `entry_time` = inicio del job; la auto-ejecución solo acepta spot_perp.

**Scan (tests `tests/test_scan_quality.py`):** candidatos = todos los perps con spot cubrible y tasa
DIARIA ≥ base (0.03 %/día), prima primero y luego volumen, tope 250 (antes top-20 por tasa cruda);
se eliminó el filtro `net_3d>0` (exigía ~73 % APR → solo picos); `net_3d` con la pierna perp y con
signo; ventana del modelo = 30 liquidaciones con `analysis/ml_features.window_stats` (misma función que
offline); `fee_loader` ya no toma defaults spot como fee del perp. Verificado contra la API pública
(2026-10-22): 149 pares analizados (antes 20), 3 con APR neto esperado > 0 (antes 1) → el mercado está
comprimido, no es un bug.

**Salidas:** `CLOSE_SUGGESTED` = dos settlements seguidos sin cobrar (último pago ≤ 0 y el próximo
también). `reports/exit_policy_20261022.md`: esa regla (B) le gana a salir al primer pago ≤ 0, y
mantener hasta ~30 d con ella es lo mejor (+3.7 % anual simulado vs +1.6 %); hold fijo pierde; un piso
de rendimiento alto NO mejora (deja slots vacíos).

**Dataset y modelo:** `scripts/binance_dataset.py` baja de data.binance.vision + fapi las tasas
LIQUIDADAS desde 2024 (2.25 M filas, 890 símbolos con deslistados) a `cache/binance/fr_binance.csv`.
`scripts/ml_binance.py` entrena con label realista de 14 d, IC diario, walk-forward mensual purgado,
hold-out de 30 d y cartera simulada (`reports/ml_binance_20261022.md`). Resultado: IC 0.54–0.55 pero
uplift vs heurístico +0.02/+0.03 (< 0.05) y el modelo vigente (20261009) rinde igual o mejor →
**no se exportó** modelo nuevo. Prod soporta `feature_spec` en el artefacto (`estimate_v0` vigente;
`settled_v1` = ventana de 30 liquidaciones previas + última liquidada como tasa actual) por si un
modelo del dataset Binance pasa el gate. `--export E0|E0a` exporta.

**Realidad del negocio:** en la simulación sistemática (5 slots, 5.2 k USD, 1x) el edge neto de fees
sobre 13 meses es chico (~3–4 % anual sobre capital), lejos del ~29 % realizado. Para atribuirlo, cada
apertura auto guarda `entry_model_prediction`/`entry_score` en el `auto_open` de `payments_json`.

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
  ai_analyzer.py        # Prompts Gemini, parse JSON, COMPRAR/MANTENER/EVITAR
  switch_analyzer.py    # Evalúa conveniencia de cambiar posición
  funding.py            # FundingAggregator: acumulados 3d, APR, ingreso diario
exchanges/
  manager.py            # ExchangeManager CEX (CCXT), historial, spot check
  defi_manager.py       # DefiExchangeManager, historial desde snapshots DB
  trade_executor.py     # ÚNICA capa que coloca órdenes CEX (spot_perp pareado, TWAP, cross, reconcile)
  binance_account.py    # Cuenta Binance del usuario: permisos, modo, saldos, posiciones, income
  onchain_bsc.py        # Pierna spot on-chain BSC (wallet dedicada, tokens Alpha)
  dex_aggregator.py     # Binance Web3 API (HMAC) / 0x / KyberSwap (fallback por región) para swaps BSC
  kyber_limit_order.py  # Órdenes límite on-chain BSC (seguro de la pierna spot, 7 días)
execution/
  jobs.py               # Jobs de ejecución real en background + reconciliación
  account_sync.py       # Sync periódico de cuentas reales (snapshots, funding real, drift)
  protection.py         # Vigilante de seguros (SL/venta límite): cierra la otra pierna, renueva BSC
tests/                  # pytest: executor, jobs (SQLite), on-chain (mocks)
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
frontend/               # Servicio Railway "web" (rootDirectory=/frontend)
  Caddyfile             # Estáticos + proxy /api /auth /health → $BACKEND_URL
  Dockerfile            # caddy:2.10-alpine
  site/
    index.html          # Landing pública (ruta /, /terms, /privacy) — dist/index.html de basyo
    app.html            # Dashboard (ruta /app). HTML estático: email y gate de login vía GET /auth/me en app.js
    static/
      app.js            # SPA frontend
      style.css         # Responsive mobile-first
      landing/          # Assets Vite de la landing (dist/assets/) — generados, no editar a mano
```

---

## Base de datos — Tablas

| Tabla | Propósito |
|-------|-----------|
| `users` | Cuentas de usuario (email, password_hash, terms_accepted_at) |
| `user_configs` | Config por usuario (capital, thresholds, Telegram encrypted) |
| `user_positions` | Posiciones abiertas con earnings y historial de pagos; `spot_qty`/`perp_qty`, `spot_venue`, `source` (manual/auto/imported), `earnings_source` (estimated/real), `job_id` |
| `user_history` | Posiciones cerradas para PnL histórico (columna `notes` para auditoría de ediciones manuales) |
| `user_exchange_keys` | API keys de exchanges cifradas (Fernet, **fail-closed**); usadas por sync y auto-ejecución |
| `user_account_snapshots` | Último estado real por (user, exchange): `binance` (saldos, posiciones, permisos, modo, importables, drift) y `bsc_wallet` |
| `execution_jobs` | Jobs de órdenes reales en background (open/close, TWAP), progreso y resultado |
| `user_wallets` | Wallet BSC dedicada del usuario (address + private key cifrada) |
| `scan_cache` | Último resultado del scan (opportunities_json, defi_json) |
| `funding_rate_snapshots` | 90 días de tasas históricas, dedup por (symbol, exchange, funding_ts) |
| `score_snapshots` | Evolución del score por oportunidad (ventana rolling 30 entradas) |
| `market_feature_snapshots` | Señales horarias por par Binance spot_perp (clave única symbol, exchange, `ts_hour` = inicio de la hora UTC en epoch s): tasa estimada del próximo pago, mark/index, basis spot–perp (mids del book), OI y Δ1h, taker buy/sell, top long/short (posición), intervalo y cap/floor, volatilidad 7 d. Retención 120 d. Ver "Señales de mercado horarias" |

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

   - **Scan horario** (`PERIODIC_SCAN_SECS`, a :04 UTC tras los settlements): corre aunque no haya posiciones (desde 2026-10-22). Antes, sin posiciones el bot quedaba mudo y no escribía snapshots.

### Señales de mercado horarias (Fase 2, `exchanges/market_features.py`)

Binance guarda solo ~30 d de OI y ratios por API → se recolectan ya para entrenar el modelo de la Fase 3.
- **Cuándo**: al final de `_run_scan_inner`, la **primera vez que escanea en cada hora UTC** (el scan horario
  garantiza ≥1 scan por hora; si el primero es un scan por evento, ese cuenta). Hilo en background; si no
  guarda nada o falla, se reintenta en el siguiente scan. Solo candidatos Binance de `spot_perp_opps`.
- **Costo**: 4 llamadas de todos-los-símbolos (`/fapi/v1/premiumIndex`, `/fapi/v1/fundingInfo`, bookTicker spot
  y perp) + 3 por símbolo en `/futures/data` (`openInterestHist`, `takerlongshortRatio`,
  `topLongShortPositionRatio`; 1000 req/5 min por IP → paceadas a `DATA_MIN_INTERVAL` = 0.32 s, ~4 min con
  250 pares) + klines 1d del perp una vez por símbolo y día (vol 7 d). Mismo cliente ccxt/proxy que
  `exchanges/manager.py`. 5 errores seguidos en `/futures/data` → se omite OI/ratios del resto.
- **Tolerancia**: una fuente caída deja NULL en sus columnas; el upsert (`upsert_rows`, Postgres/SQLite) usa
  `COALESCE` → un NULL no pisa un valor ya guardado en la misma hora. Purga diaria > 120 d (`purge_old`).
- **Definiciones** (compartidas con el backfill): `basis_pct = (perp_mid − spot_mid)/spot_mid × 100`;
  `oi_change_1h_pct` del par de puntos 1h; `taker_buy_sell_ratio` = última hora cerrada; `volatility_7d` =
  desvío de 7 log-retornos de los últimos 8 cierres diarios completos; `funding_interval_hours` = 8 si el
  símbolo no aparece en `fundingInfo`.
- **Backfill offline**: `python scripts/market_features_backfill.py [--days 30] [--symbols BTC,ETH]` → 
  `cache/binance/market_features.csv` (fusiona con el CSV previo: correrlo cada <30 d acumula historia).
  Basis desde cierres de velas 1h (no mids); sin tasa estimada/intervalo/cap (no hay historia por API).
- **Historia larga**: data.binance.vision publica `futures/um/daily/metrics/<SYM>` (OI, top trader
  long/short, taker vol ratio cada 5 min) **desde 2020** → para la Fase 3 no hace falta esperar a acumular.
- Tests: `tests/test_market_features.py`.

El historial de funding por candidato se cachea hasta el próximo settlement del par (máx. 1 h, `exchanges/manager.py:fetch_funding_history`) → el scan horario casi no gasta llamadas. Los intervalos de funding de Binance se refrescan cada 15 min (`FUNDING_INTERVALS_TTL`).

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
solo — esta adopción fue manual tras revisar el reporte. `scripts/scoring_optimizer.py:BASELINE_PARAMS`
ya está en v11.0 (evaluado con `parametric_score_candidate`; v10.6 queda como `BASELINE_V106_PARAMS`).

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

**Estimador actual (desde 2026-09-13):** `HistGradientBoostingRegressor` (31 hojas,
300 iter, lr 0.05, `min_samples_leaf=200`) entrenado con **todas** las filas,
reemplazando al GBR 300×depth3 sobre 200k. Elegido en
`reports/ml_experiments_20260913.md` (10 candidatos, mismo walk-forward): uplift IC
+0.126 vs +0.112, Δtop10 +4.9 vs +4.1, ~7× más rápido. Peso por recencia, huber y
GBR más profundo no aportaron. **Calibración** con las predicciones de los últimos
14 d (`CALIB_WINDOW_DAYS`): con todo el train los scores fuera de muestra salían
inflados (mediana ~74, 55% ≥70).

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
| `scripts/ml_experiments.py` | **Local**: compara variantes de estimador con el mismo walk-forward (cachea features en `cache/ml_exp_*`). No exporta. |
| `models/scoring_model.joblib` | Artefacto commiteado (`{model, calibration_pcts, feature_names, model_version, train_window, val_metrics}`). Viaja con el deploy (no en `.gitignore`). |

### Flujo en el scan (`analysis/arbitrage.py`)

En `_analyze_spot_perp` y `_analyze_cross_exchange`, tras `opportunity_score`
(que deja los indicadores en `params["_indicators"]`),
`ArbitrageScanner._resolve_score(sc, params)` consulta el modelo **solo si
`mode == "spot_perp"`** (desde 2026-09-13: el modelo se entrena con series de tasa
única y en cross/defi no hay label válido — IC en vivo −0.105; esos modos usan el
heurístico con `model_prediction=None`). Si predice,
`score = model_score` (calibrado 0–100 vía percentiles de las predicciones de los últimos 14 d de train),
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

0. (Desde 2026-10-25) `python scripts/binance_dataset.py` (incremental) + `python -u scripts/ml_v2.py --refresh` (universo **CEX ∪ Alpha**, label H30, fee por venue) → gate = cartera WF **y** hold-out vs modelo vigente. `ml_binance.py`/`exit_policy_study.py` solo miran spot CEX: no usarlos como gate (ver "Auditoría 2026-10-25").

1. `pip install -r requirements-dev.txt` (local).
1b. `python scripts/ml_health.py --refresh-cache` → salud del modelo VIVO fuera de
   muestra (filas posteriores a su train): IC modelo vs heurístico con CI bootstrap,
   label realista (sale tras el primer pago ≤0), IC semanal, PSI, calibración y PnL
   de `user_history`. Read-only → `reports/ml_health_YYYYMMDD.md`. Es la validación
   en vivo confiable: la de `score_snapshots` tiene sesgo de supervivencia (recorte
   a 30 filas/par).
2. `python -u scripts/ml_train.py --refresh-cache` → re-descarga 90d de snapshots,
   valida el modelo vivo contra el net_apr real de sus predicciones de ≥14d atrás
   (IC principal solo `spot_perp`; cross se desglosa aparte porque su label offline
   no aplica), entrena uno nuevo (HistGB sobre 90d), walk-forward vs heurístico
   v11.0, calibra el score, exporta `models/scoring_model.joblib`. Tarda ~15 min.
3. Revisar `reports/ml_train_YYYYMMDD.md`: ¿el IC en vivo se sostuvo? ¿el nuevo
   modelo bate al heurístico en walk-forward (uplift>0 en todos los folds, medio
   ≥0.05, σ≤0.05)?
4. Si convence: `git add models/scoring_model.joblib && commit && push` → Railway
   redeploya. Si no: investigar drift antes de promover.

**Rigor del walk-forward (desde 2026-10-09):** purging de `PURGE_DAYS=14` (el
label mira hasta 14d adelante; `--purge-days 0` reproduce el método viejo) y
series cortadas en huecos de recolección (`scripts/_scoring_data.py:split_on_gaps`).
El scanner es event-driven: **sin posiciones abiertas no escribe snapshots**
(hueco 2026-09-14→09-21), y sin el corte esos saltos se leían como pagos
consecutivos (~22% de filas afectadas). Ver `reports/ml_health_20261009.md`,
`reports/research_funding_ml_20261009.md`, `reports/business_profitability_20261009.md`.

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
- **KPI widgets** (`#earnings-kpis`): Hoy (con delta vs ayer), 7 días (con APR realizado), 30 días, Total (activas + cerradas all-time). Verde/rojo según signo. `frontend/site/static/app.js:loadDailyEarnings()` y `renderEarningsKpis()`.
- **Gráfica con toggle** (`#earnings-chart`): 
  - **Cumulativo activas** (default): líneas multi-color por posición sobre `payments_json[].cumulative`, ignora entries con `kind:"manual_adjust"` para no romper la curva. 
  - **Diario total**: bar chart sobre `series[]` del endpoint, una barra por día (verde si neto≥0, rojo si <0). Incluye cerradas.

Datos provistos por `GET /api/earnings/daily?days=30` → `DBPersistence.aggregate_daily_earnings()` en `core/db_persistence.py`. Bucketing por día local del servidor. Limitación documentada: `realized_apr_7d` usa `capital_in_use` actual como aproximación (no series histórica de capital).

## PnL neto real (posiciones auto)

**Neto = funding + PnL de precio − fees.** Antes `net_earned = funding − fees` e ignoraba el resultado de precio de las dos piernas (basis de entrada/salida, slippage, comisión del DEX). Caso #43 AGT: registrado −0.0325, real −0.0244 (precio +0.0081: basis entrada +0.2195 %, salida −0.1899 %).
- **Entrada** (`trade_executor._open_result`): `spot_avg_price_gross` = costo / cantidad **bruta** (la comisión cobrada en base ya va en `entry_fees_usd`, no se cuenta dos veces) → `user_positions.spot_entry_price`; `entry_price` = precio medio perp. `basis_pct` usa el bruto. `ref_mids` = mids al iniciar (`_ref_mids`; BSC: mid de cotizaciones chicas compra/venta, `BscSpotLeg.mid_price`) → `entry_slippage {spot, perp, total}` (USD, positivo = peor que el mid) → `entry_slippage_usd`.
- **Cierre** (`_close_pnl`): `price_pnl = (proceeds_spot − spot_entry·qty_spot) + (perp_entry·qty_perp − costo_recompra)`, incluyendo los fills de los seguros (SL / venta límite). Sin precio de entrada (manual/importada) → `None` (fallback de `spot_entry` al audit `auto_open`). `exec` = precios entrada/salida, `exit_basis_pct`, `exit_slippage`.
- **Persistencia**: parciales acumulan en `user_positions.realized_price_pnl`; `jobs.real_net_breakdown` suma todo → `user_history.price_pnl`, `slippage_usd` (entrada+salida, diagnóstico: ya está dentro del precio, no se resta aparte), `exec_json`. La edición manual (`PATCH /api/history/<id>/earnings`) recalcula con `price_pnl`. KPIs (`aggregate_daily_earnings`): `price_pnl` por día de cierre y en `all_time.net`.
- **Activas**: `/api/positions` expone `entry_basis_usd` (basis capturado) y suma `realized_price_pnl` al neto; el PnL de precio total se realiza al cerrar.
- **Backfill**: `python scripts/backfill_price_pnl.py [--user N]` (dry-run; `--apply` escribe, solo con las columnas ya migradas) reconstruye desde `execution_jobs`.

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

UI: `editPosEarnings(posId)` / `editHistEarnings(histId)` en `frontend/site/static/app.js`, mirror del patrón existente `editPosFees`.

---

## Binance en vivo: sync de cuenta + auto-ejecución (CEX y spot on-chain BSC)

Coloca y ejecuta las órdenes reales de ambas piernas con las API keys del usuario, y sincroniza saldos, posiciones y funding **reales**. **Trigger por botón** (semi-automático); la ejecución corre en **jobs en background**. Margen **aislado** en todas las posiciones; el **leverage lo elige el usuario** por posición (basyo solo valida el máximo del símbolo).

### Hallazgos de la documentación de Binance (oct-2026)
- **Alpha API = solo market data** (`/bapi/defi/v1/public/alpha-trade/*`, token list pública). No existe API para operar Alpha ni la Web3 Wallet MPC del usuario; ccxt no soporta Alpha.
- **Binance Wallet Web3 API** (`https://web3.binance.com/build`): agregador DEX para wallets self-custody (quote → swap → firmar local → broadcast). HMAC base64 sobre `ts + METHOD + "/build" + path?query + body`. ⚠ **Bloquea por región del servidor** (US, CA, NL, GB…, `40301`): con el bot en Railway US/Ámsterdam no funciona → mover el servicio a **asia-southeast (Singapur)** o usar `DEX_AGGREGATOR=0x`. El SDK Python v8.0.0 manda los POST como query (bug); aquí se usa body JSON.
- Multi-Assets solo admite cruzado; Portfolio Margin usa `papi` (no soportado → bloqueado con guía).
- ccxt dejó el sandbox de futuros de Binance: `TRADE_SANDBOX=1` usa **demo trading** (`enable_demo_trading`), sin sapi.

### Metodología (`exchanges/trade_executor.py`)
- **spot_perp pareado, delta-neutral en todo momento**: pierna spot **maker** (`LIMIT_MAKER` al mejor bid, re-precio solo si el libro se mueve) y **perp taker cubierto por cada fill** (`PerpHedger`) sobre la cantidad **neta** recibida (descuenta comisión en base; BNB no reduce la base). Taker fallback solo si `spot_ask ≤ perp_bid·(1+0.05%)` (apertura); en cierre el taker es incondicional.
- **TWAP pareado propio** (`twap={slices, duration_s}`): N tramos con la misma rutina; nunca dos TWAP nativos independientes (se desincronizan). Sugerencia automática en la simulación si nocional > 25% de la profundidad ±0.2% o > 5.000 USDT.
- **Fees reales** desde `fetch_my_trades(orderId)` convertidas a USD (USDT, BNB y base a mercado).
- **Cierre por cantidades persistidas** (`spot_qty`/`perp_qty`), nunca `exposure/precio`; capado por saldo libre y short real. Posiciones sin cantidades → no se cierran en auto.
- **Preflight Binance**: Multi-Assets → `code=MULTI_ASSETS`; leverage > máximo (`leverageBracket`) → error; aislado + leverage verificados con `/fapi/v1/symbolConfig` (funciona sin posición). Hedge mode: ccxt traduce `hedged` → `positionSide`.
- **Idempotencia**: `clientOrderId = bsy{job}-{leg}{n}`. `reconcile_spot_perp` recalcula lo ejecutado desde el exchange y re-cubre el perp hasta igualar el spot.
- `cross_exchange`: límites en ambas piernas con polling **conjunto**; el exceso de la pierna más llena se deshace (posición = cantidad emparejada).
- Liquidación estimada short aislado: `entry·(1 + 1/L − mmr)`; advertencia (no bloqueo) si distancia < 33%.

### Jobs en background (`execution/jobs.py`, tabla `execution_jobs`)
`POST /api/execute_open|close` (no dry-run) crea un job y devuelve `job_id`; un thread por job ejecuta y guarda la posición con fills reales (`source='auto'`, `job_id`, `spot_qty`, `perp_qty`, `spot_venue`, `entry_basis_pct`, `entry_mode`). Máx. 1 job activo por usuario; cancelable (`/api/exec/jobs/<id>/cancel` → termina cubriendo). Al arrancar, los jobs `running` se **reconcilian** (CEX) y quedan `interrupted` con alerta; no se reanuda un TWAP a ciegas. Fallos con piernas llenas → alerta `EXEC_FAILURE`; éxito → `EXEC_DONE`.

### Sync de cuenta real (`execution/account_sync.py`, `exchanges/binance_account.py`)
Cada 5 min (y bajo demanda) por usuario con keys de Binance → `user_account_snapshots`:
- permisos de la key (`apiRestrictions`): **se rechazan keys con retiros habilitados** al guardar; avisos si falta IP whitelist/permisos.
- modo de cuenta (PM, Multi-Assets, hedge), saldos spot/USDⓈ-M, posiciones con **liquidación real**, **importables** y **drift** (alerta `POSITION_DRIFT`, dedup diaria).
- **Funding real**: posiciones Binance `auto`/`imported` toman `earned_real` del income `FUNDING_FEE` (`earnings_source='real'`); el monitor deja de estimarlas.
- **Capital real**: con snapshot, `total = USDT libre (spot + futuros disponible + wallet BSC) + capital en posiciones`; el campo manual se oculta/ignora (sin cuenta conectada sigue el manual).
- Una posición basyo por símbolo en Binance. Transfer automático spot↔futuros opcional (`user_configs.auto_transfer`, requiere "Universal Transfer").

### Spot on-chain BSC (`exchanges/onchain_bsc.py`, `exchanges/dex_aggregator.py`)
Para tokens cuyo spot solo existe en Alpha (detectado con la token list pública, chainId 56, liquidez ≥ $200k). Wallet **dedicada** del usuario (`user_wallets`, private key cifrada fail-closed). `BscSpotLeg`: quote → guardas (impacto ≤ 1%, precio ≤ perp_bid·1.005) → approve acotado → swap (tx tipo 2/legacy firmada con eth-account; **gas = máx(eth_estimateGas×1.3, gas del agregador)** — el del agregador se quedó corto en rutas multi-hop y el swap revirtió por out-of-gas el 2026-10-18; la estimación simula y aborta antes de transmitir si revertiría) → broadcast (MEV protection) → receipt → cantidad real = delta de `balanceOf` → hedge perp. Flag `ONCHAIN_ENABLED=1`. Reconciliación automática tras reinicio: **no** (on-chain queda `interrupted` para revisión).

**Agregador con fallback** (`exchanges/dex_aggregator.py:build_aggregator`): `FallbackAggregator([primario, KyberSwap])`. Si el primario (Binance Web3) responde bloqueo de región/compliance (`40301/40302/40304`) pasa a **KyberSwap Aggregator** (sin API key, `aggregator-api.kyberswap.com/bsc`) y recuerda el bloqueo 1 h; Kyber no tiene broadcast propio → la tx sale por el RPC BSC. Sin keys Web3 → Kyber directo. `DEX_AGGREGATOR=kyber` lo fuerza. Motivo: con el bot en Amsterdam, el cierre de la posición #43 (AGT) falló con `Web3 API 40304: Service not available due to compliance restriction` (job #4, 2026-10-18). Un cierre que no ejecuta nada ahora queda `failed` + alerta (antes `done` silencioso).

### Seguros automáticos spot_perp (SL + venta límite)
Al abrir por API (`jobs._run_open` → `trade_executor.place_protection`), solo Binance:
- **Precio**: `sl = entry + 0.80·(liq − entry)` (`PROTECTION_LIQ_FRACTION`, desde 2026-10-25; antes 0.75); la venta spot va a `sl·(1−δ)` con `PROTECTION_SPOT_OFFSET` δ = 0.5 % (`protection_json.spot_price`); `liq` = `liquidationPrice` real de `fetch_positions`, fallback `liquidation_estimate`.
- **Perp**: `STOP_MARKET` buy `closePosition=true`, `workingType=MARK_PRICE` (hedge: `positionSide=SHORT`). ccxt 4.5.85 lo enruta a la **Algo Order API** (`/fapi/v1/algoOrder`); consultar/cancelar con `params={"trigger": True}`; al dispararse `info.actualOrderId` = orden de mercado real.
- **Spot CEX**: `LIMIT` sell GTC por `spot_qty` a `spot_price` (bloquea la base libre).
- **Spot BSC**: orden límite **KyberSwap Limit Order** (`exchanges/kyber_limit_order.py`): sign-message → approve del contrato DSLO → firma EIP-712 local → `POST /write/api/v1/orders`; expira a **7 días**, cancelación gasless.
- Persistido en `user_positions.protection_json` (`status`, `sl_price`, `liq_price`, `perp{id,status}`, `spot{venue,id,status,qty,expires_at}`, `errors`). Fallo → la posición queda abierta + alerta CRITICAL "Seguros NO colocados" (`status=failed`, badge "SIN SEGUROS").
- **Cierre**: `_close_spot_perp` cancela los seguros ANTES de leer el saldo libre; lo que ya llenaron suma a `exit_fees_usd` (`protection_filled`). Si el cierre falla/parcial, `jobs._restore_protection` los vuelve a poner sobre lo que queda.
- **Lecciones de AGT #44 (2026-10-23)**: el SL cerró el short en un pico y el job del vigilante no vendió el spot BSC porque Kyber marcaba un impacto de 9.86 % (sale de su oráculo USD, no del pool; el real era ~0 %). El reintento abortó al intentar cancelar una venta límite que ya estaba cancelada. Cambios:
  - (a) `_cancel_bsc_limit` es idempotente: lee el estado primero; `cancelled`/`expired`/`cancelling` no cuentan como vivas, y cuenta los llenados parciales.
  - (b) `BscSpotLeg._impact_vs_mid` compara el precio efectivo contra el mid on-chain (cotizaciones chicas); Kyber `swap_tx` ya no aborta por su oráculo.
  - (c) Con el short ya cerrado (spot desnudo), el límite de impacto de la venta sube a `MAX_PRICE_IMPACT_EXIT` = 5 %.
  - (d) Si el cierre falla, `_apply_partial_close` igual registra lo que llenaron los seguros (cantidades, `realized_price_pnl`, fees) y marca esas piernas `filled`/`canceled` en `protection_json`.
- **Lecciones de JCT #46 (2026-10-24)**: el usuario canceló el SL de basyo en la web, se llenó la venta límite BSC (+438 USD de pierna spot) y el reintento del cierre falló vendiendo 1e-11 de remanente ("Return amount is not enough") → el short quedó abierto y sin SL; el usuario lo recompró a mano (−380.9) y el cierre siguiente ("Short real 0") registró solo la pierna spot (`price_pnl` +438 vs real +57.5). Cambios:
  - (a) Un short que falta sin explicarlo los seguros (`perp_qty − short real − SL llenado` > 1 %) se busca en los fills reales (`external_perp_buyback`: userTrades del perp desde la apertura, compras más recientes, ventanas de 7 d) y entra al `price_pnl` y a las fees (`perp_external`). Si no se encuentra → `price_pnl_unknown`, alerta WARNING y el `price_pnl` total del historial queda `None` (nunca de una sola pierna; el flag persiste en `payments_json`).
  - (b) Un remanente spot de polvo se descarta ANTES de vender: el perp se recompra igual.
  - Corrección de filas viejas: `python scripts/fix_external_buyback.py --hist 46 [--apply]` (dry-run por defecto).
- **Vigilante** (`execution/protection.py`, cada 60 s, lo arranca `JobRunner.start`): si se llenó el SL o la venta → lanza un job de cierre normal (`trigger:"protection"`) que cierra la otra pierna; orden BSC a <12 h de expirar → la renueva + Telegram `PROTECTION_RENEWED`; seguro cancelado fuera de basyo → `PROTECTION_CANCELED`. Con el mark a < 2 % del SL (`HOT_PCT`) el loop baja a 10 s.
- **Llenados asimétricos (2026-10-25, `scripts/protection_study.py`)** — velas 1m, 12 meses: CEX = 20 alts con spot Binance (`reports/protection_study_20261025.md`); BSC = 20 tokens solo-Alpha con el precio on-chain de la API pública de Binance Alpha (`/bapi/defi/v1/public/alpha-trade/klines`, `--venue bsc`, `reports/protection_study_20261025_bsc.md`):
  - **Caso A** (dispara el SL, el spot no se vende → spot desnudo): es la cola peligrosa y **on-chain es frecuente** (2x: 1117 eventos en 6022 entradas vs 25 en CEX; el precio on-chain va detrás del perp). δ = 0.5 % la reduce (BSC 1117 → 408, CEX 25 → 7) y es el mejor esperado en ambos. En BSC el esperado neto por evento de seguro es negativo (≈ −0.25 % de la entrada a 2–3x) vs positivo en CEX. El cierre vende el spot **taker directo** (sin ventana maker) cuando el short ya no existe.
  - **Caso B** (se vende el spot, el SL no): en media se gana (el spot hace mecha sobre el perp). **Short suelto** (`protection_json.status = "runner"`): si la venta spot se llenó **completa**, el vigilante registra la venta (`_apply_partial_close`, spot_qty → 0, `realized_price_pnl`) y deja el short con el SL duro + `TAKE_PROFIT_MARKET` closePosition a −caída del precio de venta (`place_runner_tp`, pierna `tp`); si no toca en el tiempo máximo → cierre normal (`triggered_by: runner_timeout`). Regla por venue (`RUNNER_RULES`): **CEX −5 % / 15 min**, **BSC −20 % / 1 h** (2x: +2.56 vs +1.89 recomprar ya; en CEX esa regla rinde peor). Verificado sin enviar órdenes: ccxt manda el TP a `/fapi/v1/algoOrder` (`TAKE_PROFIT_MARKET`, `closePosition`, `CONDITIONAL`) igual que el SL. Llenado parcial o fallo al poner el TP → cierre inmediato como antes. Alerta `SPOT_TP_RUNNER`; badge "SHORT SUELTO".
  - Descartado: trailing stop (peor en CEX, neutro en BSC). En CEX la mejora del runner es marginal (+0.1–0.2 pp por evento B).
- Solo posiciones nuevas (las previas al cambio no tienen seguros).

### Rutas
`/api/execute_open` (`{opportunity_id, capital, leverage, dry_run?, twap?, allow_market_fallback?}`), `/api/execute_close` (`{position_id, dry_run?, twap?}`, solo `auto`/`imported`), `/api/exec/jobs/<id>`, `/api/exec/jobs/active`, `/api/exec/jobs/<id>/cancel`, `/api/account/binance` (snapshot), `/api/account/binance/sync`, `/api/account/binance/multi_assets_off` (`{confirm:true}`), `/api/account/binance/import` (`{symbol}`), `/api/account/settings` (`{auto_transfer}`), `/api/account/wallet` (GET/POST `{private_key, accept_risk:true}`/DELETE).

### Frontend
Modal de ejecución con TWAP, simulación con leverage/liquidación/basis/fondos por wallet/sugerencia TWAP, progreso del job en vivo (reanuda al recargar). Posiciones: badges AUTO/IMPORTADA/BSC/FUNDING REAL, short/liq real, drift. Cuenta: panel Binance (saldos, modo, desactivar Multi-Assets, importar, transfer automático, sincronizar) y wallet BSC.

### Verificación
- `pytest tests/` (exchange y agregador simulados + jobs sobre SQLite).
- `python scripts/binance_sync_check.py` — chequeo **solo lectura** con `BINANCE_USER_API_KEY/SECRET` en `.env`.
- Dinero real: lo ejecuta el usuario desde la UI con montos mínimos.

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

**Tipos de alerta:** `CLOSE_SUGGESTED` (dos settlements seguidos sin cobrar: último pago ≤0 y el próximo también), `RATE_REVERSAL` (informativa, una vez por posición), `NAKED_SPOT` (crítica: short desaparecido con spot en manos), `LIQUIDATION_RISK` (short a <20% de liquidación), `PRE_PAYMENT_UNFAVORABLE`, `SL_TP_REVIEW`, `EXCEPTIONAL_OPPORTUNITY`, `SWITCH_OPPORTUNITY`, `POSITION_CLOSED`, `SPOT_TP_RUNNER` (venta spot llenada: short suelto con TP; CEX −5 %/15 min, BSC −20 %/1 h), `EXCHANGE_GEOBLOCKED` (crítica, diaria por usuario: el sync de Binance recibe 451 geográfico).

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
Oportunidades (`frontend/site/app.html`, `frontend/site/static/app.js`), aplicados **en vivo
client-side** sobre la lista completa que devuelve el backend, válidos tanto para
CEX como DeFi. Antes estaban dispersos: los inputs APR/Score del toolbar estaban
muertos (disparaban un reload pero su valor nunca se leía) y los reales vivían en
la pestaña Config filtrando server-side.

- `frontend/site/static/app.js:sortAndFilter()` filtra por `apr` / `score` / `estimated_hold_days`
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

**Bajo demanda** (desde 2026-10-25; antes corría en cada scan sobre el top-5). Botón "Analizar con IA"
por oportunidad y por posición. **El veredicto es determinista; Gemini 3.1 Flash-Lite solo lo explica**
(`gemini-3.1-flash-lite`, `google-genai`). Motivo: las reglas fijas del prompt viejo (`z>=2 → EVITAR`)
contradecían al modelo — los picos Alpha rentables tienen z alto — y casi siempre salía EVITAR.

- **Oportunidad** (`POST /api/opportunities/<id>/ai`, body `{capital}`, caché 30 min por settlement):
  `opportunity_verdict` → `ENTRAR` (Net APR predicho ≥ `ENTER_NAPR`=5), `ESPERAR` (0–5), `NO ENTRAR`
  (≤0), `SIN MODELO`. Devuelve `facts` con tabla por leverage 1/2/3x (`roc` sobre capital = napr·L/(L+1),
  `usd_d`, `liq`) y `lev_rec` (Alpha 2x, CEX 3x, `RECOMMENDED_LEV`); Gemini agrega `analysis`, `risks`,
  `watch`, `leverage`.
- **Posición** (`POST /api/positions/ai`, body opcional `{position_id}`): `position_verdict` → `CERRAR`
  (dos pagos seguidos ≤0 o `switch_analyzer` = SWITCH), `VIGILAR` (último pago ≤0 o >30 d), `MANTENER`.
  El LLM no puede cambiar la señal (se sobrescribe tras parsear).
- Sin API key o ante 429/timeout se devuelve el veredicto con su razón (nunca vacío). 3 keys rotadas
  (`GEMINI_API_KEY_1/2/3`).

---

## Variables de entorno requeridas

```bash
DATABASE_URL          # PostgreSQL URI (Railway: PGSQL_URL)
SECRET_KEY            # Flask session secret
FERNET_KEY            # Cifrado Fernet (generar: python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())")
GEMINI_API_KEY_1      # Gemini 3.1 Flash-Lite (gratuito en aistudio.google.com)
GEMINI_API_KEY_2      # (opcional, rotación)
GEMINI_API_KEY_3      # (opcional, rotación)

# CEX API keys (opcional — sin ellas funciona con datos públicos)
BINANCE_API_KEY / BINANCE_API_SECRET
BYBIT_API_KEY / BYBIT_API_SECRET
OKX_API_KEY / OKX_API_SECRET / OKX_PASSPHRASE
BITGET_API_KEY / BITGET_API_SECRET

# Opcionales
COINGLASS_API_KEY     # Datos adicionales de mercado
ENABLED_EXCHANGES     # Default: binance (foco Binance desde 2026-10-22; otros CEX por env)
ARBITRAGE_MODES       # Default: spot_perp (cross_exchange por env; su auto-ejecución está bloqueada)
DEFI_ENABLED          # =1 → reactiva el scan DeFi (default apagado)
MAX_LEVERAGE          # tope de leverage de /api/execute_open (default 3; decisión 2026-10-25)
AUTO_UNWIND_ON_LIQ    # =1 → si el short desaparece (liquidado) el vigilante vende el spot (default: solo alerta CRITICAL)
TRADE_SANDBOX         # =1 → auto-ejecución en test (Binance: demo trading; resto: set_sandbox_mode)
ONCHAIN_ENABLED       # =1 → habilita la pierna spot on-chain BSC (tokens solo-Alpha)
DEX_AGGREGATOR        # binance (default, Web3 API, fallback auto a KyberSwap) | 0x | kyber
BINANCE_WEB3_API_KEY / BINANCE_WEB3_API_SECRET   # web3.binance.com/en/dev-portal (permisos Trading + Transaction)
ZEROX_API_KEY         # si DEX_AGGREGATOR=0x
BSC_RPC_URL           # default https://bsc-dataseed.bnbchain.org
BSC_MEV_PROTECTION    # default 1 (broadcast por mempool privada)
EXCHANGE_PROXY_URL    # socks5h://user:pass@ip:puerto — salida a exchanges vía VPS (geobloqueo); vacío = directo
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
- **Gate de rotación con modelo** (`rotation_gain_ok`, desde 2026-10-25): SWITCH solo si
  (napr_alt − napr_actual_ajustado)·14 d > fee de entrada de la alternativa + 2 % del nocional
  (`ROTATION_MARGIN`). Backtest `reports/rotation_policy_20261025.md`: margen 0 → 28.0 % anual (pierde
  por fees), sin rotar 29.3 %, margen 2 % → 34.6 %. Con modelo no aplica el atajo por `health_score < 30`.

---

## Arquitectura de rutas

| URL | Auth | Template / handler |
|-----|------|--------------------|
| `GET /` | Pública | **web (Caddy)** → `frontend/site/index.html` (landing + modal login/registro) |
| `GET /app` | Pública (gate en cliente) | **web (Caddy)** → `frontend/site/app.html`; `app.js:loadMe()` llama `/auth/me` y si 401 → `/?login=1` |
| `GET /terms`, `/privacy`, `/<otra-ruta-SPA>` | Pública | **web (Caddy)** `try_files {path} /index.html` — react-router monta la página en cliente |
| `POST /auth/login` | Pública | JSON `{ok, msg}` — Flask-Login cookie |
| `POST /auth/register` | Pública | JSON — requiere `terms_accepted: true` en body; setea `users.terms_accepted_at` |
| `POST /auth/logout` | Autenticado | JSON — redirigir a `/` tras logout |
| `GET /auth/me` | Autenticado | JSON `{ok, user}` — devuelve 401 JSON si no auth |
| `GET /auth/page` | Pública | `301 → /?login=1` (compat deep-links) |
| `GET /health` | Pública | JSON (healthcheck Railway). **503** si `USE_DB` y la DB no inicializó → Railway no promueve el deploy |
| `GET /api/*` | `@auth_required` | JSON — 401 JSON si no auth |
| `GET /api/opportunities` | `@auth_required` | Lista **completa** (sin filtrar para display); los filtros se aplican client-side en el panel Filtros. Recalcula `mins_to_next` y enriquece con `score_trend`. |
| `GET /api/earnings/daily?days=N` | `@auth_required` | Rollup diario combinando activas + cerradas: `{today, yesterday, last_7d, last_30d, all_time, realized_apr_7d, series:[{date, earned, fees, net}]}` |
| `PATCH /api/positions/<id>/earnings` | `@auth_required` | Body `{earned: float}` — override absoluto reseteable: setea `earned_real` + `last_earn_update=now`, agrega entry `kind:"manual_adjust"` a `payments_json`. Próximos settlements se acumulan encima. |
| `PATCH /api/history/<id>/earnings` | `@auth_required` | Body `{earned?, fees?}` — edita posición cerrada, recalcula `net_earned`, registra en `notes`. |
| `POST /api/account/exchange_keys/test` | `@auth_required` | Body `{exchange}` — valida las API keys guardadas vía `fetch_balance`. Devuelve `{ok, msg, usdt_balance?}`. |
| `POST /api/execute_open` | `@auth_required` | Body `{opportunity_id, capital, leverage?, dry_run?}` — coloca las órdenes reales (CEX only) vía `trade_executor`, guarda la posición con `auto_executed=True` y fills reales. `dry_run:true` simula sin enviar. |
| `POST /api/execute_close` | `@auth_required` | Body `{position_id, dry_run?}` — coloca las órdenes inversas a market y cierra con `exit_fees_real` reales. |

**Flujo unauthenticated:** `GET /app` (estático) → `loadMe()` recibe 401 de `/auth/me` → `location = '/?login=1'` → landing abre modal login.  
**Flujo logout:** `POST /auth/logout` → `window.location = '/'` (en `frontend/site/static/app.js:doLogout`).  
**Flujo delete-account:** `DELETE /api/account` → éxito → `window.location = '/'`.

### Páginas legales (Términos y Privacidad)

`/terms` y `/privacy` son rutas del SPA React (no de Flask). Caddy las atrapa con `try_files {path} /index.html` y sirve la landing; react-router monta `<TermsOfService />` o `<PrivacyPolicy />` desde `src/pages/`. El layout legal compartido vive en `src/components/legal/LegalLayout.tsx`.

El registro en `/auth/register` exige `terms_accepted: true` en el body o devuelve 400. Persistimos la marca de tiempo en `users.terms_accepted_at` para auditoría legal. El frontend muestra un checkbox en `AuthModal.tsx` (tab Registro) con links a `/terms` y `/privacy` en `target="_blank"`.

Para editar los textos legales: modificar `src/pages/TermsOfService.tsx` y `src/pages/PrivacyPolicy.tsx` en basyo, recompilar.

### Cómo editar la landing

El `frontend/site/index.html` es un shell de 28 líneas — solo meta tags y el `<div id="root">`. El contenido visible está en el bundle JS (`frontend/site/static/landing/assets/index-*.js`).

**Cambios editables directamente:**
- Meta tags SEO (título, description, og:image) → `frontend/site/index.html`
- Favicon → reemplazar `frontend/site/static/landing/favicon.png`

**Cambios de contenido (textos, secciones, colores, CTAs)** requieren recompilar el source React:
1. El source está en `/home/user/basyo` (clonado en el sandbox, rama `claude/integrate-landing-auth`).
2. Editar los `.tsx` correspondientes.
3. `cd /home/user/basyo && npm run build`
4. Copiar output:
   - `dist/index.html` → `frontend/site/index.html`
   - `dist/assets/*` → `frontend/site/static/landing/assets/`
5. Commit + push en `funding_rate_bot` → Railway redeploya `web` (y el bot).

El repo [`Jeftewan/basyo`](https://github.com/Jeftewan/basyo) en GitHub está **deprecado** — ya no se usa para deploy. El source de referencia vive en el sandbox.

---

## Despliegue Railway (proyecto `practical-magic`)

| Servicio | Fuente | Rol |
|---|---|---|
| `FUNDING_RATE_BOT` | repo raíz, Railpack + `Procfile` | API Flask + scanner. Healthcheck `/health`. Sin dominio público necesario (lo consume `web` por red privada). |
| `web` | `rootDirectory=/frontend`, `Dockerfile` | Caddy en `basyo.app`. Healthcheck `/`. Var `BACKEND_URL=http://${{FUNDING_RATE_BOT.RAILWAY_PRIVATE_DOMAIN}}:${{FUNDING_RATE_BOT.PORT}}`. |
| `Postgres` | plugin | `DATABASE_URL` |

- Healthcheck y root dir viven en la **configuración del servicio** (no en `railway.json`: Railway deprecó Config as Code el 2026-12-01).
- **Sin watch paths**: con ellos Railway marcó el merge del PR #98 como `SKIPPED` ("No changes to watched files") en ambos servicios. Todo push a `main` redeploya los dos (Caddy tarda segundos).
- Bot: `PORT=8080` (lo referencia `web`) y `MALLOC_ARENA_MAX=2` (glibc + threads fragmenta; RAM base era ~0.78 GB).
- **Salida a exchanges (proxy)**: desde el deploy del 2026-10-18 01:05 UTC (PR #102, coincidencia: el código no influye) la IP de salida de Railway (asia-southeast1) recibe **451** de Binance ("restricted location") y **403 CloudFront** de Bybit. **Causa raíz:** desde ese deploy la región Singapur sale por el bloque propio de Railway `152.55.188.0/22` (AS400940), que ipinfo/ip-api geolocalizan en **San Francisco, US** (mal geolocalizado); el último deploy OK fue el de PR #101. Geolocalización de los prefijos de AS400940 (muestreo 2026-10-18): `208.77.246.0/23` → SG, `152.55.184.0/22` y `208.77.244.0/23` → **NL**, `152.55.176.0/22`/`152.55.180.0/22`/resto → US. **Estado actual: el bot corre en EU West (Amsterdam)** (`railway scale eu-west=1 southeast-asia=0`): sale por `152.55.184.x` (NL) y Binance/Bybit responden 200 (scan 3017 pares). Postgres y `web` están en us-west2, así que la latencia a DB no empeora. ⚠ La Binance **Web3 API** (on-chain BSC, `ONCHAIN_ENABLED=1` en prod) bloquea NL → en Amsterdam el spot on-chain no funciona hasta volver a SG o usar `DEX_AGGREGATOR=0x`. Ticket abierto a Railway para corregir el geofeed de `152.55.188.0/22`: https://station.railway.com/questions/southeast-asia-egress-ips-152-55-188-0-5c5acecd (el bloque NL tuvo el mismo error hace ~5 meses y lo corrigieron). Cuando ipinfo muestre SG para `152.55.188.x`, se puede volver a `southeast-asia=1` y verificar el log `Egress exchanges`. IPv6 no aplica (Binance/Bybit sin AAAA). Static Outbound IPs (plan Pro) solo sirven si sus IPs geolocalizan fuera de US — verificarlo antes de redeployar. Detección: `exchanges/net.py` (`is_geoblock_error`, `geoblocked()`), `log.error` si el país de salida está en `BLOCKED_COUNTRIES`, `/health → egress {country, proxy, geoblocked}` y alerta `EXCHANGE_GEOBLOCKED` desde el sync. Solución: proxy SOCKS5 propio (`gost`) en un VPS de país permitido (`scripts/proxy_vps_setup.sh`) + `EXCHANGE_PROXY_URL`. `exchanges/net.py` lo aplica **solo** a ccxt (`manager.py`, `trade_executor._build_client` → también `binance_account`), Web3 API/0x y la token list Alpha; Postgres/Telegram/Gemini/DeFi/RPC BSC salen directo (no usar `HTTPS_PROXY` global). Al arrancar se loguea `Egress exchanges: <país> <ip> via ...`. Diagnóstico: `python scripts/egress_check.py` (con/sin proxy). La IP del VPS es la de la whitelist de las API keys.
- RAM ccxt: las instancias de solo lectura de `exchanges/manager.py` cargan solo spot + linear/swap y descartan `market['info']` (`_ensure_markets`); el spot check reusa esos mercados. `trade_executor` usa clientes propios, no afectados.
- CLI: `railway ... -p 7dc7767e-983c-45a6-afc0-be5210dee632 -e production -s <servicio>` (el repo no está linkeado).

---

## Pendiente / Lo que falta

### Funcionalidad incompleta (infraestructura lista, falta conectar)

- **Auto-trading CEX**: ✅ implementado (ver sección "Binance en vivo"). Botón "Ejecutar" por oportunidad + "Cerrar (auto)" por posición colocan órdenes reales vía las API keys del usuario. Spot de tokens solo-Alpha vía wallet BSC dedicada (`ONCHAIN_ENABLED`). **Pendiente**: auto-trading DeFi (Hyperliquid/GMX/Aster/Lighter/Extended) y Bitget Onchain — siguen en flujo manual.
- **Coinglass**: cliente existe en `coinglass/client.py`, no integrado en el flujo principal.

### Mejoras identificadas

- **Binance en vivo — pendientes**: (1) Web3 API bloquea regiones US/NL/GB → hoy el bot está en Amsterdam (NL) por el geobloqueo de Singapur: on-chain requiere volver a SG cuando Railway corrija `152.55.188.0/22` o usar 0x (validar con un swap mínimo); (2) actualizar Términos/Privacidad (custodia de la wallet dedicada) en el source React de la landing; (3) reconciliación automática de jobs on-chain interrumpidos; (4) Portfolio Margin (`papi`) no soportado; (5) IP fija de salida → resuelta con el VPS proxy (`EXCHANGE_PROXY_URL`); (6) `cross_exchange` sigue con límites simples (sin TWAP).

- **DeFi history fiabilidad**: `fetch_funding_history` en `defi_manager.py` depende de que los snapshots existan en DB. En deploys nuevos o símbolos nuevos, thin-history (<5 muestras) da scoring neutro — correcto pero subóptimo para toma de decisiones. Nota: GMX, Aster, Lighter y Extended reportan `volume_24h=0`; el gate de volumen del scan trata ese `0` (desconocido) como **pasa**, así que estos pares ya no quedan excluidos (el filtro de volumen real es de display, client-side).
- **Scoring DeFi bajo (pendiente)**: los modos `cross_exchange`/`defi` puntúan bajo de forma estructural — consistency (46) + stability (21) = 67% del score se calculan sobre el *diferencial* (`period_diffs`), que cambia de signo mucho más que una tasa única → streak/consistencia bajos, CV alto. A esto se suma thin-history y `volume_24h=0`. **Pendiente** (otro plan): `defi_opportunity_score()` separado con umbrales heurísticos, tras verificar cuántos snapshots/muestras hay acumulados.
- **Alertas sin posiciones activas**: el scanner solo corre cuando hay posiciones abiertas o se fuerza manualmente. Si no hay posiciones, las oportunidades excepcionales no se escanean automáticamente.
- **Tests**: suite pytest solo para ejecución/sync (`tests/`); el resto depende del syntax check via `python -c "import ast; ast.parse(...)"`.
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
| `scripts/ml_train.py` | **Entrena/valida/exporta el modelo de producción** (loop de ~15d): valida predicciones previas, entrena HistGB, walk-forward, calibra, exporta `models/scoring_model.joblib`. NO despliega (imprime las instrucciones git). Ver sección "Scoring ML en producción". |
| `scripts/ml_experiments.py` | **Compara estimadores** (GBR/HistGB, hiperparámetros, recencia, loss) con el walk-forward de `ml_train` + diagnóstico de calibración. Output: `reports/ml_experiments_YYYYMMDD.md`. |

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
