"""Per-user order execution via CCXT for CEX (Binance, Bybit, OKX, Bitget).

This module is the ONLY place in the codebase that places REAL orders. It is
intentionally separate from ``exchanges/manager.py``, which holds read-only,
public-data clients shared across all users. Every call here builds a fresh
authenticated client from the *user's own* decrypted API keys.

Methodology (matches portfolio/actions.build_entry_strategy):
  - spot_perp:        Limit BUY spot (mid, 60s) → Market SHORT perp on fill.
  - cross_exchange:   Limit on both legs simultaneously (90s); abort+unwind
                      if only one leg fills.
On any partial fill we auto-unwind the filled leg with a market order so the
user never sits on un-hedged directional exposure.

Scope (v1): CEX only. On-chain / wallet spot (Binance Alpha, Bitget Onchain,
Web3 wallets) is NOT tradeable via CCXT and is gated out — those fall back to
the manual flow.

Set env TRADE_SANDBOX=1 to route every client to the exchange CCXT testnet.
"""
import os
import time
import logging

log = logging.getLogger("bot.exec")

SUPPORTED_EXCHANGES = {"binance", "bybit", "okx", "bitget"}

_CCXT_CLASSES = {}  # lazy-filled name -> ccxt class

# Maker-leg limit-fill timeouts (seconds) per the methodology.
SPOT_LIMIT_TIMEOUT = 60
CROSS_LIMIT_TIMEOUT = 90
POLL_INTERVAL = 2.0

# Holgura para que el límite spot favorable quede por debajo del perp y siga siendo
# maker (no cruza el spread). 0.0005 == 0.05%.
FAVORABLE_BASIS_EPS = 0.0005


# ── Helpers ────────────────────────────────────────────────────────────────
def is_cex(name: str) -> bool:
    return (name or "").lower() in SUPPORTED_EXCHANGES


def to_ccxt_symbol(symbol: str, kind: str) -> str:
    """Internal base symbol ('BTC') → CCXT unified symbol.

    kind='spot' → 'BTC/USDT'   ·   kind='swap' → 'BTC/USDT:USDT'
    """
    base = symbol.upper()
    return f"{base}/USDT" if kind == "spot" else f"{base}/USDT:USDT"


def _ccxt_class(name: str):
    import ccxt  # lazy — module must import without ccxt installed
    if not _CCXT_CLASSES:
        _CCXT_CLASSES.update({
            "binance": ccxt.binance,
            "bybit": ccxt.bybit,
            "okx": ccxt.okx,
            "bitget": ccxt.bitget,
        })
    return _CCXT_CLASSES.get(name.lower())


def _build_client(exchange_name: str, creds: dict, default_type: str = "swap"):
    """Build a fresh authenticated CCXT client from the user's keys."""
    name = (exchange_name or "").lower()
    cls = _ccxt_class(name)
    if cls is None:
        raise ValueError(f"Exchange no soportado: {exchange_name}")

    params = {
        "enableRateLimit": True,
        "options": {"defaultType": default_type},
        "apiKey": creds.get("api_key", ""),
        "secret": creds.get("api_secret", ""),
    }
    if creds.get("passphrase"):
        params["password"] = creds["passphrase"]

    client = cls(params)
    if os.environ.get("TRADE_SANDBOX", "").lower() in ("1", "true", "yes"):
        try:
            client.set_sandbox_mode(True)
        except Exception as e:
            log.warning(f"sandbox mode unavailable for {name}: {e}")
    return client


def _ensure_markets(client):
    if not client.markets:
        client.load_markets()


def _top_of_book(client, ccxt_symbol: str, fallback: float = 0.0):
    """(bid, ask, mid) desde el ticker. Cae al fallback si no hay bid/ask.

    Cuando solo hay 'last' (sin bid/ask en el ticker), bid==ask==mid==last.
    """
    try:
        t = client.fetch_ticker(ccxt_symbol)
        bid = float(t.get("bid") or 0)
        ask = float(t.get("ask") or 0)
        if bid and ask:
            return bid, ask, (bid + ask) / 2
        last = float(t.get("last") or t.get("close") or 0)
        if last:
            return last, last, last
    except Exception as e:
        log.warning(f"ticker fetch failed {ccxt_symbol}: {e}")
    fb = float(fallback or 0)
    return fb, fb, fb


def _mid_price(client, ccxt_symbol: str, fallback: float = 0.0) -> float:
    """Best-effort mid price from the ticker; falls back to a provided price."""
    return _top_of_book(client, ccxt_symbol, fallback)[2]


def _norm_amount(client, ccxt_symbol: str, base_amount: float) -> float:
    """Convert a base-currency size into the exchange's order amount.

    Linear USDT swaps on OKX/Bitget are denominated in *contracts*; Binance /
    Bybit / spot use the base coin directly (contractSize == 1). Dividing by
    contractSize handles both transparently, then we round to precision.
    """
    market = client.market(ccxt_symbol)
    cs = market.get("contractSize") or 1
    amount = base_amount / cs if cs else base_amount
    try:
        return float(client.amount_to_precision(ccxt_symbol, amount))
    except Exception as e:
        # CCXT lanza InvalidOrder cuando el tamaño trunca por debajo del paso
        # mínimo del exchange. No reventamos: devolvemos 0.0 y dejamos que
        # _check_min_notional emita el error limpio y accionable.
        log.warning(f"amount_to_precision {ccxt_symbol} ({amount}) falló: {e}")
        return 0.0


def _check_min_notional(client, ccxt_symbol: str, amount: float, price: float):
    """Return an error string if the order is below exchange minimums, else None."""
    try:
        market = client.market(ccxt_symbol)
        if amount <= 0:
            step = (market.get("precision") or {}).get("amount")
            return (f"tamaño calculado bajo el mínimo operable de {ccxt_symbol} "
                    f"(paso={step}); sube el capital o el leverage")
        limits = market.get("limits", {})
        min_amt = (limits.get("amount") or {}).get("min")
        min_cost = (limits.get("cost") or {}).get("min")
        if min_amt and amount < min_amt:
            return f"tamaño {amount} < mínimo {min_amt} en {ccxt_symbol}"
        if min_cost and price and (amount * price) < min_cost:
            return f"notional ${amount * price:.2f} < mínimo ${min_cost} en {ccxt_symbol}"
    except Exception:
        pass
    return None


def _order_fee_usd(order: dict) -> float:
    """Sum filled fees from a CCXT order, in quote (USDT ≈ USD)."""
    total = 0.0
    fee = order.get("fee")
    fees = order.get("fees") or ([] if fee is None else [fee])
    for f in fees:
        if f and f.get("cost") is not None:
            try:
                total += abs(float(f["cost"]))
            except (TypeError, ValueError):
                pass
    return total


def _exchange_margin_params(client) -> dict:
    """Params extra que requiere set_margin_mode por exchange."""
    if getattr(client, "id", "") == "bitget":
        return {"marginCoin": "USDT", "productType": "USDT-FUTURES"}
    return {}


def _exchange_leverage_params(client, ccxt_symbol: str, side: str | None) -> dict:
    """Params extra que requiere set_leverage por exchange.

    En hedge mode Bitget/OKX fijan el leverage por lado (holdSide/posSide); en
    one-way no se pasa el lado.
    """
    ex = getattr(client, "id", "")
    hedged = _is_hedged(client, ccxt_symbol) is True
    if ex == "bitget":
        p = {"marginCoin": "USDT", "productType": "USDT-FUTURES"}
        if side and hedged:
            p["holdSide"] = "long" if side == "long" else "short"
        return p
    if ex == "okx":
        p = {"marginMode": "isolated"}
        if side and hedged:
            p["posSide"] = "long" if side == "long" else "short"
        return p
    return {}


def _is_no_change_error(e: Exception) -> bool:
    """True si el error indica que el margen/leverage YA estaba en el valor pedido.

    Varios exchanges lanzan en vez de devolver no-op cuando el setting no cambia;
    eso NO es un fallo — significa que la configuración ya es la correcta.
    """
    msg = str(e).lower()
    needles = ("not be changed", "not changed", "no need to change", "no change",
               "not modified", "same as", "repeat", "duplicate", "已", "40109", "45117")
    return any(n in msg for n in needles)


def _read_position_config(client, ccxt_symbol: str):
    """Lee (margin_mode, leverage) configurados para el símbolo desde el exchange.

    Devuelve (None, None) si no se puede leer (algunos exchanges no exponen la
    config cuando no hay posición). Es la verificación autoritativa de que el
    set_* realmente surtió efecto.
    """
    try:
        positions = client.fetch_positions([ccxt_symbol])
    except Exception as e:
        log.warning(f"read position config {ccxt_symbol} failed: {e}")
        return (None, None)
    for p in positions:
        if p.get("symbol") and p.get("symbol") != ccxt_symbol:
            continue
        info = p.get("info") or {}
        mm = p.get("marginMode") or info.get("marginMode") or info.get("marginType")
        lev = p.get("leverage")
        if lev is None:
            lev = info.get("leverage")
        mm = str(mm).lower() if mm is not None else None
        if mm == "crossed":
            mm = "cross"
        try:
            lev = float(lev) if lev is not None else None
        except (TypeError, ValueError):
            lev = None
        return (mm, lev)
    return (None, None)


def _ensure_margin_and_leverage(client, ccxt_symbol: str, leverage: int,
                                side: str | None = None):
    """Garantiza margen AISLADO + el leverage exacto, verificando por read-back.

    Devuelve None si quedó garantizado, o un string de error accionable si no.
    El caller debe abortar ANTES de colocar la primera orden cuando devuelve error,
    para no quedar expuesto en cruzado o con un leverage distinto al indicado.
    """
    leverage = int(leverage)
    mm_err = lev_err = None
    try:
        client.set_margin_mode("isolated", ccxt_symbol, _exchange_margin_params(client))
    except Exception as e:
        if not _is_no_change_error(e):
            mm_err = e
    try:
        client.set_leverage(leverage, ccxt_symbol,
                            _exchange_leverage_params(client, ccxt_symbol, side))
    except Exception as e:
        if not _is_no_change_error(e):
            lev_err = e

    # Read-back: autoritativo cuando está disponible.
    mm, lev = _read_position_config(client, ccxt_symbol)
    problems = []
    if mm is not None:
        if mm != "isolated":
            problems.append(f"el margen quedó en '{mm}' (se requiere isolated)")
    elif mm_err is not None:
        problems.append(f"no se pudo fijar el margen aislado: {mm_err}")
    if lev is not None:
        if int(round(lev)) != leverage:
            problems.append(f"el leverage quedó en {int(round(lev))}x (se pidió {leverage}x)")
    elif lev_err is not None:
        problems.append(f"no se pudo fijar el leverage: {lev_err}")

    if problems:
        return (f"No se pudo garantizar margen aislado + leverage en {ccxt_symbol}: "
                + "; ".join(problems)
                + ". Si hay una posición u orden abierta en ese símbolo, ciérrala e "
                "inténtalo de nuevo.")
    return None


def _is_hedged(client, ccxt_symbol: str):
    """Modo de posición real de la cuenta: True=hedge, False=one-way, None=desconocido.

    Cacheado por cliente para no repetir la llamada en cada pierna. Si el exchange
    no soporta la consulta o falla, devolvemos None y el caller decide el fallback.
    """
    cache = getattr(client, "_pos_mode_cache", None)
    if cache is None:
        cache = {}
        try:
            client._pos_mode_cache = cache
        except Exception:
            pass
    if ccxt_symbol in cache:
        return cache[ccxt_symbol]
    hedged = None
    # Bitget no implementa fetch_position_mode; el modo (posMode) viene en el
    # info crudo de fetch_margin_mode. Otros CEX sí soportan fetch_position_mode.
    try:
        if getattr(client, "has", {}).get("fetchPositionMode"):
            res = client.fetch_position_mode(ccxt_symbol)
            hedged = bool(res.get("hedged")) if isinstance(res, dict) else None
        else:
            mm = client.fetch_margin_mode(ccxt_symbol)
            pos_mode = (mm.get("info") or {}).get("posMode") if isinstance(mm, dict) else None
            if pos_mode == "hedge_mode":
                hedged = True
            elif pos_mode == "one_way_mode":
                hedged = False
    except Exception as e:
        log.warning(f"detect position mode {ccxt_symbol} failed: {e}")
    cache[ccxt_symbol] = hedged
    return hedged


def _perp_open_params(client, ccxt_symbol: str) -> dict:
    """Params para ABRIR una pierna perp según el modo de posición de la cuenta.

    En hedge mode se pasa el booleano unificado ``hedged``; CCXT deriva
    internamente ``tradeSide=Open`` y el ``posSide`` a partir del side. En one-way
    (o desconocido) no se añade nada (comportamiento por defecto).
    """
    if _is_hedged(client, ccxt_symbol) is True:
        return {"hedged": True}
    return {}


def _perp_close_params(client, ccxt_symbol: str) -> dict:
    """Params para CERRAR / reducir una pierna perp según el modo de posición.

    one-way / desconocido: ``reduceOnly``. hedge: ``reduceOnly`` + ``hedged``; CCXT
    convierte esto en ``tradeSide=Close`` e invierte el side a la convención de
    Bitget (cerrar short = side sell, cerrar long = side buy).
    """
    if _is_hedged(client, ccxt_symbol) is True:
        return {"reduceOnly": True, "hedged": True}
    return {"reduceOnly": True}


def _ensure_one_way_or_abort(client, ccxt_symbol: str):
    """Pre-flight de seguridad antes de colocar la primera orden.

    Devuelve un mensaje de error si NO podemos garantizar que el perp se colocará
    correctamente, en cuyo caso el caller debe abortar ANTES de tocar el spot.
    Devuelve None si se puede proceder (one-way detectado, hedge soportado vía
    params, o pudimos forzar one-way).

    - hedge mode → None: las órdenes se adaptan con _perp_open/close_params.
    - one-way    → None: flujo normal.
    - desconocido → intentamos forzar one-way; si Bitget responde 40920
      ("cannot be switched", hay posición/orden previa) no sabemos el modo real
      y abortamos para no quedar medio-abiertos.
    """
    hedged = _is_hedged(client, ccxt_symbol)
    if hedged is not None:
        return None
    try:
        client.set_position_mode(False, ccxt_symbol)
        return None
    except Exception as e:
        msg = str(e)
        if "40920" in msg or "cannot be switched" in msg.lower():
            return (f"No se pudo fijar el modo de posición en {ccxt_symbol}: hay una "
                    f"posición u orden abierta en futuros y no se pudo detectar el modo. "
                    f"Cierra/cancela esa posición en Bitget e inténtalo de nuevo.")
        log.warning(f"set_position_mode(one-way,{ccxt_symbol}) failed: {e}")
        return None


def _spot_sellable(client, ccxt_symbol: str, desired: float) -> float:
    """Cantidad de base realmente vendible = min(desired, free balance), a precisión.

    En una compra spot el fee se descuenta del activo recibido, así que el balance
    libre es menor que 'filled'. Vender 'filled' completo provoca insufficient
    balance (code 43012).
    """
    try:
        base = client.market(ccxt_symbol)["base"]
        free = (client.fetch_balance().get("free") or {}).get(base) or 0
        sellable = min(desired, float(free))
        return float(client.amount_to_precision(ccxt_symbol, sellable))
    except Exception as e:
        log.warning(f"spot_sellable {ccxt_symbol} failed: {e}")
        return desired


def _open_perp_size(client, ccxt_symbol: str, side: str) -> float:
    """Contratos realmente abiertos en el perp para `side` ('long'/'short').

    Lee la posición real del exchange (fetch_positions) en vez de recalcular la
    cantidad desde el notional. Cerrar contra el estado real garantiza un cierre
    completo, sin el contrato residual que deja recalcular `exposure / precio`
    (deriva de precio entre apertura y cierre + truncado de precisión). Es el
    análogo perp de `_spot_sellable`.

    `contracts` ya viene en unidades de contrato (la misma que usa create_order),
    así que NO se divide por contractSize ni se re-trunca a precisión (eso podría
    re-introducir el residual). Devuelve 0.0 si no se puede leer → el caller cae
    al cálculo por notional como fallback.
    """
    try:
        positions = client.fetch_positions([ccxt_symbol])
    except Exception as e:
        log.warning(f"fetch_positions {ccxt_symbol} falló: {e}")
        return 0.0
    for p in positions:
        contracts = p.get("contracts")
        if not contracts:
            continue
        # one-way: una sola entrada. hedge: long y short separadas → filtramos por lado.
        pside = p.get("side")
        if pside and side and pside != side:
            continue
        try:
            return abs(float(contracts))
        except (TypeError, ValueError):
            continue
    return 0.0


def _poll_fill(client, order_id: str, ccxt_symbol: str, timeout: float):
    """Poll until the order is fully filled ('closed') or timeout.

    Returns (filled: bool, order: dict|None).
    """
    deadline = time.time() + timeout
    last = None
    while time.time() < deadline:
        try:
            last = client.fetch_order(order_id, ccxt_symbol)
        except Exception as e:
            log.warning(f"fetch_order {order_id} failed: {e}")
            time.sleep(POLL_INTERVAL)
            continue
        status = last.get("status")
        if status == "closed":
            return True, last
        if status in ("canceled", "rejected", "expired"):
            return False, last
        time.sleep(POLL_INTERVAL)
    return False, last


def _safe_cancel(client, order_id: str, ccxt_symbol: str):
    try:
        client.cancel_order(order_id, ccxt_symbol)
    except Exception as e:
        log.warning(f"cancel_order {order_id} failed: {e}")


# ── Public: connection test + spot gate ────────────────────────────────────
def test_connection(exchange_name: str, creds: dict) -> dict:
    """Validate the user's keys by fetching the futures balance."""
    if not is_cex(exchange_name):
        return {"ok": False, "msg": f"{exchange_name} no soportado para auto-trading"}
    if not creds or not creds.get("api_key") or not creds.get("api_secret"):
        return {"ok": False, "msg": "Faltan API key / secret"}
    try:
        client = _build_client(exchange_name, creds, default_type="swap")
        bal = client.fetch_balance()
        usdt = (bal.get("total") or {}).get("USDT")
        return {"ok": True, "msg": "Conexión OK", "usdt_balance": usdt}
    except Exception as e:
        return {"ok": False, "msg": f"Conexión fallida: {type(e).__name__}: {e}"}


def spot_tradeable(exchange_name: str, creds: dict, symbol: str) -> bool:
    """True iff the base symbol has a tradeable CENTRALIZED spot market.

    This is the gate that excludes Binance Alpha / Bitget Onchain / Web3-only
    pairs, which never appear as an active spot market in CCXT.
    """
    try:
        client = _build_client(exchange_name, creds, default_type="spot")
        _ensure_markets(client)
        sym = to_ccxt_symbol(symbol, "spot")
        m = client.markets.get(sym)
        return bool(m and m.get("spot") and m.get("active", True))
    except Exception as e:
        log.warning(f"spot_tradeable check failed {exchange_name}/{symbol}: {e}")
        return False


# ── Public: open ────────────────────────────────────────────────────────────
def execute_open(creds_by_exchange: dict, opp: dict, capital: float,
                 leverage: int = 1, dry_run: bool = False,
                 allow_market_fallback: bool = True) -> dict:
    """Place the real orders for an opportunity. Returns a result dict.

    Success: {ok:True, dry_run, entry_price, entry_fees_usd, order_ids[], legs[]}
    Failure: {ok:False, msg, unwound?:bool, legs?:[]}

    allow_market_fallback (spot_perp): si el límite spot favorable no llena en la
    ventana, completar a mercado SOLO si el basis sigue ≥ 0 (spot ≤ perp). Con
    False, en ese caso se aborta sin entrar.
    """
    mode = opp.get("mode", "spot_perp")
    symbol = opp.get("symbol", "")
    leverage = max(1, int(leverage))

    if mode == "spot_perp":
        return _open_spot_perp(creds_by_exchange, opp, symbol, capital, leverage,
                               dry_run, allow_market_fallback)
    if mode == "cross_exchange":
        return _open_cross(creds_by_exchange, opp, symbol, capital, leverage, dry_run)
    return {"ok": False, "msg": f"Modo '{mode}' no soportado en auto (usa flujo manual)"}


def _open_spot_perp(creds_by_exchange, opp, symbol, capital, leverage, dry_run,
                    allow_market_fallback=True):
    exchange = opp.get("exchange", "")
    if not is_cex(exchange):
        return {"ok": False, "msg": f"{exchange} no es CEX soportado"}
    creds = creds_by_exchange.get(exchange.lower())
    if not creds:
        return {"ok": False, "msg": f"Sin API keys para {exchange}"}

    # Delta-neutral sizing (mirror of portfolio.manager.open_position).
    fut_margin = capital / (leverage + 1)
    spot_size = capital - fut_margin      # USD long spot
    exposure = spot_size                  # both legs equal notional

    spot_sym = to_ccxt_symbol(symbol, "spot")
    perp_sym = to_ccxt_symbol(symbol, "swap")

    try:
        spot_cli = _build_client(exchange, creds, default_type="spot")
        perp_cli = _build_client(exchange, creds, default_type="swap")
        _ensure_markets(spot_cli)
        _ensure_markets(perp_cli)
    except Exception as e:
        return {"ok": False, "msg": f"No se pudo conectar a {exchange}: {e}"}

    spot_bid, spot_ask, spot_mid = _top_of_book(spot_cli, spot_sym, opp.get("price", 0))
    perp_bid, perp_ask, perp_mid = _top_of_book(perp_cli, perp_sym, opp.get("price", 0))
    if spot_mid <= 0 or perp_mid <= 0:
        return {"ok": False, "msg": "No se pudo obtener precio de mercado"}

    # Precio límite favorable: comprar el spot al/o por debajo del perp (basis ≥ 0)
    # para que el slippage juegue a favor. Anclado al bid del perp (menos EPS para
    # seguir maker) y topeado al bid del spot (nunca postear por encima del mejor bid).
    fav_px = min(spot_bid, perp_bid * (1 - FAVORABLE_BASIS_EPS))
    if fav_px <= 0:
        fav_px = spot_bid or spot_mid

    spot_amt = _norm_amount(spot_cli, spot_sym, spot_size / fav_px)
    perp_amt = _norm_amount(perp_cli, perp_sym, exposure / perp_mid)

    err = _check_min_notional(spot_cli, spot_sym, spot_amt, fav_px) \
        or _check_min_notional(perp_cli, perp_sym, perp_amt, perp_mid)
    if err:
        return {"ok": False, "msg": f"Orden bajo el mínimo: {err}"}

    fav_basis_pct = round((perp_bid - fav_px) / fav_px * 100, 4) if fav_px else None

    if dry_run:
        return {
            "ok": True, "dry_run": True,
            "entry_price": perp_mid,
            "entry_fees_usd": round((spot_size + exposure) * 0.0006, 4),
            "order_ids": [],
            "entry_mode": "limit_favorable",
            "basis_pct": fav_basis_pct,
            "limit_px": fav_px,
            "legs": [
                {"side": "buy", "kind": "spot", "exchange": exchange, "symbol": spot_sym,
                 "amount": spot_amt, "price": fav_px, "type": "limit"},
                {"side": "sell", "kind": "perp", "exchange": exchange, "symbol": perp_sym,
                 "amount": perp_amt, "price": perp_mid, "type": "market"},
            ],
        }

    mode_err = _ensure_one_way_or_abort(perp_cli, perp_sym)
    if mode_err:
        return {"ok": False, "msg": mode_err}
    cfg_err = _ensure_margin_and_leverage(perp_cli, perp_sym, leverage, side="short")
    if cfg_err:
        return {"ok": False, "msg": cfg_err}

    # Leg 1 — maker-first: limit BUY spot al precio favorable; si no llena en la
    # ventana, completar a mercado SOLO si el basis sigue ≥ 0 (guardia de basis).
    try:
        spot_order = spot_cli.create_order(spot_sym, "limit", "buy", spot_amt, fav_px)
    except Exception as e:
        return {"ok": False, "msg": f"No se pudo colocar límite spot: {e}"}

    filled, spot_order = _poll_fill(spot_cli, spot_order["id"], spot_sym, SPOT_LIMIT_TIMEOUT)
    entry_mode = "limit_favorable"
    spot_fee = _order_fee_usd(spot_order)
    spot_fill_amt = (spot_order.get("filled") or 0) if spot_order else 0
    spot_fill_px = (spot_order or {}).get("average") or fav_px

    if not filled:
        _safe_cancel(spot_cli, spot_order["id"], spot_sym)
        part = (spot_order or {}).get("filled") or 0
        remaining = spot_amt - part
        # Guardia de basis: solo ir a mercado si comprar el spot AHORA mantiene
        # basis ≥ 0 (mejor ask del spot ≤ mejor bid del perp).
        _, cur_spot_ask, _ = _top_of_book(spot_cli, spot_sym, spot_ask)
        cur_perp_bid, _, _ = _top_of_book(perp_cli, perp_sym, perp_bid)
        basis_ok = cur_spot_ask > 0 and cur_perp_bid > 0 and cur_spot_ask <= cur_perp_bid

        if allow_market_fallback and basis_ok and remaining > 0:
            try:
                fb = spot_cli.create_order(spot_sym, "market", "buy",
                                           _norm_amount(spot_cli, spot_sym, remaining))
            except Exception as e:
                if part > 0:
                    try:
                        spot_cli.create_order(spot_sym, "market", "sell",
                                              _spot_sellable(spot_cli, spot_sym, part))
                    except Exception:
                        return {"ok": False, "msg": f"Fallback de mercado falló y parcial spot NO deshecho — REVISA MANUALMENTE: {e}"}
                return {"ok": False, "msg": f"Fallback de mercado spot falló: {e}"}
            spot_fee += _order_fee_usd(fb)
            spot_fill_amt = part + (fb.get("filled") or 0)
            spot_fill_px = fb.get("average") or spot_fill_px
            entry_mode = "market_fallback"
        else:
            # Sin fallback (apagado, basis negativo o nada pendiente): quedar flat.
            if part > 0:
                try:
                    spot_cli.create_order(spot_sym, "market", "sell",
                                          _spot_sellable(spot_cli, spot_sym, part))
                except Exception as e:
                    return {"ok": False, "msg": f"Límite spot parcial NO deshecho — REVISA MANUALMENTE: {e}"}
            if not allow_market_fallback:
                return {"ok": False, "msg": f"La orden límite spot no se llenó en {SPOT_LIMIT_TIMEOUT}s, abortado"}
            return {"ok": False,
                    "msg": (f"No se logró basis favorable en {SPOT_LIMIT_TIMEOUT}s "
                            f"(spot ask {cur_spot_ask:.6g} > perp bid {cur_perp_bid:.6g}); "
                            "abortado para no entrar con basis negativo")}

    if spot_fill_amt <= 0:
        return {"ok": False, "msg": "El spot no se llenó, abortado"}

    # Leg 2 — taker: market SHORT perp. On failure, unwind the spot leg.
    try:
        perp_order = perp_cli.create_order(perp_sym, "market", "sell", perp_amt, None,
                                           _perp_open_params(perp_cli, perp_sym))
    except Exception as e:
        unwound = True
        try:
            spot_cli.create_order(spot_sym, "market", "sell",
                                  _spot_sellable(spot_cli, spot_sym, spot_fill_amt))
        except Exception as ue:
            unwound = False
            log.error(f"UNWIND FAILED spot {spot_sym}: {ue}")
        return {
            "ok": False,
            "msg": f"Falló el short perp: {e}. Pierna spot {'deshecha' if unwound else 'NO deshecha — REVISA MANUALMENTE'}.",
            "unwound": unwound,
        }

    perp_fee = _order_fee_usd(perp_order)
    perp_fill_px = perp_order.get("average") or perp_order.get("price") or perp_mid
    real_basis_pct = (round((perp_fill_px - spot_fill_px) / spot_fill_px * 100, 4)
                      if spot_fill_px else None)

    return {
        "ok": True, "dry_run": False,
        "entry_price": perp_fill_px,
        "entry_fees_usd": round(spot_fee + perp_fee, 6),
        "order_ids": [spot_order.get("id"), perp_order.get("id")],
        "entry_mode": entry_mode,
        "basis_pct": real_basis_pct,
        "limit_px": fav_px,
        "legs": [
            {"side": "buy", "kind": "spot", "exchange": exchange, "symbol": spot_sym,
             "amount": spot_fill_amt, "price": spot_fill_px,
             "order_id": spot_order.get("id"), "fee_usd": spot_fee},
            {"side": "sell", "kind": "perp", "exchange": exchange, "symbol": perp_sym,
             "amount": perp_order.get("filled") or perp_amt, "price": perp_fill_px,
             "order_id": perp_order.get("id"), "fee_usd": perp_fee},
        ],
    }


def _open_cross(creds_by_exchange, opp, symbol, capital, leverage, dry_run):
    long_ex = opp.get("long_exchange", "")
    short_ex = opp.get("short_exchange", "")
    if not is_cex(long_ex) or not is_cex(short_ex):
        return {"ok": False, "msg": "Auto solo soporta cross entre CEX (una pierna no es CEX)"}
    long_creds = creds_by_exchange.get(long_ex.lower())
    short_creds = creds_by_exchange.get(short_ex.lower())
    if not long_creds or not short_creds:
        missing = long_ex if not long_creds else short_ex
        return {"ok": False, "msg": f"Sin API keys para {missing}"}

    margin_side = capital / 2
    exposure = margin_side * leverage
    sym = to_ccxt_symbol(symbol, "swap")

    try:
        long_cli = _build_client(long_ex, long_creds, default_type="swap")
        short_cli = _build_client(short_ex, short_creds, default_type="swap")
        _ensure_markets(long_cli)
        _ensure_markets(short_cli)
    except Exception as e:
        return {"ok": False, "msg": f"No se pudo conectar: {e}"}

    long_px = _mid_price(long_cli, sym, opp.get("long_price", 0))
    short_px = _mid_price(short_cli, sym, opp.get("short_price", 0))
    if long_px <= 0 or short_px <= 0:
        return {"ok": False, "msg": "No se pudo obtener precio de mercado"}

    long_amt = _norm_amount(long_cli, sym, exposure / long_px)
    short_amt = _norm_amount(short_cli, sym, exposure / short_px)

    err = _check_min_notional(long_cli, sym, long_amt, long_px) \
        or _check_min_notional(short_cli, sym, short_amt, short_px)
    if err:
        return {"ok": False, "msg": f"Orden bajo el mínimo: {err}"}

    if dry_run:
        return {
            "ok": True, "dry_run": True,
            "entry_price": short_px,
            "entry_fees_usd": round(exposure * 2 * 0.0006, 4),
            "order_ids": [],
            "legs": [
                {"side": "buy", "kind": "perp", "exchange": long_ex, "symbol": sym,
                 "amount": long_amt, "price": long_px, "type": "limit"},
                {"side": "sell", "kind": "perp", "exchange": short_ex, "symbol": sym,
                 "amount": short_amt, "price": short_px, "type": "limit"},
            ],
        }

    mode_err = _ensure_one_way_or_abort(long_cli, sym) or _ensure_one_way_or_abort(short_cli, sym)
    if mode_err:
        return {"ok": False, "msg": mode_err}
    cfg_err = (_ensure_margin_and_leverage(long_cli, sym, leverage, side="long")
               or _ensure_margin_and_leverage(short_cli, sym, leverage, side="short"))
    if cfg_err:
        return {"ok": False, "msg": cfg_err}

    # Both legs as limit, placed back-to-back, then polled within the window.
    try:
        long_order = long_cli.create_order(sym, "limit", "buy", long_amt, long_px,
                                           _perp_open_params(long_cli, sym))
    except Exception as e:
        return {"ok": False, "msg": f"No se pudo colocar límite long en {long_ex}: {e}"}
    try:
        short_order = short_cli.create_order(sym, "limit", "sell", short_amt, short_px,
                                             _perp_open_params(short_cli, sym))
    except Exception as e:
        _safe_cancel(long_cli, long_order["id"], sym)
        return {"ok": False, "msg": f"No se pudo colocar límite short en {short_ex}: {e}"}

    long_filled, long_order = _poll_fill(long_cli, long_order["id"], sym, CROSS_LIMIT_TIMEOUT)
    short_filled, short_order = _poll_fill(short_cli, short_order["id"], sym, CROSS_LIMIT_TIMEOUT)

    if long_filled and short_filled:
        lf = _order_fee_usd(long_order)
        sf = _order_fee_usd(short_order)
        return {
            "ok": True, "dry_run": False,
            "entry_price": short_order.get("average") or short_px,
            "entry_fees_usd": round(lf + sf, 6),
            "order_ids": [long_order.get("id"), short_order.get("id")],
            "legs": [
                {"side": "buy", "kind": "perp", "exchange": long_ex, "symbol": sym,
                 "amount": long_order.get("filled"), "price": long_order.get("average") or long_px,
                 "order_id": long_order.get("id"), "fee_usd": lf},
                {"side": "sell", "kind": "perp", "exchange": short_ex, "symbol": sym,
                 "amount": short_order.get("filled"), "price": short_order.get("average") or short_px,
                 "order_id": short_order.get("id"), "fee_usd": sf},
            ],
        }

    # Partial / no fill → cancel both and unwind whatever filled to stay flat.
    _safe_cancel(long_cli, long_order["id"], sym)
    _safe_cancel(short_cli, short_order["id"], sym)
    unwind_ok = True
    if long_filled and not short_filled:
        amt = long_order.get("filled") or 0
        if amt > 0:
            try:
                long_cli.create_order(sym, "market", "sell", amt, None,
                                      _perp_close_params(long_cli, sym))
            except Exception as e:
                unwind_ok = False
                log.error(f"UNWIND long failed {long_ex}/{sym}: {e}")
    elif short_filled and not long_filled:
        amt = short_order.get("filled") or 0
        if amt > 0:
            try:
                short_cli.create_order(sym, "market", "buy", amt, None,
                                       _perp_close_params(short_cli, sym))
            except Exception as e:
                unwind_ok = False
                log.error(f"UNWIND short failed {short_ex}/{sym}: {e}")

    detail = "ninguna pierna se llenó" if not (long_filled or short_filled) else \
        ("solo el long se llenó" if long_filled else "solo el short se llenó")
    note = "deshecha" if unwind_ok else "NO deshecha — REVISA MANUALMENTE"
    return {
        "ok": False,
        "msg": f"Abortado en {CROSS_LIMIT_TIMEOUT}s: {detail}. Pierna {note}.",
        "unwound": unwind_ok,
    }


# ── Public: close ───────────────────────────────────────────────────────────
def execute_close(creds_by_exchange: dict, position: dict, dry_run: bool = False) -> dict:
    """Reverse both legs of an open position with market orders.

    Returns {ok, dry_run, exit_fees_usd, order_ids[], legs[]} or {ok:False, msg}.
    """
    mode = position.get("mode", "spot_perp")
    symbol = position.get("symbol", "")
    exposure = position.get("exposure", 0) or 0

    if mode == "spot_perp":
        exchange = position.get("exchange", "")
        creds = creds_by_exchange.get(exchange.lower())
        if not creds:
            return {"ok": False, "msg": f"Sin API keys para {exchange}"}
        spot_sym = to_ccxt_symbol(symbol, "spot")
        perp_sym = to_ccxt_symbol(symbol, "swap")
        try:
            spot_cli = _build_client(exchange, creds, default_type="spot")
            perp_cli = _build_client(exchange, creds, default_type="swap")
            _ensure_markets(spot_cli)
            _ensure_markets(perp_cli)
        except Exception as e:
            return {"ok": False, "msg": f"No se pudo conectar a {exchange}: {e}"}

        spot_px = _mid_price(spot_cli, spot_sym, position.get("entry_price", 0))
        perp_px = _mid_price(perp_cli, perp_sym, position.get("entry_price", 0))
        spot_amt = _norm_amount(spot_cli, spot_sym, exposure / spot_px) if spot_px else 0
        # Cerrar la cantidad REAL del short (lectura del exchange); fallback al notional.
        perp_amt = _open_perp_size(perp_cli, perp_sym, "short") \
            or (_norm_amount(perp_cli, perp_sym, exposure / perp_px) if perp_px else 0)

        if dry_run:
            return {"ok": True, "dry_run": True,
                    "exit_fees_usd": round(exposure * 2 * 0.0006, 4), "order_ids": [],
                    "legs": [
                        {"side": "sell", "kind": "spot", "symbol": spot_sym, "amount": spot_amt},
                        {"side": "buy", "kind": "perp", "symbol": perp_sym, "amount": perp_amt},
                    ]}

        legs, fees, ids = [], 0.0, []
        # Sell spot (close long).
        try:
            o = spot_cli.create_order(spot_sym, "market", "sell",
                                      _spot_sellable(spot_cli, spot_sym, spot_amt))
            fees += _order_fee_usd(o); ids.append(o.get("id"))
            legs.append({"side": "sell", "kind": "spot", "symbol": spot_sym, "order_id": o.get("id")})
        except Exception as e:
            return {"ok": False, "msg": f"No se pudo vender spot: {e}"}
        # Buy-to-close perp (close short).
        try:
            o = perp_cli.create_order(perp_sym, "market", "buy", perp_amt, None,
                                      _perp_close_params(perp_cli, perp_sym))
            fees += _order_fee_usd(o); ids.append(o.get("id"))
            legs.append({"side": "buy", "kind": "perp", "symbol": perp_sym, "order_id": o.get("id")})
        except Exception as e:
            return {"ok": False, "msg": f"Spot cerrado pero perp NO — REVISA MANUALMENTE: {e}"}
        return {"ok": True, "dry_run": False, "exit_fees_usd": round(fees, 6),
                "order_ids": ids, "legs": legs}

    if mode == "cross_exchange":
        long_ex = position.get("long_exchange", "")
        short_ex = position.get("short_exchange", "")
        long_creds = creds_by_exchange.get(long_ex.lower())
        short_creds = creds_by_exchange.get(short_ex.lower())
        if not long_creds or not short_creds:
            return {"ok": False, "msg": "Sin API keys para una de las piernas"}
        sym = to_ccxt_symbol(symbol, "swap")
        try:
            long_cli = _build_client(long_ex, long_creds, default_type="swap")
            short_cli = _build_client(short_ex, short_creds, default_type="swap")
            _ensure_markets(long_cli)
            _ensure_markets(short_cli)
        except Exception as e:
            return {"ok": False, "msg": f"No se pudo conectar: {e}"}

        long_px = _mid_price(long_cli, sym, position.get("entry_price", 0))
        short_px = _mid_price(short_cli, sym, position.get("entry_price", 0))
        # Cerrar la cantidad REAL de cada pierna (lectura del exchange); fallback al notional.
        long_amt = _open_perp_size(long_cli, sym, "long") \
            or (_norm_amount(long_cli, sym, exposure / long_px) if long_px else 0)
        short_amt = _open_perp_size(short_cli, sym, "short") \
            or (_norm_amount(short_cli, sym, exposure / short_px) if short_px else 0)

        if dry_run:
            return {"ok": True, "dry_run": True,
                    "exit_fees_usd": round(exposure * 2 * 0.0006, 4), "order_ids": [],
                    "legs": [
                        {"side": "sell", "kind": "perp", "exchange": long_ex, "symbol": sym, "amount": long_amt},
                        {"side": "buy", "kind": "perp", "exchange": short_ex, "symbol": sym, "amount": short_amt},
                    ]}

        legs, fees, ids = [], 0.0, []
        try:  # close long → sell reduceOnly
            o = long_cli.create_order(sym, "market", "sell", long_amt, None,
                                      _perp_close_params(long_cli, sym))
            fees += _order_fee_usd(o); ids.append(o.get("id"))
            legs.append({"side": "sell", "kind": "perp", "exchange": long_ex, "order_id": o.get("id")})
        except Exception as e:
            return {"ok": False, "msg": f"No se pudo cerrar el long en {long_ex}: {e}"}
        try:  # close short → buy reduceOnly
            o = short_cli.create_order(sym, "market", "buy", short_amt, None,
                                       _perp_close_params(short_cli, sym))
            fees += _order_fee_usd(o); ids.append(o.get("id"))
            legs.append({"side": "buy", "kind": "perp", "exchange": short_ex, "order_id": o.get("id")})
        except Exception as e:
            return {"ok": False, "msg": f"Long cerrado pero short NO en {short_ex} — REVISA MANUALMENTE: {e}"}
        return {"ok": True, "dry_run": False, "exit_fees_usd": round(fees, 6),
                "order_ids": ids, "legs": legs}

    return {"ok": False, "msg": f"Modo '{mode}' no soportado en auto"}
