# PLAN: Reestructuración Funding Rate Bot v8.0

## Filosofía del cambio
Eliminar la estrategia dual (safe/aggressive). El bot pasa a ser un **asistente de funding rate puro**: escanea, puntúa, presenta oportunidades ordenadas, el usuario decide en cuál entrar con cuánto capital, y el bot monitorea cada posición alineándose al horario de pago real de cada exchange.

---

## ARQUITECTURA GENERAL

```
┌─────────────────────────────────────────────────────────┐
│                    FRONTEND (SPA)                        │
│                                                          │
│  ┌──────────┐  ┌──────────────┐  ┌───────────────────┐  │
│  │ Settings │  │ Oportunidades│  │ Posiciones Activas │  │
│  │  Panel   │  │  (Tabla)     │  │    (Monitor)       │  │
│  └──────────┘  └──────────────┘  └───────────────────┘  │
│       │               │                    │             │
│  Config dinámica  "Entrar con $X"    Cerrar manual /    │
│  desde el front   en oportunidad     Ver ganancias      │
└───────────┬───────────┬────────────────┬─────────────────┘
            │           │                │
            ▼           ▼                ▼
┌─────────────────────────────────────────────────────────┐
│                     API REST (Flask)                     │
│                                                          │
│  GET  /api/config          POST /api/config              │
│  GET  /api/opportunities   POST /api/open_position       │
│  GET  /api/positions       POST /api/close_position      │
│  GET  /api/history         POST /api/force_scan          │
│  POST /api/test_email                                    │
└───────────┬───────────┬────────────────┬─────────────────┘
            │           │                │
            ▼           ▼                ▼
┌──────────────┐ ┌──────────────┐ ┌──────────────────────┐
│   Scanner    │ │  Portfolio   │ │   Notification       │
│   Worker     │ │  Manager     │ │   Engine (SMTP)      │
│              │ │              │ │                       │
│ - Fetch FR   │ │ - Positions  │ │ - Alerta de cierre   │
│ - Fetch Vol  │ │ - Earnings   │ │ - Resumen diario     │
│ - History    │ │ - History    │ │ - Tasa desfavorable   │
│ - Score      │ │ - P&L real   │ │                       │
└──────┬───────┘ └──────┬───────┘ └───────────────────────┘
       │                │
       ▼                ▼
┌─────────────────────────────────┐
│     Exchanges (CCXT)            │
│  Binance | Bybit | OKX | Bitget│
└─────────────────────────────────┘
```

---

## MÓDULOS Y CAMBIOS DETALLADOS

### 1. CONFIGURACIÓN DINÁMICA DESDE EL FRONTEND

**Archivo: `core/state.py`**
- Eliminar: `safe_pct`, `aggr_pct`, `reserve_pct`, `max_pos_safe`, `max_pos_aggr`, `min_apr_safe`, `min_apr_aggr`
- Agregar/mantener:
  ```python
  DEFAULT_STATE = {
      # --- Configuración editable desde el front ---
      "total_capital": 1000,          # Capital total disponible USD
      "scan_interval": 300,           # Segundos entre escaneos
      "min_volume": 1_000_000,        # Volumen 24h mínimo
      "min_apr": 10,                  # APR% mínimo para mostrar oportunidad
      "min_score": 40,                # Score mínimo (0-100)
      "min_stability_days": 3,        # Días mínimos que debe mantenerse estable
      "max_positions": 5,             # Máximo posiciones simultáneas
      "alert_minutes_before": 5,      # Minutos antes del pago para revisar tasa

      # --- Email / Notificaciones ---
      "email_enabled": False,
      "smtp_host": "smtp.gmail.com",
      "smtp_port": 587,
      "smtp_user": "",
      "smtp_password": "",
      "email_to": "",

      # --- Estado operativo (no editable) ---
      "positions": [],
      "history": [],
      "total_earned": 0,
      "scan_count": 0,
      "all_data": [],
      "opportunities": [],           # Lista unificada de oportunidades
      "status": "Iniciando...",
      "last_error": "",
      "last_scan_time": "—",
      "last_scan": 0,
      "alerts": [],
  }
  ```

**Archivo: `api/routes.py` — endpoint `GET/POST /api/config`**
- Ampliar campos editables para incluir: `total_capital`, `scan_interval`, `min_volume`, `min_apr`, `min_score`, `min_stability_days`, `max_positions`, `alert_minutes_before`, `email_enabled`, `smtp_host`, `smtp_port`, `smtp_user`, `smtp_password`, `email_to`
- Validación: tipos numéricos, rangos lógicos, email format básico
- Al cambiar `scan_interval`, reiniciar el timer del scanner

**Archivo: `templates/index.html` + `static/app.js`**
- Panel de Settings con todos los campos configurables
- Sección separada para configuración de email con botón "Test Email"
- Guardar con POST /api/config, feedback visual

---

### 2. SISTEMA UNIFICADO DE OPORTUNIDADES (eliminar safe/aggressive)

**Archivo: `scanner/worker.py` — `_run_scan()`**
- Eliminar `_analyze_safe()` y `_analyze_aggr()`
- Nuevo flujo unificado:
  1. Fetch funding rates de los 4 exchanges en paralelo
  2. Para cada token con FR positivo (carry positivo = spot long + perp short):
     a. Fetch historial 30 días
     b. Calcular score de estabilidad
     c. Calcular fees (entrada + salida)
     d. Calcular APR neto (descontando fees)
     e. Estimar si se mantiene rentable ≥3 días (basado en estabilidad)
     f. Calcular break-even
  3. Para oportunidades cross-exchange (diferencial entre exchanges):
     a. Mismo análisis de estabilidad y fees
  4. Unificar todo en una sola lista `opportunities`
  5. Ordenar por: score DESC (que ya pondera estabilidad, volumen, rentabilidad)
  6. Filtrar por `min_apr`, `min_score`, `min_volume`

**Archivo: `analysis/scoring.py`**
- Ajustar score para priorizar sostenibilidad ≥3 días:
  - **Estabilidad** (25pts): CV del historial + min_rate_ratio (SUBIR de 20 a 25)
  - **Consistencia/Streak** (20pts): Racha consecutiva favorable (SUBIR de 15 a 20)
  - **Volumen/Liquidez** (15pts): Mayor volumen = menor riesgo de cambio brusco (SUBIR de 10 a 15)
  - **Yield Diario** (20pts): APR neto después de fees
  - **Frecuencia** (10pts): Más pagos/día = mejor (BAJAR de 25 a 10)
  - **Tendencia** (10pts): Trend reciente vs histórico
- Agregar campo `estimated_hold_days`: días estimados que se mantendrá favorable
- Agregar campo `stability_grade`: A/B/C/D basado en CV

**Archivo: `analysis/arbitrage.py`**
- Mantener `scan_spot_perp_opportunities()` y `scan_cross_exchange_opportunities()`
- Agregar a cada oportunidad: `estimated_hold_days`, `stability_grade`, `net_apr` (después de fees)
- Agregar campo `daily_income_per_1k` ya calculado

**Archivo: `portfolio/actions.py`**
- ELIMINAR completamente el sistema de acciones automáticas (OPEN/EXIT/ROTATE/WAIT)
- Ya no se generan recomendaciones automáticas de abrir/cerrar
- Las oportunidades se presentan y el USUARIO decide

---

### 3. FLUJO DE APERTURA DE POSICIÓN

**Nuevo endpoint: `POST /api/open_position`**
```python
# Request body:
{
    "opportunity_id": "BTC_Binance_spot_perp",  # ID único de la oportunidad
    "capital": 500,                              # Cuánto capital asignar
}

# Lógica:
1. Validar que hay capital disponible (total_capital - capital_in_use >= capital)
2. Validar que no se excede max_positions
3. Buscar la oportunidad en state["opportunities"] por ID
4. Crear posición:
   {
       "id": uuid,
       "symbol": "BTC/USDT",
       "exchange": "Binance",
       "type": "spot_perp" | "cross_exchange",
       "long_exchange": "Bybit",      # solo para cross_exchange
       "short_exchange": "Binance",    # solo para cross_exchange
       "entry_fr": 0.0150,             # Funding rate al entrar
       "entry_price": 65000,
       "entry_time": timestamp_ms,
       "capital_used": 500,
       "interval_hours": 8,
       "payments": [],                 # Array de pagos reales recibidos
       "earned_real": 0,
       "estimated_earnings": 12.50,    # Ganancia estimada calculada al abrir
       "status": "active",
   }
5. Devolver instrucciones paso a paso para ejecutar manualmente:
   - "1. Compra $250 de BTC spot en Binance"
   - "2. Abre short de $250 en BTC/USDT perpetuo en Binance"
6. Guardar state
```

**Response incluye:**
```json
{
    "ok": true,
    "position": { ... },
    "steps": [
        "1. Compra $250 de BTC spot en Binance",
        "2. Abre short de $250 BTC/USDT perpetuo en Binance"
    ],
    "estimated_daily": 1.25,
    "estimated_3day": 3.75,
    "fees_total": 0.65,
    "break_even_hours": 12.5
}
```

---

### 4. MONITOREO DE POSICIONES Y TRACKING DE PAGOS REALES

**Archivo: `scanner/worker.py` — `_monitor_loop()` (cada 60s)**

Nuevo flujo por cada posición activa:
1. **Obtener hora del próximo pago** del exchange (vía `next_funding_time` de CCXT)
2. **5 minutos antes del pago** (`alert_minutes_before` configurable):
   - Fetch tasa actual en tiempo real
   - Si la tasa sigue favorable (mismo signo que entry): OK, loguear
   - Si la tasa cambió de signo o cayó >75%: **ENVIAR ALERTA por email**
     - Asunto: "⚠️ ALERTA: {symbol} en {exchange} - Tasa desfavorable"
     - Cuerpo: tasa actual, tasa de entrada, ganancia acumulada, recomendación
3. **En el instante del pago** (cuando `now >= next_funding_time`):
   - Fetch la tasa exacta del momento del pago
   - Calcular ganancia de este intervalo:
     - spot_perp: `(capital/2) * tasa` (la mitad está en futuros)
     - cross_exchange: `(capital/2) * diferencial`
   - Almacenar pago en `position["payments"]`:
     ```python
     {
         "timestamp": funding_time,
         "rate": 0.0150,
         "earned": 3.75,
         "cumulative": 15.50
     }
     ```
   - Actualizar `position["earned_real"]`
   - Actualizar `position["next_payment"]` con el siguiente horario

**Archivo: `portfolio/manager.py`**
- Refactorizar `update_position_earnings()` para usar el nuevo sistema de pagos
- Agregar `get_next_payment_time(position)` que calcula cuándo es el próximo pago
- Agregar `check_pre_payment_rate(position, all_data)` que verifica 5 min antes
- Función `record_payment(position, rate, timestamp)` que registra cada pago individual

**Nuevo en state de cada posición:**
```python
{
    "payments": [                    # Historial de cada pago recibido
        {"ts": 1234, "rate": 0.015, "earned": 3.75, "cumulative": 3.75},
        {"ts": 5678, "rate": 0.012, "earned": 3.00, "cumulative": 6.75},
    ],
    "next_payment": 1699999999000,   # Timestamp del próximo pago
    "payment_count": 2,              # Total pagos recibidos
    "avg_rate": 0.0135,              # Promedio de tasa en pagos recibidos
}
```

---

### 5. CIERRE DE POSICIONES

**Endpoint: `POST /api/close_position`**
```python
{
    "position_id": "uuid-xxx",
    "reason": "manual" | "unfavorable_rate" | "target_reached"
}
```

Lógica:
1. Buscar posición por ID
2. Calcular P&L final:
   - Ganancia real acumulada (sum de payments)
   - Fees estimados (entrada + salida)
   - Ganancia neta = earned_real - fees
3. Mover a `history[]` con todos los datos
4. Liberar capital
5. Enviar email resumen si email_enabled:
   - "Posición cerrada: BTC en Binance"
   - "Duración: 4.5 días | Pagos: 13 | Ganancia neta: $15.30"

**Alertas automáticas de cierre (NO cierra solo, solo alerta):**
- Tasa se revirtió (cambio de signo)
- Tasa cayó >75% del valor de entrada
- Se envía email + se marca en UI con alerta visual

---

### 6. NOTIFICACIONES POR EMAIL (configurables desde el front)

**Archivo: `notifications/email.py`**
- Mantener la clase EmailNotifier pero leer config del state (no de env vars)
- Tipos de notificación:
  1. **Pre-pago desfavorable**: 5 min antes del pago, tasa cambió → "Revisa {symbol}"
  2. **Tasa revertida**: Cambio de signo → "URGENTE: Cierra {symbol}"
  3. **Posición cerrada**: Resumen de P&L al cerrar
  4. **Test**: Email de prueba desde settings

**Archivo: `api/routes.py`**
- `POST /api/test_email`: enviar email de prueba con la config actual del state
- Validar que smtp_user, smtp_password y email_to estén configurados

---

### 7. FRONTEND REDISEÑADO

**3 tabs principales:**

#### Tab 1: Oportunidades (página principal)
```
┌─────────────────────────────────────────────────────────────┐
│  🔍 Escanear   |  Último escaneo: 14:32:05  |  Scan #45    │
├─────────────────────────────────────────────────────────────┤
│                                                              │
│  ┌─ Filtros ─────────────────────────────────────────────┐  │
│  │ APR mín: [10%]  Score mín: [40]  Vol mín: [1M]       │  │
│  └───────────────────────────────────────────────────────┘  │
│                                                              │
│  #  Par       Exchange  Tipo        FR%     APR%   Score    │
│     │         │         │           │       │      │        │
│  1  BTC/USDT  Binance   Spot-Perp   0.015   16.4   92  [A] │
│     Vol: $2.1B | Est. 3d: $4.50/1K | Fees: $0.30 |         │
│     Estable hace 15 días | Break-even: 4h                   │
│     ┌──────────────────────────────────────┐                │
│     │ Capital: [$____]  [📊 Calcular] [▶ Entrar] │         │
│     │ Est. ganancia 3d: $X.XX | Diaria: $X.XX    │         │
│     └──────────────────────────────────────┘                │
│                                                              │
│  2  ETH/USDT  Bybit     Cross-Exch  0.022   24.1   87  [A] │
│     Long: OKX | Short: Bybit | Dif: 0.022%                 │
│     ...                                                      │
│                                                              │
│  3  SOL/USDT  OKX       Spot-Perp   0.018   19.7   78  [B] │
│     ...                                                      │
└─────────────────────────────────────────────────────────────┘
```

Funcionalidad:
- Cada oportunidad tiene un campo para ingresar capital
- Botón "Calcular" muestra ganancia estimada (3 días, diaria, fees, break-even)
- Botón "Entrar" abre la posición y muestra los pasos a ejecutar manualmente
- Badge de estabilidad: A (>85), B (70-84), C (55-69), D (<55)
- Ordenar por: Score, APR, Estabilidad (clickeable en headers)

#### Tab 2: Posiciones Activas
```
┌─────────────────────────────────────────────────────────────┐
│  Capital total: $1,000 | En uso: $750 | Disponible: $250   │
│  Ganancia total: $45.30                                      │
├─────────────────────────────────────────────────────────────┤
│                                                              │
│  ┌─ BTC/USDT — Binance (Spot-Perp) ────────────────────┐   │
│  │ Capital: $500  |  Desde: hace 3.2 días               │   │
│  │                                                       │   │
│  │ Próximo pago: en 2h 15m (16:00 UTC)                  │   │
│  │ Tasa actual: 0.0150% ✅                               │   │
│  │                                                       │   │
│  │ Pagos recibidos: 12                                   │   │
│  │ ┌────────┬──────────┬─────────┬────────────┐         │   │
│  │ │ #      │ Hora     │ Tasa    │ Ganancia   │         │   │
│  │ │ 12     │ 08:00    │ 0.015%  │ $3.75      │         │   │
│  │ │ 11     │ 00:00    │ 0.014%  │ $3.50      │         │   │
│  │ │ 10     │ 16:00    │ 0.016%  │ $4.00      │         │   │
│  │ │ ...    │          │         │            │         │   │
│  │ └────────┴──────────┴─────────┴────────────┘         │   │
│  │                                                       │   │
│  │ Ganancia acumulada: $42.50                            │   │
│  │ Fees estimados (entrada+salida): $1.30                │   │
│  │ Ganancia neta: $41.20                                 │   │
│  │ Tasa promedio: 0.0148%                                │   │
│  │                                                       │   │
│  │ [🔴 Cerrar posición]                                  │   │
│  └───────────────────────────────────────────────────────┘   │
│                                                              │
│  ┌─ ETH/USDT — Cross (Bybit↔OKX) ──────────────────────┐   │
│  │ ...                                                   │   │
│  └───────────────────────────────────────────────────────┘   │
└─────────────────────────────────────────────────────────────┘
```

#### Tab 3: Configuración
```
┌─────────────────────────────────────────────────────────────┐
│  ⚙️ Configuración General                                   │
│  ├─ Capital total USD:        [$1000    ]                   │
│  ├─ Intervalo de escaneo:     [5    ] minutos               │
│  ├─ Máx. posiciones:          [5    ]                       │
│  ├─ Volumen mínimo 24h:       [1000000 ]                    │
│  ├─ APR mínimo:               [10   ] %                     │
│  ├─ Score mínimo:             [40   ]                       │
│  ├─ Días mín. estabilidad:    [3    ]                       │
│  ├─ Alerta min antes pago:    [5    ] minutos               │
│                                                              │
│  📧 Notificaciones Email                                     │
│  ├─ Habilitado:               [✓]                           │
│  ├─ Servidor SMTP:            [smtp.gmail.com]              │
│  ├─ Puerto:                   [587 ]                        │
│  ├─ Usuario SMTP:             [mi@gmail.com   ]             │
│  ├─ Contraseña SMTP:          [••••••••        ]             │
│  ├─ Email destino:            [alertas@gmail.com]            │
│  │                                                           │
│  │  [📧 Enviar email de prueba]   ✅ Conexión exitosa       │
│                                                              │
│  [💾 Guardar configuración]                                  │
└─────────────────────────────────────────────────────────────┘
```

Además: sección de **Historial** al fondo de Posiciones o como sub-tab, mostrando posiciones cerradas con su P&L.

---

## 8. ARCHIVOS A MODIFICAR / CREAR

| Archivo | Acción | Descripción |
|---------|--------|-------------|
| `core/state.py` | MODIFICAR | Nuevo DEFAULT_STATE sin safe/aggr split |
| `config.py` | MODIFICAR | Quitar env vars de email (pasan al state) |
| `scanner/worker.py` | MODIFICAR | Unificar scan, nuevo monitor alineado a pagos |
| `analysis/scoring.py` | MODIFICAR | Rebalancear pesos, agregar estimated_hold_days |
| `analysis/arbitrage.py` | MODIFICAR | Agregar stability_grade, net_apr a cada oportunidad |
| `analysis/fees.py` | MANTENER | Sin cambios significativos |
| `analysis/funding.py` | MANTENER | Sin cambios significativos |
| `portfolio/manager.py` | MODIFICAR | Nuevo sistema de pagos, eliminar safe/aggr budget |
| `portfolio/actions.py` | ELIMINAR/REESCRIBIR | Ya no genera acciones automáticas, solo instrucciones al abrir |
| `portfolio/risk.py` | MODIFICAR | Simplificar: solo chequeo pre-pago y reversión |
| `api/routes.py` | MODIFICAR | Nuevos endpoints, eliminar /confirm, /skip |
| `notifications/email.py` | MODIFICAR | Leer config del state en vez de env vars |
| `exchanges/manager.py` | MANTENER | Sin cambios mayores |
| `coinglass/client.py` | MANTENER | Sin cambios |
| `templates/index.html` | REESCRIBIR | Nuevo layout 3 tabs |
| `static/app.js` | REESCRIBIR | Nueva lógica frontend |
| `static/style.css` | MODIFICAR | Ajustar estilos al nuevo layout |
| `app.py` | MODIFICAR | Ajustar inicialización |

---

## 9. FLUJO COMPLETO DEL SISTEMA

```
1. ESCANEO (cada N minutos, configurable)
   │
   ├─ Fetch funding rates de 4 exchanges (paralelo)
   ├─ Fetch volúmenes 24h
   ├─ Para cada token con FR positivo:
   │   ├─ Fetch historial 30 días
   │   ├─ Calcular: score, stability_grade, estimated_hold_days
   │   ├─ Calcular: fees, net_apr, break_even, daily_income
   │   └─ Si pasa filtros (min_apr, min_score, min_volume) → agregar a lista
   ├─ Scan cross-exchange (diferenciales)
   │   └─ Mismo análisis
   ├─ Coinglass data (si configurado)
   └─ Guardar opportunities[] ordenadas por score DESC

2. USUARIO VE OPORTUNIDADES EN EL FRONT
   │
   ├─ Elige una oportunidad
   ├─ Ingresa capital a invertir
   ├─ Click "Calcular" → ve ganancia estimada
   ├─ Click "Entrar" → POST /api/open_position
   │   ├─ Se crea posición en state
   │   ├─ Se devuelven pasos para ejecutar manualmente
   │   └─ Se calcula próximo pago del exchange
   └─ Usuario ejecuta los pasos en el exchange manualmente

3. MONITOREO CONTINUO (cada 60s)
   │
   ├─ Para cada posición activa:
   │   ├─ Calcular tiempo al próximo pago
   │   ├─ Si faltan ≤5 minutos para el pago:
   │   │   ├─ Fetch tasa actual
   │   │   ├─ Si desfavorable → ALERTA EMAIL + marca en UI
   │   │   └─ Si favorable → OK
   │   ├─ Si ya pasó el momento del pago:
   │   │   ├─ Fetch tasa del instante de pago
   │   │   ├─ Calcular ganancia de este intervalo
   │   │   ├─ Registrar en payments[]
   │   │   ├─ Actualizar earned_real
   │   │   └─ Calcular próximo pago
   │   └─ Actualizar UI en tiempo real
   │
   └─ Alertas:
       ├─ Tasa revertida → email "URGENTE: Cerrar {symbol}"
       ├─ Tasa cayó >75% → email "ADVERTENCIA: Revisar {symbol}"
       └─ Se muestra alerta visual en la UI

4. CIERRE DE POSICIÓN
   │
   ├─ Manual: Usuario click "Cerrar" → POST /api/close_position
   │   ├─ Calcular P&L final
   │   ├─ Mover a history[]
   │   ├─ Liberar capital
   │   └─ Email resumen (si habilitado)
   └─ El bot NUNCA cierra solo, solo alerta
```

---

## 10. ENDPOINTS API FINALES

| Método | Ruta | Descripción |
|--------|------|-------------|
| GET | `/api/config` | Obtener toda la configuración |
| POST | `/api/config` | Actualizar configuración (todos los campos editables) |
| GET | `/api/opportunities` | Lista de oportunidades ordenadas por score |
| POST | `/api/open_position` | Abrir posición con capital específico |
| GET | `/api/positions` | Posiciones activas con pagos y P&L |
| POST | `/api/close_position` | Cerrar posición manualmente |
| GET | `/api/history` | Historial de posiciones cerradas |
| POST | `/api/force_scan` | Forzar escaneo inmediato |
| POST | `/api/test_email` | Enviar email de prueba |
| POST | `/api/calculate` | Calcular ganancia estimada para una oportunidad + capital |

---

## 11. ORDEN DE IMPLEMENTACIÓN

1. **Fase 1 - Core**: state.py, config.py (nueva estructura)
2. **Fase 2 - Scoring**: scoring.py (rebalancear pesos, stability_grade)
3. **Fase 3 - Scanner**: worker.py (unificar scan, eliminar safe/aggr)
4. **Fase 4 - Portfolio**: manager.py (nuevo sistema pagos), eliminar actions.py viejo
5. **Fase 5 - API**: routes.py (nuevos endpoints)
6. **Fase 6 - Notifications**: email.py (leer de state)
7. **Fase 7 - Frontend**: HTML + JS + CSS completo
8. **Fase 8 - Testing**: Verificar flujo completo
