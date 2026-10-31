"""Per-user order execution via CCXT for CEX (Binance, Bybit, OKX, Bitget).

This module is the ONLY place in the codebase that places REAL orders on CEX.
It is intentionally separate from ``exchanges/manager.py``, which holds
read-only, public-data clients shared across all users. Every call here builds
a fresh authenticated client from the *user's own* decrypted API keys (markets
are reused from a process-wide cache so we don't reload them per call).

Methodology — spot_perp (paired, delta-neutral at every step):
  - Spot leg MAKER (post-only LIMIT_MAKER) at the best bid, capped by the perp
    bid (basis guard). Re-priced every REPRICE_SECS while the window lasts.
  - Perp leg TAKER: every time the spot order fills (partially or fully) the
    NET base received (gross − commission charged in base) is hedged at market.
  - After the maker window: taker fallback for the remainder only if the basis
    guard still holds (open). On close the taker fallback is unconditional —
    getting out has priority.
  - TWAP: the same routine runs in N slices spread over a duration.
  - Quantities and fees come from the real fills (fetch_my_trades by orderId),
    fees converted to USD (USDT as-is, BNB/base at market).
cross_exchange: limit on both legs, polled together; any imbalance at the end
of the window is unwound so both legs carry the matched size.

Every order carries clientOrderId ``bsy{job}-{leg}{seq}`` so an interrupted
job (process restart) can be reconciled from the exchange (see ``reconcile``).

Set env TRADE_SANDBOX=1 to route clients to the exchange test environment
(Binance: demo trading — ccxt dropped futures sandbox).
"""
import os
import time
import math
import logging

from exchanges.net import ccxt_proxy_params

log = logging.getLogger("bot.exec")

SUPPORTED_EXCHANGES = {"binance", "bybit", "okx", "bitget"}

_CCXT_CLASSES = {}  # lazy-filled name -> ccxt class

# Ventanas maker por slice (s) y polling.
SPOT_LIMIT_TIMEOUT = 60
CROSS_LIMIT_TIMEOUT = 90
POLL_INTERVAL = 2.0
REPRICE_SECS = 10

# Basis guard: máximo basis NEGATIVO tolerado al comprar el spot (spot por
# encima del perp). 0.0005 == 0.05%.
MAX_NEG_BASIS = 0.0005

# Markets cache (públicos, compartidos entre usuarios).
_MARKETS_TTL = 1800
_markets_src = {}  # exchange -> (client_with_markets, loaded_ts)

# Monedas que valen ~1 USD para convertir fees.
_USD_LIKE = {"USDT", "USDC", "FDUSD", "BUSD", "USD", "TUSD"}


class ExecError(Exception):
    """Error accionable con código opcional para la UI (p.ej. MULTI_ASSETS)."""

    def __init__(self, msg, code=None):
        super().__init__(msg)
        self.code = code


class ExecContext:
    """Contexto de una ejecución: idempotencia, progreso y cancelación.

    job_id → clientOrderId determinista ``bsy{job}-{leg}{seq}``.
    progress(dict) se llama tras cada evento relevante (lo persiste el job).
    cancelled() → True si el usuario pidió cancelar (se termina cubriendo).
    """

    def __init__(self, job_id=None, progress=None, cancelled=None):
        self.job_id = job_id
        self._progress = progress
        self._cancelled = cancelled
        self._seq = {}

    def coid(self, leg: str):
        if not self.job_id:
            return None
        n = self._seq.get(leg, 0) + 1
        self._seq[leg] = n
        return f"bsy{self.job_id}-{leg}{n}"

    def prefix(self):
        return f"bsy{self.job_id}-" if self.job_id else None

    def progress(self, data: dict):
        if self._progress:
            try:
                self._progress(data)
            except Exception as e:
                log.warning(f"progress callback failed: {e}")

    def cancelled(self) -> bool:
        try:
            return bool(self._cancelled and self._cancelled())
        except Exception:
            return False


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


def _sandbox() -> bool:
    return os.environ.get("TRADE_SANDBOX", "").lower() in ("1", "true", "yes")


def _build_client(exchange_name: str, creds: dict, default_type: str = "swap"):
    """Build a fresh authenticated CCXT client from the user's keys."""
    name = (exchange_name or "").lower()
    cls = _ccxt_class(name)
    if cls is None:
        raise ValueError(f"Exchange no soportado: {exchange_name}")

    options = {"defaultType": default_type}
    if name == "binance":
        # Solo spot + USDⓈ-M: menos RAM/tiempo y nada de inverse/options.
        options["fetchMarkets"] = {"types": ["spot", "linear"]}
        options["adjustForTimeDifference"] = True
    params = {
        "enableRateLimit": True,
        "options": options,
        "apiKey": creds.get("api_key", ""),
        "secret": creds.get("api_secret", ""),
        **ccxt_proxy_params(),
    }
    if creds.get("passphrase"):
        params["password"] = creds["passphrase"]

    client = cls(params)
    if _sandbox():
        try:
            if name == "binance":
                client.enable_demo_trading(True)
            else:
                client.set_sandbox_mode(True)
        except Exception as e:
            log.warning(f"sandbox/demo mode unavailable for {name}: {e}")
    client._bsy_name = name
    return client


def _ensure_markets(client):
    """Carga mercados reutilizando el cache del proceso (TTL 30 min)."""
    if client.markets:
        return
    name = getattr(client, "_bsy_name", getattr(client, "id", ""))
    key = (name, _sandbox())
    src = _markets_src.get(key)
    if src and time.time() - src[1] < _MARKETS_TTL:
        try:
            client.set_markets_from_exchange(src[0])
            return
        except Exception:
            pass
    client.load_markets()
    _markets_src[key] = (client, time.time())


def _top_of_book(client, ccxt_symbol: str, fallback: float = 0.0):
    """(bid, ask, mid) desde el ticker. Cae al fallback si no hay bid/ask."""
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


def _ref_mids(spot_cli, spot_sym, perp_cli, perp_sym, leg=None, fallback: float = 0.0) -> dict:
    """Mids de referencia al INICIAR la ejecución (para medir slippage).

    Spot on-chain: mid de cotizaciones chicas compra/venta (la comisión del DEX
    se compensa entre ambas y queda el precio justo)."""
    _, _, pmid = _top_of_book(perp_cli, perp_sym, fallback)
    smid = None
    try:
        if leg is not None and hasattr(leg, "mid_price"):
            smid = leg.mid_price(pmid)
        else:
            _, _, smid = _top_of_book(spot_cli, spot_sym)
    except Exception as e:
        log.warning(f"ref mid spot {spot_sym}: {e}")
    return {"spot_mid": smid or None, "perp_mid": pmid or None, "ts": time.time()}


def _slippage_usd(side: str, avg, mid, qty):
    """Costo vs mid en USD (positivo = peor que el mid). None sin datos."""
    if not avg or not mid or not qty:
        return None
    return round(((avg - mid) if side == "buy" else (mid - avg)) * qty, 6)


def _slip_summary(spot_side, spot_avg, spot_qty, perp_avg, perp_qty, ref) -> dict:
    ref = ref or {}
    s = _slippage_usd(spot_side, spot_avg, ref.get("spot_mid"), spot_qty)
    p = _slippage_usd("sell" if spot_side == "buy" else "buy", perp_avg, ref.get("perp_mid"), perp_qty)
    total = round((s or 0) + (p or 0), 6) if (s is not None or p is not None) else None
    return {"spot": s, "perp": p, "total": total}


def _legacy_spot_entry(position: dict):
    """Precio spot de entrada desde el audit auto_open (posiciones previas)."""
    for p in position.get("payments") or position.get("payments_json") or []:
        if p.get("kind") == "auto_open":
            for leg in p.get("legs") or []:
                if leg.get("kind") == "spot" and leg.get("price"):
                    return float(leg["price"])
    return None


def _mid_price(client, ccxt_symbol: str, fallback: float = 0.0) -> float:
    return _top_of_book(client, ccxt_symbol, fallback)[2]


def _norm_amount(client, ccxt_symbol: str, base_amount: float) -> float:
    """Base-currency size → exchange order amount (contracts on OKX/Bitget),
    truncated to precision. 0.0 if it truncates below the step."""
    market = client.market(ccxt_symbol)
    cs = market.get("contractSize") or 1
    amount = base_amount / cs if cs else base_amount
    if amount <= 0:
        return 0.0
    try:
        return float(client.amount_to_precision(ccxt_symbol, amount))
    except Exception as e:
        log.debug(f"amount_to_precision {ccxt_symbol} ({amount}) falló: {e}")
        return 0.0


def _contract_size(client, ccxt_symbol: str) -> float:
    try:
        return float(client.market(ccxt_symbol).get("contractSize") or 1)
    except Exception:
        return 1.0


def _min_ok(client, ccxt_symbol: str, amount: float, price: float) -> bool:
    return _check_min_notional(client, ccxt_symbol, amount, price) is None


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
        cs = market.get("contractSize") or 1
        if min_amt and amount < min_amt:
            return f"tamaño {amount} < mínimo {min_amt} en {ccxt_symbol}"
        if min_cost and price and (amount * cs * price) < min_cost:
            return f"notional ${amount * cs * price:.2f} < mínimo ${min_cost} en {ccxt_symbol}"
    except Exception:
        pass
    return None


def _usd_price(client, ccy: str, cache: dict) -> float:
    ccy = (ccy or "").upper()
    if ccy in _USD_LIKE:
        return 1.0
    if ccy in cache:
        return cache[ccy]
    px = 0.0
    try:
        px = _mid_price(client, f"{ccy}/USDT")
    except Exception:
        pass
    cache[ccy] = px
    return px


def _order_fee_usd(order: dict) -> float:
    """Fees from a CCXT order dict, only when charged in a USD-like currency.

    Fallback when trades can't be read; BNB/base fees are not summed as USD.
    """
    total = 0.0
    fee = order.get("fee")
    fees = order.get("fees") or ([] if fee is None else [fee])
    for f in fees:
        if f and f.get("cost") is not None and (f.get("currency") or "").upper() in _USD_LIKE:
            try:
                total += abs(float(f["cost"]))
            except (TypeError, ValueError):
                pass
    return total


def order_fills(client, ccxt_symbol: str, order_id: str, px_cache: dict = None,
                expected_filled: float = None) -> dict:
    """Real fills of one order from its trades.

    Returns {gross, cost, avg, fee_usd, fee_base} where gross is in base units
    (contracts × contractSize for derivatives) and fee_base is the commission
    charged in the base asset (reduces what you actually hold after a buy).

    `expected_filled` (en unidades de la orden): si los trades aún no reflejan
    todo lo llenado (lag del endpoint), cantidad/costo salen del propio order
    para no subestimar el precio de entrada.
    """
    out = _order_fills_trades(client, ccxt_symbol, order_id, px_cache)
    if not order_id or out.get("_from_order"):
        out.pop("_from_order", None)
        return out
    market = client.market(ccxt_symbol)
    mult = (market.get("contractSize") or 1) if market.get("contract") else 1
    need_order = out["gross"] <= 0 or (expected_filled is not None
                                       and out["gross"] < float(expected_filled) * mult * 0.999)
    if need_order:
        try:
            o = client.fetch_order(order_id, ccxt_symbol)
            g = float(o.get("filled") or 0) * mult
            if g > out["gross"]:
                avg = float(o.get("average") or 0) or (out["avg"] if out["avg"] else 0)
                cost = float(o.get("cost") or 0) or g * avg
                fee_usd = max(out["fee_usd"], _order_fee_usd(o))
                out.update(gross=g, cost=cost, avg=cost / g if g else avg, fee_usd=fee_usd)
        except Exception as e:
            log.warning(f"fetch_order fallback {ccxt_symbol} order={order_id}: {e}")
    return out


def _order_fills_trades(client, ccxt_symbol: str, order_id: str, px_cache: dict = None) -> dict:
    """Fills desde fetch_my_trades (o, si no se pueden leer, desde el order)."""
    px_cache = px_cache if px_cache is not None else {}
    out = {"gross": 0.0, "cost": 0.0, "avg": 0.0, "fee_usd": 0.0, "fee_base": 0.0}
    if not order_id:
        return out
    try:
        trades = client.fetch_my_trades(ccxt_symbol, params={"orderId": order_id})
    except Exception as e:
        log.warning(f"fetch_my_trades {ccxt_symbol} order={order_id} failed: {e}")
        trades = None
    market = client.market(ccxt_symbol)
    cs = market.get("contractSize") or 1
    if trades is None:
        # Fallback: orden sin trades legibles → filled/average del order.
        try:
            o = client.fetch_order(order_id, ccxt_symbol)
            g = float(o.get("filled") or 0) * (cs if market.get("contract") else 1)
            out.update(gross=g, avg=float(o.get("average") or 0),
                       cost=float(o.get("cost") or 0), fee_usd=_order_fee_usd(o))
        except Exception:
            pass
        out["_from_order"] = True
        return out
    return _sum_trades(client, market, [t for t in trades if str(t.get("order")) == str(order_id)],
                       px_cache, out)


def _sum_trades(client, market, trades, px_cache, out) -> dict:
    """Acumula en `out` cantidad base, costo y fees (USD / base) de una lista de trades."""
    base = market.get("base", "")
    cs = market.get("contractSize") or 1
    for t in trades:
        amt = float(t.get("amount") or 0) * (cs if market.get("contract") else 1)
        price = float(t.get("price") or 0)
        out["gross"] += amt
        out["cost"] += float(t.get("cost") or amt * price)
        fee = t.get("fee") or {}
        fcost = abs(float(fee.get("cost") or 0))
        fccy = (fee.get("currency") or "").upper()
        if fcost:
            if fccy == base.upper():
                out["fee_base"] += fcost
                out["fee_usd"] += fcost * price
            else:
                out["fee_usd"] += fcost * _usd_price(client, fccy, px_cache)
    if out["gross"] > 0:
        out["avg"] = out["cost"] / out["gross"]
    return out


_TRADES_WINDOW_MS = 7 * 86_400_000 - 1     # Binance userTrades: startTime→endTime ≤ 7 días
EXTERNAL_CLOSE_TOL = 0.01                  # fracción del short registrado que puede faltar sin buscar recompra


def external_perp_buyback(client, ccxt_symbol: str, qty: float, since_ms=None) -> dict:
    """Recompra REAL de un short cerrado FUERA de basyo (manual en la web,
    liquidación): las compras más recientes del perp desde la apertura hasta cubrir
    `qty` (base). {gross, cost, avg, fee_usd, trades} o None si no se pueden leer.

    JCT #46 (2026-10-24): el usuario recompró el short a mano y el cierre solo
    contó la venta del spot → price_pnl +438 sobre 631 USD de nocional.
    """
    market = client.market(ccxt_symbol)
    cs = (market.get("contractSize") or 1) if market.get("contract") else 1
    end = int(time.time() * 1000)
    since_ms = int(since_ms) - 60_000 if since_ms else end - _TRADES_WINDOW_MS
    picked, need = [], qty
    try:
        while end > since_ms and need > 1e-12:
            start = max(since_ms, end - _TRADES_WINDOW_MS)
            trades = client.fetch_my_trades(ccxt_symbol, since=start, limit=1000,
                                            params={"endTime": end})
            buys = sorted((t for t in trades or [] if (t.get("side") or "").lower() == "buy"),
                          key=lambda t: t.get("timestamp") or 0)
            for t in reversed(buys):                      # la más reciente primero
                amt = float(t.get("amount") or 0) * cs
                if amt <= 0:
                    continue
                frac = min(1.0, need / amt)
                fee = t.get("fee") or {}
                picked.append({**t, "amount": float(t["amount"]) * frac,
                               "cost": float(t.get("cost") or amt * float(t.get("price") or 0)) * frac,
                               "fee": {**fee, "cost": abs(float(fee.get("cost") or 0)) * frac}})
                need -= amt * frac
                if need <= 1e-12:
                    break
            end = start - 1
    except Exception as e:
        log.warning(f"external_perp_buyback {ccxt_symbol}: {e}")
        return None
    out = _sum_trades(client, market, picked, {},
                      {"gross": 0.0, "cost": 0.0, "avg": 0.0, "fee_usd": 0.0, "fee_base": 0.0})
    out["trades"] = [str(t.get("id") or t.get("order")) for t in picked]
    return out


def _exchange_margin_params(client) -> dict:
    """Params extra que requiere set_margin_mode por exchange."""
    if getattr(client, "id", "") == "bitget":
        return {"marginCoin": "USDT", "productType": "USDT-FUTURES"}
    return {}


def _exchange_leverage_params(client, ccxt_symbol: str, side: str | None) -> dict:
    """Params extra que requiere set_leverage por exchange (hedge: por lado)."""
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
    """True si el error indica que el margen/leverage YA estaba en el valor pedido."""
    msg = str(e).lower()
    needles = ("not be changed", "not changed", "no need to change", "no change",
               "not modified", "same as", "repeat", "duplicate", "已", "40109", "45117",
               "-4046", "-4059")
    return any(n in msg for n in needles)


def _read_position_config(client, ccxt_symbol: str):
    """(margin_mode, leverage) configurados para el símbolo, leídos del exchange.

    Binance: /fapi/v1/symbolConfig (funciona sin posición abierta). Resto:
    fetch_positions. (None, None) si no se puede leer.
    """
    if getattr(client, "id", "") == "binance":
        try:
            mid = client.market(ccxt_symbol)["id"]
            rows = client.fapiprivate_get_symbolconfig({"symbol": mid})
            for r in rows or []:
                if r.get("symbol") == mid:
                    mt = str(r.get("marginType", "")).lower()
                    mm = "isolated" if mt == "isolated" else ("cross" if mt else None)
                    lev = r.get("leverage")
                    return (mm, float(lev) if lev is not None else None)
        except Exception as e:
            log.warning(f"symbolConfig {ccxt_symbol} failed: {e}")
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
    """Modo de posición real: True=hedge, False=one-way, None=desconocido (cacheado)."""
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
    """Params para ABRIR una pierna perp según el modo de posición.

    hedge → ``hedged`` (ccxt deriva positionSide en Binance / tradeSide en Bitget).
    """
    if _is_hedged(client, ccxt_symbol) is True:
        return {"hedged": True}
    return {}


def _perp_close_params(client, ccxt_symbol: str) -> dict:
    """Params para CERRAR / reducir una pierna perp.

    one-way: ``reduceOnly``. hedge: ``reduceOnly`` + ``hedged`` (ccxt Binance
    quita reduceOnly y pone positionSide del lado que se cierra).
    """
    if _is_hedged(client, ccxt_symbol) is True:
        return {"reduceOnly": True, "hedged": True}
    return {"reduceOnly": True}


def _with_coid(params: dict, coid) -> dict:
    p = dict(params or {})
    if coid:
        p["clientOrderId"] = coid
    return p


def _ensure_one_way_or_abort(client, ccxt_symbol: str):
    """Pre-flight: None si se puede proceder, o mensaje de error para abortar."""
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
                    f"Cierra/cancela esa posición e inténtalo de nuevo.")
        log.warning(f"set_position_mode(one-way,{ccxt_symbol}) failed: {e}")
        return None


def _free_base(client, ccxt_symbol: str):
    try:
        base = client.market(ccxt_symbol)["base"]
        return float((client.fetch_balance().get("free") or {}).get(base) or 0)
    except Exception as e:
        log.warning(f"free balance {ccxt_symbol} failed: {e}")
        return None


def _spot_sellable(client, ccxt_symbol: str, desired: float) -> float:
    """min(desired, free balance) a precisión. Nunca vende más que `desired`."""
    free = _free_base(client, ccxt_symbol)
    amt = desired if free is None else min(desired, free)
    return _norm_amount(client, ccxt_symbol, amt)


def _open_perp_size(client, ccxt_symbol: str, side: str):
    """Tamaño realmente abierto en el perp para `side`, en BASE (no contratos).

    None si no se pudo leer (tras reintentos): el caller NO debe tratarlo como 0
    — vender el spot creyendo que no hay short dejaría el short desnudo.
    """
    positions = None
    for attempt in range(3):
        try:
            positions = client.fetch_positions([ccxt_symbol])
            break
        except Exception as e:
            log.warning(f"fetch_positions {ccxt_symbol} intento {attempt + 1} falló: {e}")
            time.sleep(1 + attempt)
    if positions is None:
        return None
    cs = _contract_size(client, ccxt_symbol)
    for p in positions:
        contracts = p.get("contracts")
        if not contracts:
            continue
        pside = p.get("side")
        if pside and side and pside != side:
            continue
        try:
            return abs(float(contracts)) * cs
        except (TypeError, ValueError):
            continue
    return 0.0


def _poll_fill(client, order_id: str, ccxt_symbol: str, timeout: float):
    """Poll until 'closed' (True) / canceled-rejected-expired (False) or timeout."""
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


def _safe_cancel(client, order_id: str, ccxt_symbol: str) -> bool:
    try:
        client.cancel_order(order_id, ccxt_symbol)
        return True
    except Exception as e:
        log.warning(f"cancel_order {order_id} failed: {e}")
        return False


def _find_by_coid(client, ccxt_symbol: str, coid):
    """Orden por clientOrderId (Binance: origClientOrderId), o None.

    Tras un create_order que lanzó (timeout, 5xx) la orden pudo haber entrado:
    se consulta antes de reintentar para no duplicarla.
    """
    if not coid:
        return None
    for attempt in range(2):
        try:
            return client.fetch_order(None, ccxt_symbol, {"origClientOrderId": coid})
        except Exception as e:
            if "not exist" in str(e).lower() or "notfound" in type(e).__name__.lower():
                return None
            log.warning(f"fetch_order coid={coid} intento {attempt + 1}: {e}")
            time.sleep(1)
    return None


def _create_idempotent(client, ccxt_symbol, typ, side, amount, price, params):
    """create_order que, si lanza, busca la orden por su clientOrderId antes de
    rendirse. Devuelve la orden (creada o encontrada) o re-lanza el error."""
    try:
        return client.create_order(ccxt_symbol, typ, side, amount, price, params)
    except Exception:
        found = _find_by_coid(client, ccxt_symbol, (params or {}).get("clientOrderId"))
        if found and found.get("id"):
            log.warning(f"create_order {ccxt_symbol} lanzó pero la orden existe "
                        f"(coid={params.get('clientOrderId')}): se sigue con ella")
            return found
        raise


def _final_order(client, order_id, ccxt_symbol):
    """Re-consulta la orden tras cancelarla (no confiar en el último poll)."""
    for _ in range(3):
        try:
            return client.fetch_order(order_id, ccxt_symbol)
        except Exception as e:
            log.warning(f"fetch_order final {order_id} failed: {e}")
            time.sleep(1)
    return None


# ── Binance preflight ───────────────────────────────────────────────────────
def binance_max_leverage(perp_cli, perp_sym: str):
    """(max_leverage, maint_margin_ratio) del primer bracket, o (None, None)."""
    try:
        mid = perp_cli.market(perp_sym)["id"]
        rows = perp_cli.fapiprivate_get_leveragebracket({"symbol": mid})
        rows = rows if isinstance(rows, list) else [rows]
        for r in rows:
            if r.get("symbol") == mid and r.get("brackets"):
                b = r["brackets"][0]
                return int(b.get("initialLeverage")), float(b.get("maintMarginRatio") or 0)
    except Exception as e:
        log.warning(f"leverageBracket {perp_sym} failed: {e}")
    return None, None


def liquidation_estimate(entry: float, leverage: int, mmr: float = 0.004) -> dict:
    """Short aislado: liq ≈ entry·(1 + 1/L − mmr). Distancia en % sobre entry."""
    lev = max(1, int(leverage))
    dist = max(0.0, 1.0 / lev - (mmr or 0))
    return {"liq_price": entry * (1 + dist) if entry else None,
            "liq_distance_pct": round(dist * 100, 2),
            "warning": dist < 0.33}


def binance_preflight(perp_cli, perp_sym: str, leverage: int):
    """Raise ExecError si la cuenta no permite operar aislado con ese leverage.

    - Multi-Assets mode → solo admite cruzado → code MULTI_ASSETS (la UI ofrece
      desactivarlo con confirmación explícita).
    - leverage > máximo del símbolo → error.
    """
    try:
        r = perp_cli.fapiprivate_get_multiassetsmargin()
        if str(r.get("multiAssetsMargin")).lower() == "true":
            raise ExecError("Tu cuenta de futuros Binance está en modo Multi-Assets, que solo "
                            "admite margen cruzado. Desactívalo para operar en aislado.",
                            code="MULTI_ASSETS")
    except ExecError:
        raise
    except Exception as e:
        log.warning(f"multiAssetsMargin check failed: {e}")
    max_lev, _ = binance_max_leverage(perp_cli, perp_sym)
    if max_lev and int(leverage) > max_lev:
        raise ExecError(f"Leverage {leverage}x supera el máximo de {perp_sym} ({max_lev}x)")


# ── Seguros spot_perp: SL del short + venta límite del spot ──────────────────
# SL al 80 % del camino entrada → liquidación del short aislado. El spot se vende
# con una límite un poco POR DEBAJO (sl·(1−δ); CEX: LIMIT GTC; BSC: orden límite
# Kyber de 7 días) para que en una subida salga primero el spot:
#   * solo SL (caso A): spot desnudo → el vigilante lo vende a mercado.
#   * solo spot (caso B): el short queda suelto con su SL duro + un take profit a
#     −caída de la venta; si no toca en el tiempo máximo se recompra (RUNNER_RULES).
# Ver reports/protection_study_20261025.md.
PROTECTION_LIQ_FRACTION = 0.80
PROTECTION_SPOT_OFFSET = 0.005       # δ: el caso A cae ~75 % sin cambiar el esperado
# (caída del TP, tiempo máx) por venue del spot. On-chain el spot hace mechas más
# largas y esperar una caída mayor rinde más; en CEX no (estudios del 2026-10-25).
RUNNER_RULES = {"binance": (0.05, 15 * 60), "bsc": (0.20, 60 * 60)}
PROTECTION_EXCHANGES = {"binance"}   # STOP_MARKET closePosition vía Algo API (ccxt)


def protection_price(entry: float, liq: float, fraction: float = PROTECTION_LIQ_FRACTION) -> float:
    return entry + fraction * (liq - entry)


def protection_spot_price(sl: float, offset: float = PROTECTION_SPOT_OFFSET) -> float:
    return sl * (1 - offset)


def _short_liq_price(perp_cli, perp_sym: str):
    try:
        for p in perp_cli.fetch_positions([perp_sym]):
            if p.get("contracts") and p.get("side") in (None, "short") and p.get("liquidationPrice"):
                return float(p["liquidationPrice"])
    except Exception as e:
        log.warning(f"liquidationPrice {perp_sym} failed: {e}")
    return None


def _protection_clients(creds_by_exchange, position):
    exchange = position.get("exchange", "")
    if exchange.lower() not in PROTECTION_EXCHANGES:
        raise ExecError(f"Seguros automáticos no soportados en {exchange}")
    creds = creds_by_exchange.get(exchange.lower())
    if not creds:
        raise ExecError(f"Sin API keys para {exchange}")
    spot_cli = _build_client(exchange, creds, default_type="spot")
    perp_cli = _build_client(exchange, creds, default_type="swap")
    _ensure_markets(spot_cli)
    _ensure_markets(perp_cli)
    sym = position.get("symbol", "")
    return spot_cli, perp_cli, to_ccxt_symbol(sym, "spot"), to_ccxt_symbol(sym, "swap")


def place_protection(creds_by_exchange: dict, position: dict, ctx: ExecContext = None,
                     wallet: dict = None, token: dict = None) -> dict:
    """Coloca SL (STOP_MARKET closePosition, mark price) + venta límite del spot.

    Cada pierna es independiente: si una falla, la otra se conserva y el error
    queda en `errors`. Devuelve el dict a persistir en user_positions.protection_json
    (ok=False si no se pudo colocar ninguna).
    """
    ctx = ctx or ExecContext()
    try:
        spot_cli, perp_cli, spot_sym, perp_sym = _protection_clients(creds_by_exchange, position)
    except ExecError as e:
        return {"ok": False, "msg": str(e)}
    except Exception as e:
        return {"ok": False, "msg": f"No se pudo conectar a {position.get('exchange')}: {e}"}

    entry = float(position.get("entry_price") or 0)
    liq, liq_source = _short_liq_price(perp_cli, perp_sym), "exchange"
    if not liq and entry:
        _, mmr = binance_max_leverage(perp_cli, perp_sym)
        liq = liquidation_estimate(entry, position.get("leverage") or 1, mmr or 0.004)["liq_price"]
        liq_source = "estimate"
    if not entry or not liq or liq <= entry:
        return {"ok": False, "msg": f"No se pudo determinar entrada/liquidación (entry={entry}, liq={liq})"}
    sl = float(perp_cli.price_to_precision(perp_sym, protection_price(entry, liq)))
    sp = protection_spot_price(sl)
    prot = {"status": "active", "sl_price": sl, "spot_price": sp, "liq_price": liq,
            "liq_source": liq_source, "entry_price": entry, "fraction": PROTECTION_LIQ_FRACTION,
            "spot_offset": PROTECTION_SPOT_OFFSET, "placed_at": time.time(),
            "perp": None, "spot": None, "errors": []}

    try:
        params = {"closePosition": True, "workingType": "MARK_PRICE", "triggerPrice": sl}
        if _is_hedged(perp_cli, perp_sym) is True:
            params["positionSide"] = "SHORT"
        o = perp_cli.create_order(perp_sym, "STOP_MARKET", "buy", None, None,
                                  _with_coid(params, ctx.coid("sl")))
        prot["perp"] = {"id": str(o.get("id")), "status": "open"}
    except Exception as e:
        log.error(f"SL {perp_sym} failed: {e}")
        prot["errors"].append(f"SL del short: {e}")

    venue = position.get("spot_venue") or "binance"
    try:
        if not float(position.get("spot_qty") or 0):
            pass                           # short suelto: no queda spot que proteger
        elif venue == "bsc":
            from exchanges import kyber_limit_order as klo
            from exchanges import onchain_bsc as bsc
            if not wallet or not token:
                raise ExecError("falta wallet/token BSC")
            held = bsc.from_wei(bsc.erc20_balance(token["address"], wallet["address"]), int(token["decimals"]))
            qty = min(float(position.get("spot_qty") or 0), held)
            lo = klo.place_sell(wallet, token, qty, sp)
            prot["spot"] = {"venue": "bsc", "id": lo["id"], "status": "open", "qty": qty,
                            "expires_at": lo["expires_at"]}
        else:
            qty = _spot_sellable(spot_cli, spot_sym, float(position.get("spot_qty") or 0))
            sp = prot["spot_price"] = float(spot_cli.price_to_precision(spot_sym, sp))
            if qty <= 0 or not _min_ok(spot_cli, spot_sym, qty, sp):
                raise ExecError(f"cantidad spot {qty:g} bajo el mínimo")
            o = spot_cli.create_order(spot_sym, "limit", "sell", qty, sp, _with_coid({}, ctx.coid("tp")))
            prot["spot"] = {"venue": "binance", "id": str(o.get("id")), "status": "open", "qty": qty}
    except Exception as e:
        log.error(f"venta límite spot {spot_sym} failed: {e}")
        prot["errors"].append(f"Venta límite spot: {e}")

    prot["ok"] = bool(prot["perp"] or prot["spot"])
    if not prot["ok"]:
        prot["msg"] = "; ".join(prot["errors"])
    return prot


def runner_rule(position: dict) -> tuple:
    return RUNNER_RULES.get(position.get("spot_venue") or "binance", RUNNER_RULES["binance"])


def place_runner_tp(creds_by_exchange: dict, position: dict, spot_fill_price: float,
                    ctx: ExecContext = None) -> dict:
    """Take profit del short suelto (caso B): TAKE_PROFIT_MARKET closePosition a
    −caída del precio de la venta spot (last price, RUNNER_RULES). Va por la Algo API."""
    ctx = ctx or ExecContext()
    _, perp_cli, _, perp_sym = _protection_clients(creds_by_exchange, position)
    fall, max_secs = runner_rule(position)
    px = float(perp_cli.price_to_precision(perp_sym, spot_fill_price * (1 - fall)))
    params = {"closePosition": True, "workingType": "CONTRACT_PRICE", "triggerPrice": px}
    if _is_hedged(perp_cli, perp_sym) is True:
        params["positionSide"] = "SHORT"
    o = perp_cli.create_order(perp_sym, "TAKE_PROFIT_MARKET", "buy", None, None,
                              _with_coid(params, ctx.coid("rtp")))
    return {"id": str(o.get("id")), "status": "open", "price": px, "max_secs": max_secs}


def _leg_active(leg) -> bool:
    return bool(leg and leg.get("id") and leg.get("status") == "open")


_DEAD_STATUSES = ("canceled", "cancelled", "expired", "rejected")


def _algo_state(o: dict) -> str:
    """Estado de una orden trigger (SL): filled|open|canceled|unknown.

    Con actualOrderId la condición ya se disparó (orden de mercado real) aunque
    el status no diga 'closed'. Un status no reconocido NUNCA se lee como
    cancelado: 'unknown' → el vigilante no lo marca ni alerta en falso.
    """
    st = (o.get("status") or "").lower()
    if st == "closed" or (o.get("info") or {}).get("actualOrderId"):
        return "filled"
    if st == "open":
        return "open"
    if st in _DEAD_STATUSES:
        return "canceled"
    return "unknown"


def _limit_state(o: dict) -> str:
    """Estado de la venta límite spot. Un llenado PARCIAL cuenta como 'filled'
    (el spot vendido ya no está cubierto por el short completo)."""
    st = (o.get("status") or "").lower()
    if st == "closed" or float(o.get("filled") or 0) > 0:
        return "filled"
    if st == "open":
        return "open"
    if st in _DEAD_STATUSES:
        return "canceled"
    return "unknown"


def protection_status(creds_by_exchange: dict, position: dict, wallet: dict = None) -> dict:
    """Estado real de los seguros: {perp, spot} ∈ open|filled|canceled|missing|unknown,
    con fills (`perp_fill`, `spot_fill`) cuando hay ejecución."""
    prot = position.get("protection") or {}
    out = {"perp": "missing", "spot": "missing", "tp": "missing"}
    spot_cli, perp_cli, spot_sym, perp_sym = _protection_clients(creds_by_exchange, position)
    for leg in ("perp", "tp"):             # SL duro y take profit del short suelto
        if not _leg_active(prot.get(leg)):
            continue
        try:
            o = perp_cli.fetch_order(prot[leg]["id"], perp_sym, {"trigger": True})
            out[leg] = _algo_state(o)
            if out[leg] == "filled":
                actual = (o.get("info") or {}).get("actualOrderId")
                out[f"{leg}_fill"] = order_fills(perp_cli, perp_sym, actual) if actual else None
        except Exception as e:
            log.warning(f"{leg} status {perp_sym}: {e}")
            out[leg] = "unknown"
    spot = prot.get("spot")
    if _leg_active(spot):
        try:
            if spot.get("venue") == "bsc":
                from exchanges import kyber_limit_order as klo
                o = klo.get_order(wallet["address"], spot["id"]) if wallet else None
                st = (o or {}).get("status")
                filled = int((o or {}).get("filledMakingAmount") or 0)
                if st == "filled" or (filled and st not in klo.ACTIVE_STATUSES):
                    out["spot"] = "filled"
                    out["spot_full"] = st == "filled"
                    qty, usdt = _bsc_filled(spot, o)
                    out["spot_fill"] = {"gross": qty, "cost": usdt, "fee_usd": 0.0}
                elif st in klo.ACTIVE_STATUSES:
                    out["spot"] = "open"
                else:
                    out["spot"] = "canceled" if o else "unknown"   # cancelled/expired
            else:
                o = spot_cli.fetch_order(spot["id"], spot_sym)
                out["spot"] = _limit_state(o)
                if out["spot"] == "filled":
                    out["spot_full"] = (o.get("status") or "").lower() == "closed"
                    out["spot_fill"] = order_fills(spot_cli, spot_sym, spot["id"])
        except Exception as e:
            log.warning(f"spot protection status {spot_sym}: {e}")
            out["spot"] = "unknown"
    return out


_BSC_LIVE = ("open", "partially_filled")


def _bsc_filled(spot, o) -> tuple:
    """(base vendida, USDT recibidos) de una orden límite Kyber."""
    o = o or {}
    filled, making = int(o.get("filledMakingAmount") or 0), int(o.get("makingAmount") or 0)
    if not (filled and making):
        return 0.0, 0.0
    taking = int(o.get("filledTakingAmount") or 0) or int(o.get("takingAmount") or 0) * filled // making
    return float(spot.get("qty") or 0) * filled / making, taking / 1e18   # USDT BSC: 18 dec


def _cancel_bsc_limit(wallet, spot) -> tuple:
    """Cancela la venta límite Kyber de forma idempotente → (viva, base_llenada, usdt).

    Una orden ya cancelada/expirada no es error: el reintento del cierre de AGT #44
    (2026-10-23) abortaba con PROTECTION_CANCEL porque el job anterior ya la había
    cancelado. 'cancelling' cuenta como cancelada (al vender, la orden queda sin saldo).
    """
    from exchanges import kyber_limit_order as klo
    oid, o, cancelled = spot["id"], None, False
    for attempt in range(3):
        try:
            o = klo.get_order(wallet["address"], oid)
            if o is None or o.get("status") in _BSC_LIVE:
                klo.cancel(wallet, [oid])
                cancelled = True
                o = klo.get_order(wallet["address"], oid)
            if (o is None and cancelled) or (o is not None and o.get("status") not in _BSC_LIVE):
                break
        except Exception as e:
            log.warning(f"cancel venta límite BSC {oid} intento {attempt + 1}: {e}")
        time.sleep(1)
    alive = o.get("status") in _BSC_LIVE if o is not None else not cancelled
    return (alive, *_bsc_filled(spot, o))


def _cancel_protection_clients(spot_cli, perp_cli, spot_sym, perp_sym, prot, wallet=None) -> dict:
    """Cancela las órdenes de seguro vivas. Devuelve fees/qty de lo que ya se
    había llenado (para sumarlo al cierre)."""
    out = {"perp_filled": 0.0, "perp_cost": 0.0, "tp_filled": 0.0, "spot_filled": 0.0,
           "spot_cost": 0.0, "fee_usd": 0.0}
    alive = []
    for leg, label in (("perp", "SL del short"), ("tp", "take profit del short")):
        perp = prot.get(leg)
        if not _leg_active(perp):
            continue
        o = None
        for attempt in range(3):
            try:
                o = perp_cli.fetch_order(perp["id"], perp_sym, {"trigger": True})
                if _algo_state(o) == "open":
                    perp_cli.cancel_order(perp["id"], perp_sym, {"trigger": True})
                    o = perp_cli.fetch_order(perp["id"], perp_sym, {"trigger": True})
                if _algo_state(o) != "open":
                    break
            except Exception as e:
                log.warning(f"cancel {label} {perp_sym} intento {attempt + 1}: {e}")
                o = None
            time.sleep(1)
        if o is None or _algo_state(o) == "open":
            # Un closePosition vivo cerraría también una posición futura del símbolo.
            alive.append(f"{label} {perp['id']}")
        elif _algo_state(o) == "filled" and (o.get("info") or {}).get("actualOrderId"):
            f = order_fills(perp_cli, perp_sym, o["info"]["actualOrderId"])
            out["perp_filled"] += f["gross"]
            out["perp_cost"] += f["cost"]
            out["fee_usd"] += f["fee_usd"]
            if leg == "tp":
                out["tp_filled"] = f["gross"]
    spot = prot.get("spot")
    if _leg_active(spot):
        if spot.get("venue") == "bsc":
            if wallet:
                bsc_alive, filled, cost = _cancel_bsc_limit(wallet, spot)
                if bsc_alive:
                    alive.append(f"venta límite BSC {spot['id']}")
                out["spot_filled"], out["spot_cost"] = filled, cost
        else:
            o = None
            for attempt in range(3):
                try:
                    o = spot_cli.fetch_order(spot["id"], spot_sym)
                    if (o.get("status") or "").lower() == "open":
                        spot_cli.cancel_order(spot["id"], spot_sym)
                        o = _final_order(spot_cli, spot["id"], spot_sym) or o
                    if (o.get("status") or "").lower() != "open":
                        break
                except Exception as e:
                    log.warning(f"cancel venta límite {spot_sym} intento {attempt + 1}: {e}")
                    o = None
                time.sleep(1)
            if o is None or (o.get("status") or "").lower() == "open":
                # Con la límite viva el spot sigue bloqueado y se vendería solo después.
                alive.append(f"venta límite spot {spot['id']}")
            elif float(o.get("filled") or 0) > 0:
                f = order_fills(spot_cli, spot_sym, spot["id"], expected_filled=o.get("filled"))
                out["spot_filled"], out["spot_cost"] = f["gross"], f["cost"]
                out["fee_usd"] += f["fee_usd"]
    if alive:
        raise ExecError("No se pudieron cancelar los seguros (" + ", ".join(alive)
                        + "); cierre abortado sin operar. Reintenta o cancélalos en Binance.",
                        code="PROTECTION_CANCEL")
    return out


def short_health(creds_by_exchange: dict, position: dict) -> dict:
    """Estado real del short: {short (base), mark, liq, liq_distance_pct}.

    short=None si no se pudo leer. Lo usa el vigilante para detectar un short
    liquidado/cerrado fuera de basyo (spot desnudo) y la cercanía a liquidación.
    """
    _, perp_cli, _, perp_sym = _protection_clients(creds_by_exchange, position)
    out = {"short": None, "mark": None, "liq": None, "liq_distance_pct": None}
    try:
        positions = perp_cli.fetch_positions([perp_sym])
    except Exception as e:
        log.warning(f"short_health {perp_sym}: {e}")
        return out
    cs = _contract_size(perp_cli, perp_sym)
    out["short"] = 0.0
    for p in positions:
        if not p.get("contracts") or p.get("side") not in (None, "short"):
            continue
        out["short"] = abs(float(p["contracts"])) * cs
        mark = p.get("markPrice") or (p.get("info") or {}).get("markPrice")
        liq = p.get("liquidationPrice")
        out["mark"] = float(mark) if mark else None
        out["liq"] = float(liq) if liq else None
        if out["mark"] and out["liq"]:
            out["liq_distance_pct"] = round((out["liq"] - out["mark"]) / out["mark"] * 100, 2)
        break
    return out


def cancel_protection(creds_by_exchange: dict, position: dict, wallet: dict = None) -> dict:
    prot = position.get("protection") or {}
    if not prot:
        return {}
    spot_cli, perp_cli, spot_sym, perp_sym = _protection_clients(creds_by_exchange, position)
    return _cancel_protection_clients(spot_cli, perp_cli, spot_sym, perp_sym, prot, wallet)


# ── Public: connection test + spot gate ────────────────────────────────────
def test_connection(exchange_name: str, creds: dict) -> dict:
    """Validate the user's keys by fetching the futures balance."""
    if not is_cex(exchange_name):
        return {"ok": False, "msg": f"{exchange_name} no soportado para auto-trading"}
    if not creds or not creds.get("api_key") or not creds.get("api_secret"):
        return {"ok": False, "msg": "Faltan API key / secret"}
    if exchange_name.lower() == "binance":
        from exchanges.binance_account import BinanceAccount
        return BinanceAccount(creds).check()
    try:
        client = _build_client(exchange_name, creds, default_type="swap")
        bal = client.fetch_balance()
        usdt = (bal.get("total") or {}).get("USDT")
        return {"ok": True, "msg": "Conexión OK", "usdt_balance": usdt}
    except Exception as e:
        return {"ok": False, "msg": f"Conexión fallida: {type(e).__name__}: {e}"}


def spot_tradeable(exchange_name: str, creds: dict, symbol: str) -> bool:
    """True iff the base symbol has a tradeable CENTRALIZED spot market."""
    try:
        client = _build_client(exchange_name, creds, default_type="spot")
        _ensure_markets(client)
        sym = to_ccxt_symbol(symbol, "spot")
        m = client.markets.get(sym)
        return bool(m and m.get("spot") and m.get("active", True))
    except Exception as e:
        log.warning(f"spot_tradeable check failed {exchange_name}/{symbol}: {e}")
        return False


# ── Spot legs ───────────────────────────────────────────────────────────────
class CexSpotLeg:
    """Pierna spot en un CEX: maker post-only con re-precio + taker opcional.

    buy/sell llaman on_fill(net_base_delta) cada vez que crece lo llenado, para
    que el caller cubra el perp de inmediato.
    """

    venue = "cex"

    def __init__(self, client, sym, ctx: ExecContext, perp_cli, perp_sym):
        self.c = client
        self.sym = sym
        self.ctx = ctx
        self.perp_cli = perp_cli
        self.perp_sym = perp_sym
        self.base = client.market(sym)["base"]
        self.px_cache = {}
        self.fee_usd = 0.0
        self.order_ids = []

    def _maker_px(self, side):
        sb, sa, _ = _top_of_book(self.c, self.sym)
        pb, pa, _ = _top_of_book(self.perp_cli, self.perp_sym)
        if side == "buy":
            # Comprar al bid, sin pagar más que perp_bid·(1+MAX_NEG_BASIS).
            cap = pb * (1 + MAX_NEG_BASIS) if pb else sb
            return min(sb, cap), sa, pb
        # Vender al ask, sin cobrar menos que perp_ask·(1−MAX_NEG_BASIS).
        floor_ = pa * (1 - MAX_NEG_BASIS) if pa else sa
        return max(sa, floor_), sb, pa

    def _run_order(self, side, amount, price, typ, on_fill, deadline):
        """Coloca una orden y la sigue hasta llenarse o `deadline`.

        Limit: cada REPRICE_SECS compara con el mejor precio actual y solo
        cancela/re-coloca si cambió (conserva la prioridad en la cola). La orden
        se cancela SIEMPRE al salir (también si la cobertura lanza). Devuelve
        los fills reales de la orden.
        """
        params = _with_coid({"postOnly": True} if typ == "limit" else {}, self.ctx.coid("s"))
        # Idempotente: si create_order lanza pero la orden entró, se sigue con ella
        # (si no, quedaría viva sin rastrear y podría llenarse sin cobertura).
        o = _create_idempotent(self.c, self.sym, typ, side, amount,
                               price if typ == "limit" else None, params)
        oid = o["id"]
        self.order_ids.append(oid)
        seen = 0.0
        terminal = False
        hedge_err = None
        next_reprice = time.time() + REPRICE_SECS
        try:
            while True:
                if typ == "limit":
                    time.sleep(POLL_INTERVAL)
                try:
                    last = self.c.fetch_order(oid, self.sym)
                except Exception as e:
                    log.warning(f"fetch_order {oid}: {e}")
                    last = {}
                status = last.get("status")
                filled = float(last.get("filled") or 0)
                if filled > seen:
                    f = order_fills(self.c, self.sym, oid, self.px_cache, expected_filled=filled)
                    seen = filled
                    try:
                        on_fill(oid, f, f["gross"] - (f["fee_base"] if side == "buy" else 0))
                    except ExecError as e:
                        # La cobertura falló: dejar de operar, pero contar igual los
                        # fills tardíos (tras cancelar) para deshacer el spot correcto.
                        hedge_err = e
                        break
                if status in ("closed", "canceled", "rejected", "expired"):
                    terminal = True
                    break
                if time.time() > deadline or self.ctx.cancelled():
                    break
                if typ == "limit" and time.time() >= next_reprice:
                    next_reprice = time.time() + REPRICE_SECS
                    new_px, _, _ = self._maker_px(side)
                    try:
                        new_px = float(self.c.price_to_precision(self.sym, new_px))
                    except Exception:
                        pass
                    if new_px and new_px != price:
                        break  # re-colocar a precio fresco
                if typ != "limit":
                    time.sleep(0.5)
        finally:
            final = None
            if not terminal:
                _safe_cancel(self.c, oid, self.sym)
                final = _final_order(self.c, oid, self.sym)
        f = order_fills(self.c, self.sym, oid, self.px_cache,
                        expected_filled=(final or {}).get("filled"))
        if hedge_err is not None:
            try:
                on_fill(oid, f, f["gross"] - (f["fee_base"] if side == "buy" else 0))
            except ExecError:
                pass
            raise hedge_err
        on_fill(oid, f, f["gross"] - (f["fee_base"] if side == "buy" else 0))
        if not terminal and (final is None or (final.get("status") or "").lower() == "open"):
            # No se pudo confirmar la cancelación: no colocar otra orden encima.
            raise ExecError(f"No se pudo cancelar la orden spot {oid}; ejecución detenida "
                            f"— revisa órdenes abiertas en {self.sym}", code="CANCEL_FAILED")
        return f

    def trade(self, side, target_base, on_fill, window, taker_ok):
        """Compra/vende ~target_base. Devuelve base bruto ejecutado.

        Maker al mejor precio del libro (re-precio solo si se mueve) hasta
        `window`; luego taker por el resto si taker_ok(precio_taker, ref_perp).
        """
        done_gross = 0.0
        deadline = time.time() + window
        while time.time() < deadline and not self.ctx.cancelled():
            amt = _norm_amount(self.c, self.sym, target_base - done_gross)
            px, _, _ = self._maker_px(side)
            if amt <= 0 or not px or not _min_ok(self.c, self.sym, amt, px):
                break
            px = float(self.c.price_to_precision(self.sym, px))
            try:
                f = self._run_order(side, amt, px, "limit", on_fill, deadline)
            except ExecError:
                raise
            except Exception as e:
                # LIMIT_MAKER rechazada (cruzaría el libro) → reintento con precio fresco.
                log.info(f"maker {side} {self.sym} rechazada: {e}")
                time.sleep(POLL_INTERVAL)
                continue
            done_gross += f["gross"]
            self.fee_usd += f["fee_usd"]
        amt = _norm_amount(self.c, self.sym, target_base - done_gross)
        _, taker_px, ref = self._maker_px(side)
        if amt > 0 and not self.ctx.cancelled() and _min_ok(self.c, self.sym, amt, taker_px)                 and taker_ok(taker_px, ref):
            f = self._run_order(side, amt, None, "market", on_fill, time.time() + 15)
            done_gross += f["gross"]
            self.fee_usd += f["fee_usd"]
        return done_gross


class PerpHedger:
    """Cubre el perp a mercado siguiendo lo neto llenado en el spot.

    Abre short (open) o recompra reduceOnly (close). Acumula hasta que el
    delta supere los mínimos del perp; el residuo < paso queda como polvo.
    """

    def __init__(self, client, sym, ctx: ExecContext, opening: bool, max_base=None):
        self.c = client
        self.sym = sym
        self.ctx = ctx
        self.opening = opening
        self.max_base = max_base
        self.hedged = 0.0      # base cubierta
        self.fee_usd = 0.0
        self.cost = 0.0
        self.order_ids = []
        self.px_cache = {}
        self.cs = _contract_size(client, sym)
        self.failed = None

    def target(self, base_target: float, force_all=False):
        """Lleva lo cubierto a base_target (≤ max_base). Devuelve error o None."""
        if self.max_base is not None:
            base_target = min(base_target, self.max_base)
        delta = base_target - self.hedged
        amt = _norm_amount(self.c, self.sym, delta)
        if amt <= 0:
            return None
        _, _, mid = _top_of_book(self.c, self.sym)
        if not force_all and not _min_ok(self.c, self.sym, amt, mid):
            return None
        side = "sell" if self.opening else "buy"
        params = _perp_open_params(self.c, self.sym) if self.opening \
            else _perp_close_params(self.c, self.sym)
        last_err = None
        # Mismo clientOrderId en el reintento: si el primer intento entró (timeout
        # con orden aceptada), Binance rechaza el duplicado y _create_idempotent la
        # encuentra → nunca se duplica el short ni la recompra.
        coid = self.ctx.coid("p")
        for attempt in range(2):
            try:
                o = _create_idempotent(self.c, self.sym, "market", side, amt, None,
                                       _with_coid(params, coid))
                self.order_ids.append(o["id"])
                f = order_fills(self.c, self.sym, o["id"], self.px_cache,
                                expected_filled=o.get("filled") or None)
                got = f["gross"] or amt * self.cs
                self.hedged += got
                self.cost += f["cost"] or got * (f["avg"] or mid)
                self.fee_usd += f["fee_usd"]
                return None
            except Exception as e:
                last_err = e
                log.error(f"perp hedge {side} {self.sym} intento {attempt + 1} falló: {e}")
                time.sleep(1)
        self.failed = str(last_err)
        return self.failed

    @property
    def avg(self):
        return self.cost / self.hedged if self.hedged else 0.0


# ── Public: open ────────────────────────────────────────────────────────────
def _plan_spot_perp(spot_cli, perp_cli, spot_sym, perp_sym, capital, leverage, fallback_px):
    fut_margin = capital / (leverage + 1)
    spot_size = capital - fut_margin      # USD long spot
    sb, sa, smid = _top_of_book(spot_cli, spot_sym, fallback_px)
    pb, pa, pmid = _top_of_book(perp_cli, perp_sym, fallback_px)
    if smid <= 0 or pmid <= 0:
        raise ExecError("No se pudo obtener precio de mercado")
    base_target = spot_size / sb if sb else spot_size / smid
    spot_amt = _norm_amount(spot_cli, spot_sym, base_target)
    perp_amt = _norm_amount(perp_cli, perp_sym, base_target)
    err = _check_min_notional(spot_cli, spot_sym, spot_amt, sb or smid) \
        or _check_min_notional(perp_cli, perp_sym, perp_amt, pmid)
    if err:
        raise ExecError(f"Orden bajo el mínimo: {err}")
    return {
        "fut_margin": fut_margin, "spot_size": spot_size, "base_target": base_target,
        "spot_amt": spot_amt, "perp_amt": perp_amt,
        "spot_bid": sb, "spot_ask": sa, "perp_bid": pb, "perp_ask": pa, "perp_mid": pmid,
        "basis_pct": round((pb - sb) / sb * 100, 4) if sb else None,
    }


def twap_suggestion(spot_cli, spot_sym, notional_usd: float, threshold_usd=5000.0) -> dict:
    """Sugiere TWAP si el nocional > 25% de la profundidad ±0.2% o > umbral."""
    depth = None
    try:
        ob = spot_cli.fetch_order_book(spot_sym, limit=100)
        bid = ob["bids"][0][0] if ob.get("bids") else 0
        ask = ob["asks"][0][0] if ob.get("asks") else 0
        mid = (bid + ask) / 2 if bid and ask else 0
        if mid:
            depth = sum(p * q for p, q, *_ in ob["asks"] if p <= mid * 1.002)
    except Exception as e:
        log.debug(f"order book {spot_sym}: {e}")
    suggest = notional_usd > threshold_usd or (depth is not None and notional_usd > 0.25 * depth)
    slices = 1
    if suggest:
        base = depth * 0.1 if depth else threshold_usd / 5
        slices = int(min(48, max(4, math.ceil(notional_usd / max(base, 1)))))
    return {"suggest": bool(suggest), "depth_usd_0_2pct": round(depth, 2) if depth else None,
            "slices": slices, "duration_s": slices * 120 if suggest else 0}


def execute_open(creds_by_exchange: dict, opp: dict, capital: float,
                 leverage: int = 1, dry_run: bool = False,
                 allow_market_fallback: bool = True, ctx: ExecContext = None,
                 twap: dict = None, spot_leg_factory=None) -> dict:
    """Place the real orders for an opportunity. Returns a result dict.

    Success: {ok:True, dry_run, entry_price, entry_fees_usd, spot_qty, perp_qty,
              order_ids[], legs[], entry_mode, basis_pct}
    Failure: {ok:False, msg, code?, unwound?:bool, partial?:{...}}

    twap: {slices, duration_s} → TWAP pareado. spot_leg_factory: pierna spot
    alternativa (on-chain BSC); por defecto CexSpotLeg.
    """
    ctx = ctx or ExecContext()
    mode = opp.get("mode", "spot_perp")
    symbol = opp.get("symbol", "")
    leverage = max(1, int(leverage))
    try:
        if mode == "spot_perp":
            return _open_spot_perp(creds_by_exchange, opp, symbol, capital, leverage,
                                   dry_run, allow_market_fallback, ctx, twap or {},
                                   spot_leg_factory)
        if mode == "cross_exchange":
            return _open_cross(creds_by_exchange, opp, symbol, capital, leverage, dry_run, ctx)
    except ExecError as e:
        return {"ok": False, "msg": str(e), "code": e.code}
    return {"ok": False, "msg": f"Modo '{mode}' no soportado en auto (usa flujo manual)"}


def _open_spot_perp(creds_by_exchange, opp, symbol, capital, leverage, dry_run,
                    allow_market_fallback, ctx, twap, spot_leg_factory):
    exchange = opp.get("exchange", "")
    if not is_cex(exchange):
        return {"ok": False, "msg": f"{exchange} no es CEX soportado"}
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

    onchain = spot_leg_factory is not None
    if onchain:
        # Spot on-chain: el plan se dimensiona contra el perp (no hay spot CEX).
        pb, pa, pmid = _top_of_book(perp_cli, perp_sym, opp.get("price", 0))
        fut_margin = capital / (leverage + 1)
        spot_size = capital - fut_margin
        plan = {"fut_margin": fut_margin, "spot_size": spot_size,
                "base_target": spot_size / pmid if pmid else 0, "perp_mid": pmid,
                "perp_bid": pb, "spot_bid": None, "basis_pct": None,
                "spot_amt": None, "perp_amt": _norm_amount(perp_cli, perp_sym, spot_size / pmid if pmid else 0)}
        err = _check_min_notional(perp_cli, perp_sym, plan["perp_amt"], pmid)
        if err:
            raise ExecError(f"Orden bajo el mínimo: {err}")
    else:
        plan = _plan_spot_perp(spot_cli, perp_cli, spot_sym, perp_sym, capital, leverage,
                               opp.get("price", 0))

    is_binance = exchange.lower() == "binance"
    max_lev, mmr = (binance_max_leverage(perp_cli, perp_sym) if is_binance else (None, None))
    liq = liquidation_estimate(plan["perp_mid"], leverage, mmr or 0.004)
    if is_binance and liq.get("liq_price"):
        liq["sl_price"] = protection_price(plan["perp_mid"], liq["liq_price"])

    if dry_run:
        sugg = twap_suggestion(spot_cli, spot_sym, plan["spot_size"]) if not onchain else \
            {"suggest": plan["spot_size"] > 5000, "slices": 1, "duration_s": 0}
        if onchain:
            # Cotización real del agregador: valida acceso (región/keys) y basis.
            try:
                leg = spot_leg_factory(perp_cli, perp_sym, ctx)
                px = leg.quote_price(plan["base_target"], "buy", ref_px=plan["perp_bid"])
                plan["spot_bid"] = px
                plan["spot_amt"] = plan["base_target"]
                if px:
                    plan["basis_pct"] = round((plan["perp_bid"] - px) / px * 100, 4)
            except Exception as e:
                raise ExecError(f"No se pudo cotizar el spot on-chain: {e}", code="ONCHAIN_QUOTE")
        return {
            "ok": True, "dry_run": True,
            "entry_price": plan["perp_mid"],
            "entry_fees_usd": round(plan["spot_size"] * 2 * 0.0006, 4),
            "order_ids": [],
            "entry_mode": "onchain_swap" if onchain else "maker_paired",
            "basis_pct": plan["basis_pct"],
            "leverage": leverage, "max_leverage": max_lev,
            "liquidation": liq, "twap": sugg,
            "spot_venue": "bsc" if onchain else exchange.lower(),
            "legs": [
                {"side": "buy", "kind": "spot", "exchange": "BSC" if onchain else exchange,
                 "symbol": spot_sym, "amount": plan["spot_amt"],
                 "price": plan["spot_bid"], "type": "swap" if onchain else "limit maker"},
                {"side": "sell", "kind": "perp", "exchange": exchange, "symbol": perp_sym,
                 "amount": plan["perp_amt"], "price": plan["perp_mid"], "type": "market"},
            ],
        }

    if is_binance:
        binance_preflight(perp_cli, perp_sym, leverage)
    mode_err = _ensure_one_way_or_abort(perp_cli, perp_sym)
    if mode_err:
        return {"ok": False, "msg": mode_err}
    cfg_err = _ensure_margin_and_leverage(perp_cli, perp_sym, leverage, side="short")
    if cfg_err:
        return {"ok": False, "msg": cfg_err}

    leg = spot_leg_factory(perp_cli, perp_sym, ctx) if onchain \
        else CexSpotLeg(spot_cli, spot_sym, ctx, perp_cli, perp_sym)
    hedger = PerpHedger(perp_cli, perp_sym, ctx, opening=True)
    state = {"spot_net": 0.0, "spot_cost": 0.0, "spot_gross": 0.0, "per_order": {},
             "ref": _ref_mids(spot_cli, spot_sym, perp_cli, perp_sym, leg if onchain else None,
                              plan.get("perp_mid") or 0)}

    def on_fill(oid, f, net):
        # f/net son acumulados de ESA orden → recomputar el total neto.
        state["per_order"][oid] = (net, f["cost"], f["gross"])
        state["spot_net"] = sum(v[0] for v in state["per_order"].values())
        state["spot_cost"] = sum(v[1] for v in state["per_order"].values())
        state["spot_gross"] = sum(v[2] for v in state["per_order"].values())
        err = hedger.target(state["spot_net"])
        ctx.progress({"phase": "open", "spot_net": state["spot_net"],
                      "perp_hedged": hedger.hedged, "slices_done": state.get("slice", 0)})
        if err:
            raise ExecError(f"Falló la cobertura perp: {err}", code="HEDGE_FAILED")

    def taker_ok(spot_ask, perp_bid):
        if not allow_market_fallback:
            return False
        return bool(spot_ask and perp_bid and spot_ask <= perp_bid * (1 + MAX_NEG_BASIS))

    slices = max(1, int((twap or {}).get("slices") or 1))
    duration = float((twap or {}).get("duration_s") or 0)
    interval = duration / slices if slices > 1 else 0
    window = SPOT_LIMIT_TIMEOUT if slices == 1 else max(15, min(SPOT_LIMIT_TIMEOUT, interval * 0.8))
    base_target = plan["base_target"]
    error = None
    try:
        for i in range(slices):
            if ctx.cancelled():
                break
            state["slice"] = i
            t0 = time.time()
            slice_target = (base_target * (i + 1) / slices)
            # Objetivo acumulado: compensa slices que llenaron de menos.
            need = slice_target - state["spot_net"]
            if need > 0:
                leg.trade("buy", need, on_fill, window, taker_ok)
            if i < slices - 1:
                rest = interval - (time.time() - t0)
                while rest > 0 and not ctx.cancelled():
                    time.sleep(min(5, rest))
                    rest -= 5
    except ExecError as e:
        error = e
    except Exception as e:
        error = ExecError(f"Error en la pierna spot: {e}")

    # Cierre de la cobertura: todo lo neto comprado debe quedar cubierto.
    if hedger.failed is None:
        hedger.target(state["spot_net"])

    if hedger.failed is not None:
        # No se pudo cubrir → vender lo no cubierto para no quedar direccional.
        unhedged = max(0.0, state["spot_net"] - hedger.hedged)
        unwound = _unwind_spot(leg, unhedged)
        partial = None
        if hedger.hedged > 0:
            # La posición guardada es SOLO lo cubierto (lo deshecho ya no está en manos).
            pstate = dict(state)
            if unwound and state["spot_net"] > hedger.hedged:
                k = hedger.hedged / state["spot_net"]
                pstate.update(spot_net=hedger.hedged, spot_cost=state["spot_cost"] * k,
                              spot_gross=(state.get("spot_gross") or state["spot_net"]) * k)
            partial = _open_result(exchange, spot_sym, perp_sym, leg, hedger, pstate,
                                   leverage, onchain)
        return {"ok": False, "code": "HEDGE_FAILED", "unwound": unwound,
                "msg": (f"Falló el short perp: {hedger.failed}. Spot no cubierto "
                        f"{'deshecho' if unwound else 'NO deshecho — REVISA MANUALMENTE'}."),
                "partial": partial}

    if hedger.hedged <= 0:
        msg = str(error) if error else ("No se logró basis favorable ni llenar el spot en la "
                                        "ventana; abortado sin abrir posición")
        if state["spot_net"] > 0:
            unwound = _unwind_spot(leg, state["spot_net"])
            msg += f" (spot residual {'vendido' if unwound else 'NO vendido — revisa'})"
        return {"ok": False, "msg": msg, "unwound": True}

    res = _open_result(exchange, spot_sym, perp_sym, leg, hedger, state, leverage, onchain)
    res["ok"] = True
    if error or ctx.cancelled():
        res["partial_note"] = (f"Ejecución parcial: {error}" if error
                               else "Cancelado por el usuario: se guardó lo ejecutado")
    res["filled_ratio"] = round(min(1.0, state["spot_net"] / base_target), 4) if base_target else None
    res["liquidation"] = liq
    return res


def _unwind_spot(leg, base_amt) -> bool:
    if base_amt <= 0:
        return True
    try:
        if isinstance(leg, CexSpotLeg):
            amt = _spot_sellable(leg.c, leg.sym, base_amt)
            if amt > 0 and _min_ok(leg.c, leg.sym, amt, _mid_price(leg.c, leg.sym)):
                leg.c.create_order(leg.sym, "market", "sell", amt, None,
                                   _with_coid({}, leg.ctx.coid("u")))
            return True
        return bool(leg.unwind(base_amt))
    except Exception as e:
        log.error(f"UNWIND spot failed: {e}")
        return False


def _open_result(exchange, spot_sym, perp_sym, leg, hedger, state, leverage, onchain):
    spot_px = state["spot_cost"] / state["spot_net"] if state["spot_net"] else 0
    # Bruto (sin la comisión cobrada en base, que ya va en entry_fees_usd): base del
    # PnL de precio, para no contar la comisión dos veces.
    gross = state.get("spot_gross") or state["spot_net"]
    spot_px_gross = state["spot_cost"] / gross if gross else 0
    perp_px = hedger.avg
    basis = round((perp_px - spot_px_gross) / spot_px_gross * 100, 4) if spot_px_gross and perp_px else None
    spot_ids = list(getattr(leg, "order_ids", []))
    return {
        "dry_run": False,
        "entry_price": perp_px,
        "spot_avg_price": spot_px,
        "spot_avg_price_gross": spot_px_gross,
        "ref_mids": state.get("ref"),
        "entry_slippage": _slip_summary("buy", spot_px_gross, gross, perp_px, hedger.hedged,
                                        state.get("ref")),
        "entry_fees_usd": round(leg.fee_usd + hedger.fee_usd, 6),
        "spot_qty": state["spot_net"],
        "perp_qty": hedger.hedged,
        "dust_qty": max(0.0, state["spot_net"] - hedger.hedged),
        "exposure": hedger.hedged * perp_px,
        "capital_used": state["spot_cost"] + hedger.hedged * perp_px / max(1, leverage),
        "order_ids": spot_ids + hedger.order_ids,
        "entry_mode": "onchain_swap" if onchain else "maker_paired",
        "basis_pct": basis,
        "spot_venue": "bsc" if onchain else exchange.lower(),
        "legs": [
            {"side": "buy", "kind": "spot", "exchange": "BSC" if onchain else exchange,
             "symbol": spot_sym, "amount": state["spot_net"], "price": spot_px,
             "order_ids": spot_ids, "fee_usd": round(leg.fee_usd, 6)},
            {"side": "sell", "kind": "perp", "exchange": exchange, "symbol": perp_sym,
             "amount": hedger.hedged, "price": perp_px,
             "order_ids": hedger.order_ids, "fee_usd": round(hedger.fee_usd, 6)},
        ],
    }


def _open_cross(creds_by_exchange, opp, symbol, capital, leverage, dry_run, ctx):
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
            "order_ids": [], "leverage": leverage,
            "liquidation": liquidation_estimate(short_px, leverage),
            "legs": [
                {"side": "buy", "kind": "perp", "exchange": long_ex, "symbol": sym,
                 "amount": long_amt, "price": long_px, "type": "limit"},
                {"side": "sell", "kind": "perp", "exchange": short_ex, "symbol": sym,
                 "amount": short_amt, "price": short_px, "type": "limit"},
            ],
        }

    for cli in (long_cli, short_cli):
        if getattr(cli, "id", "") == "binance":
            binance_preflight(cli, sym, leverage)
    mode_err = _ensure_one_way_or_abort(long_cli, sym) or _ensure_one_way_or_abort(short_cli, sym)
    if mode_err:
        return {"ok": False, "msg": mode_err}
    cfg_err = (_ensure_margin_and_leverage(long_cli, sym, leverage, side="long")
               or _ensure_margin_and_leverage(short_cli, sym, leverage, side="short"))
    if cfg_err:
        return {"ok": False, "msg": cfg_err}

    try:
        long_order = long_cli.create_order(sym, "limit", "buy", long_amt, long_px,
                                           _with_coid(_perp_open_params(long_cli, sym), ctx.coid("l")))
    except Exception as e:
        return {"ok": False, "msg": f"No se pudo colocar límite long en {long_ex}: {e}"}
    try:
        short_order = short_cli.create_order(sym, "limit", "sell", short_amt, short_px,
                                             _with_coid(_perp_open_params(short_cli, sym), ctx.coid("h")))
    except Exception as e:
        _safe_cancel(long_cli, long_order["id"], sym)
        fin = _final_order(long_cli, long_order["id"], sym) or {}
        if float(fin.get("filled") or 0) > 0:
            _reduce(long_cli, sym, "sell", float(fin["filled"]), ctx)
        return {"ok": False, "msg": f"No se pudo colocar límite short en {short_ex}: {e}",
                "unwound": True}

    # Polling CONJUNTO de ambas piernas dentro de la misma ventana.
    deadline = time.time() + CROSS_LIMIT_TIMEOUT
    lst = {"l": None, "s": None}
    while time.time() < deadline and not ctx.cancelled():
        for k, cli, o in (("l", long_cli, long_order), ("s", short_cli, short_order)):
            try:
                lst[k] = cli.fetch_order(o["id"], sym)
            except Exception as e:
                log.warning(f"fetch_order cross {o['id']}: {e}")
        if all((lst[k] or {}).get("status") == "closed" for k in ("l", "s")):
            break
        time.sleep(POLL_INTERVAL)

    for k, cli, o in (("l", long_cli, long_order), ("s", short_cli, short_order)):
        if (lst[k] or {}).get("status") != "closed":
            _safe_cancel(cli, o["id"], sym)
    lf = order_fills(long_cli, sym, long_order["id"])
    sf = order_fills(short_cli, sym, short_order["id"])
    matched = min(lf["gross"], sf["gross"])

    # Igualar piernas: deshacer el exceso de la pierna más llena.
    unwind_ok = True
    if lf["gross"] - matched > 0:
        unwind_ok &= _reduce(long_cli, sym, "sell", lf["gross"] - matched, ctx)
    if sf["gross"] - matched > 0:
        unwind_ok &= _reduce(short_cli, sym, "buy", sf["gross"] - matched, ctx)

    if matched <= 0:
        note = "deshecha" if unwind_ok else "NO deshecha — REVISA MANUALMENTE"
        return {"ok": False, "unwound": unwind_ok,
                "msg": f"Abortado en {CROSS_LIMIT_TIMEOUT}s: no se llenaron ambas piernas. Pierna {note}."}

    return {
        "ok": True, "dry_run": False,
        "entry_price": sf["avg"] or short_px,
        "entry_fees_usd": round(lf["fee_usd"] + sf["fee_usd"], 6),
        "perp_qty": matched, "spot_qty": None,
        "exposure": matched * (sf["avg"] or short_px),
        "order_ids": [long_order.get("id"), short_order.get("id")],
        "entry_mode": "limit_both",
        "partial_note": None if matched >= min(long_amt, short_amt) * 0.999 else
        f"Llenado parcial: {matched} de {min(long_amt, short_amt)}",
        "legs": [
            {"side": "buy", "kind": "perp", "exchange": long_ex, "symbol": sym,
             "amount": matched, "price": lf["avg"] or long_px,
             "order_id": long_order.get("id"), "fee_usd": lf["fee_usd"]},
            {"side": "sell", "kind": "perp", "exchange": short_ex, "symbol": sym,
             "amount": matched, "price": sf["avg"] or short_px,
             "order_id": short_order.get("id"), "fee_usd": sf["fee_usd"]},
        ],
    }


def _reduce(cli, sym, side, base_amt, ctx) -> bool:
    amt = _norm_amount(cli, sym, base_amt)
    if amt <= 0:
        return True
    try:
        cli.create_order(sym, "market", side, amt, None,
                         _with_coid(_perp_close_params(cli, sym), ctx.coid("u")))
        return True
    except Exception as e:
        log.error(f"UNWIND {side} {sym} failed: {e}")
        return False


# ── Public: close ───────────────────────────────────────────────────────────
def _legacy_qtys(position: dict):
    """Cantidades de posiciones auto antiguas (sin spot_qty/perp_qty) desde el
    audit `auto_open` de payments_json."""
    for p in position.get("payments") or position.get("payments_json") or []:
        if p.get("kind") == "auto_open":
            spot = perp = None
            for leg in p.get("legs") or []:
                if leg.get("kind") == "spot":
                    spot = leg.get("amount")
                elif leg.get("kind") == "perp":
                    perp = leg.get("amount")
            return spot, perp
    return None, None


def execute_close(creds_by_exchange: dict, position: dict, dry_run: bool = False,
                  ctx: ExecContext = None, twap: dict = None, spot_leg_factory=None) -> dict:
    """Cierra ambas piernas usando las cantidades PERSISTIDAS de la posición.

    Returns {ok, dry_run, exit_fees_usd, order_ids[], legs[], spot_sold, perp_closed}
    or {ok:False, msg}.
    """
    ctx = ctx or ExecContext()
    mode = position.get("mode", "spot_perp")
    try:
        if mode == "spot_perp":
            return _close_spot_perp(creds_by_exchange, position, dry_run, ctx, twap or {},
                                    spot_leg_factory)
        if mode == "cross_exchange":
            return _close_cross(creds_by_exchange, position, dry_run, ctx)
    except ExecError as e:
        return {"ok": False, "msg": str(e), "code": e.code}
    return {"ok": False, "msg": f"Modo '{mode}' no soportado en auto"}


def _close_spot_perp(creds_by_exchange, position, dry_run, ctx, twap, spot_leg_factory):
    symbol = position.get("symbol", "")
    exchange = position.get("exchange", "")
    creds = creds_by_exchange.get(exchange.lower())
    if not creds:
        return {"ok": False, "msg": f"Sin API keys para {exchange}"}
    spot_qty = position.get("spot_qty")
    perp_qty = position.get("perp_qty")
    if spot_qty is None or perp_qty is None:
        spot_qty, perp_qty = _legacy_qtys(position)
    if not spot_qty and not perp_qty:
        return {"ok": False, "msg": ("La posición no tiene cantidades registradas por pierna; "
                                     "ciérrala manualmente en el exchange o impórtala desde Cuenta.")}
    spot_qty = float(spot_qty or 0)
    perp_qty = float(perp_qty or 0)

    spot_sym = to_ccxt_symbol(symbol, "spot")
    perp_sym = to_ccxt_symbol(symbol, "swap")
    try:
        spot_cli = _build_client(exchange, creds, default_type="spot")
        perp_cli = _build_client(exchange, creds, default_type="swap")
        _ensure_markets(spot_cli)
        _ensure_markets(perp_cli)
    except Exception as e:
        return {"ok": False, "msg": f"No se pudo conectar a {exchange}: {e}"}

    onchain = spot_leg_factory is not None
    leg = spot_leg_factory(perp_cli, perp_sym, ctx) if onchain else None
    # El short se lee ANTES de tocar nada: si no se puede leer, no se opera (vender
    # el spot creyendo que no hay short dejaría el short abierto y sin rastrear).
    if _open_perp_size(perp_cli, perp_sym, "short") is None:
        raise ExecError("No se pudo leer la posición perp en el exchange; cierre abortado "
                        "sin operar. Reintenta en unos minutos.", code="POSITION_READ")
    # Seguros primero: la venta límite bloquea la base (saldo libre) y el SL no
    # debe dispararse a mitad del cierre. Lo que ya llenaron cuenta como salida.
    prot_fill = {}
    if position.get("protection") and not dry_run:
        prot_fill = _cancel_protection_clients(spot_cli, perp_cli, spot_sym, perp_sym,
                                               position["protection"],
                                               getattr(leg, "wallet", None))
    actual_short = _open_perp_size(perp_cli, perp_sym, "short")
    if actual_short is None:
        raise ExecError("No se pudo leer la posición perp tras cancelar los seguros; cierre "
                        "abortado sin operar (los seguros se re-colocan).", code="POSITION_READ")
    perp_to_close = min(perp_qty, actual_short) if actual_short else 0.0
    if onchain and not actual_short and hasattr(leg, "naked_exit"):
        # Spot desnudo (SL o liquidación ya cerraron el short): vender pesa más que el
        # precio, en tramos y con slippage corto (AIN #51).
        leg.naked_exit()
    if onchain:
        spot_held = leg.held_base()
    else:
        leg = CexSpotLeg(spot_cli, spot_sym, ctx, perp_cli, perp_sym)
        spot_held = _free_base(spot_cli, spot_sym)
    spot_to_sell = min(spot_qty, spot_held) if spot_held is not None else spot_qty

    drift = None
    if perp_qty and actual_short < perp_qty * 0.95:
        drift = f"Short real {actual_short:g} < registrado {perp_qty:g}"
    if spot_qty and spot_held is not None and spot_held < spot_qty * 0.95:
        drift = (drift + "; " if drift else "") + f"spot libre {spot_held:g} < registrado {spot_qty:g}"

    if dry_run:
        _, _, pmid = _top_of_book(perp_cli, perp_sym, position.get("entry_price", 0))
        return {"ok": True, "dry_run": True, "drift": drift,
                "exit_fees_usd": round(spot_to_sell * pmid * 2 * 0.0006, 4), "order_ids": [],
                "legs": [
                    {"side": "sell", "kind": "spot", "symbol": spot_sym, "amount": spot_to_sell,
                     "type": "swap" if onchain else "limit maker → market"},
                    {"side": "buy", "kind": "perp", "symbol": perp_sym, "amount": perp_to_close,
                     "type": "market reduceOnly"},
                ]}

    ref = _ref_mids(spot_cli, spot_sym, perp_cli, perp_sym, leg if onchain else None,
                    position.get("entry_price") or 0)
    # Short cerrado fuera de basyo (web, liquidación): su recompra real entra al PnL;
    # si no se encuentra, el PnL de precio queda indeterminado (nunca de una sola pierna).
    ext = None
    missing = perp_qty - actual_short - float(prot_fill.get("perp_filled") or 0)
    if perp_qty and missing > perp_qty * EXTERNAL_CLOSE_TOL:
        f = external_perp_buyback(perp_cli, perp_sym, missing, position.get("entry_time"))
        if f and f["gross"] >= missing * 0.99:
            ext = {"missing": missing, **f}
        else:
            ext = {"missing": missing, "found": (f or {}).get("gross"),
                   "unknown": (f"No se encontró la recompra del short cerrado fuera de basyo "
                               f"({(f or {}).get('gross') or 0:g} de {missing:g})")}
    ext_fee = float((ext or {}).get("fee_usd") or 0)
    if spot_to_sell and _is_dust(leg, spot_to_sell, ref):
        # JCT #46: tras llenarse la venta límite quedaban 1e-11 en la wallet; el swap
        # revertía ("Return amount is not enough") y el short quedaba sin recomprar.
        spot_to_sell = 0.0
    hedger = PerpHedger(perp_cli, perp_sym, ctx, opening=False, max_base=perp_to_close)
    ratio = (perp_to_close / spot_to_sell) if spot_to_sell else 0
    state = {"per_order": {}, "sold": 0.0, "proceeds": 0.0}

    def on_fill(oid, f, net):
        state["per_order"][oid] = (f["gross"], f["cost"])
        state["sold"] = sum(v[0] for v in state["per_order"].values())
        state["proceeds"] = sum(v[1] for v in state["per_order"].values())
        err = hedger.target(state["sold"] * ratio)
        ctx.progress({"phase": "close", "spot_sold": state["sold"], "perp_closed": hedger.hedged})
        if err:
            raise ExecError(f"Falló la recompra del perp: {err}", code="HEDGE_FAILED")

    slices = max(1, int(twap.get("slices") or 1))
    duration = float(twap.get("duration_s") or 0)
    interval = duration / slices if slices > 1 else 0
    window = SPOT_LIMIT_TIMEOUT if slices == 1 else max(15, min(SPOT_LIMIT_TIMEOUT, interval * 0.8))
    if spot_to_sell and not actual_short:
        window = 0       # spot desnudo (el SL ya cerró el short): taker ya, sin esperar maker
    error = None
    try:
        for i in range(slices):
            if ctx.cancelled():
                break
            t0 = time.time()
            need = spot_to_sell * (i + 1) / slices - state["sold"]
            if need > 0:
                # Cerrar tiene prioridad: taker incondicional tras la ventana maker.
                leg.trade("sell", need, on_fill, window, lambda *_: True)
            if i < slices - 1:
                rest = interval - (time.time() - t0)
                while rest > 0 and not ctx.cancelled():
                    time.sleep(min(5, rest))
                    rest -= 5
    except ExecError as e:
        error = e
    except Exception as e:
        error = ExecError(f"Error vendiendo el spot: {e}")

    # Cobertura final: el perp se cierra en proporción a lo vendido; si se vendió
    # todo (o no había spot), se recompra el perp completo. Un remanente spot bajo
    # el mínimo vendible es polvo: cuenta como vendido (si no, la posición quedaría
    # trabada y cada cierre posterior fallaría con "nada ejecutado").
    remaining = max(0.0, spot_to_sell - state["sold"])
    fully_sold = (spot_to_sell <= 0 or state["sold"] >= spot_to_sell * 0.999
                  or (not error and not ctx.cancelled() and _is_dust(leg, remaining, ref)))
    if hedger.failed is None:
        target = perp_to_close if fully_sold else state["sold"] * ratio
        hedger.target(target, force_all=fully_sold)
    if hedger.failed is not None:
        return {"ok": False, "code": "HEDGE_FAILED",
                "msg": (f"Spot vendido ({state['sold']:g}) pero el perp NO se recompró "
                        f"completamente: {hedger.failed} — REVISA MANUALMENTE"),
                "spot_sold": state["sold"], "perp_closed": hedger.hedged, "perp_external": ext,
                "price_pnl_unknown": (ext or {}).get("unknown"),
                "exit_fees_usd": round(leg.fee_usd + hedger.fee_usd + prot_fill.get("fee_usd", 0.0)
                                       + ext_fee, 6)}

    res = {
        "ok": True, "dry_run": False, "drift": drift,
        "exit_fees_usd": round(leg.fee_usd + hedger.fee_usd + prot_fill.get("fee_usd", 0.0)
                               + ext_fee, 6),
        "protection_filled": prot_fill or None,
        "perp_external": ext, "price_pnl_unknown": (ext or {}).get("unknown"),
        "spot_sold": state["sold"], "perp_closed": hedger.hedged,
        "spot_exit_price": state["proceeds"] / state["sold"] if state["sold"] else None,
        "perp_exit_price": hedger.avg or None,
        "remaining_spot": max(0.0, spot_qty - state["sold"]),
        "remaining_perp": max(0.0, perp_qty - hedger.hedged),
        "complete": fully_sold and hedger.hedged >= perp_to_close * 0.999,
        "order_ids": list(leg.order_ids) + hedger.order_ids,
        "legs": [
            {"side": "sell", "kind": "spot", "symbol": spot_sym, "amount": state["sold"],
             "order_ids": list(leg.order_ids), "fee_usd": round(leg.fee_usd, 6)},
            {"side": "buy", "kind": "perp", "symbol": perp_sym, "amount": hedger.hedged,
             "order_ids": hedger.order_ids, "fee_usd": round(hedger.fee_usd, 6)},
        ],
    }
    res.update(_close_pnl(position, state, hedger, prot_fill, ref, ext))
    if getattr(leg, "exit_log", None):
        res["exec"]["exit_swaps"] = leg.exit_log
    if error or ctx.cancelled():
        res["partial_note"] = str(error) if error else "Cancelado por el usuario"
    return res


def _is_dust(leg, amt: float, ref) -> bool:
    """True si `amt` (base) ya no se puede vender: bajo el mínimo del mercado spot
    (CEX) o < 1 USD (on-chain)."""
    if amt <= 0:
        return True
    px = (ref or {}).get("spot_mid") or (ref or {}).get("perp_mid") or 0
    if isinstance(leg, CexSpotLeg):
        try:
            px = px or _mid_price(leg.c, leg.sym)
            n = _norm_amount(leg.c, leg.sym, amt)
            return n <= 0 or not _min_ok(leg.c, leg.sym, n, px)
        except Exception:
            return False
    return bool(px) and amt * px < 1.0


def _close_pnl(position, state, hedger, prot_fill, ref, ext=None) -> dict:
    """PnL de precio REAL de lo cerrado en esta ejecución (incluye lo que llenaron
    los seguros y la recompra del short cerrado fuera de basyo): (salida − entrada)·qty
    en el spot + (entrada − salida)·qty en el perp. Ya contiene basis, slippage y
    comisión del DEX; las fees explícitas van aparte en exit_fees_usd. price_pnl=None
    si falta el precio de entrada o la recompra externa no se pudo leer."""
    spot_entry = position.get("spot_entry_price") or _legacy_spot_entry(position)
    perp_entry = float(position.get("entry_price") or 0) or None
    ext_ok = ext if ext and not ext.get("unknown") else {}
    spot_out = state["sold"] + float(prot_fill.get("spot_filled") or 0)
    spot_proc = state["proceeds"] + float(prot_fill.get("spot_cost") or 0)
    perp_out = hedger.hedged + float(prot_fill.get("perp_filled") or 0) + float(ext_ok.get("gross") or 0)
    perp_cost = hedger.cost + float(prot_fill.get("perp_cost") or 0) + float(ext_ok.get("cost") or 0)
    spot_exit = spot_proc / spot_out if spot_out else None
    perp_exit = perp_cost / perp_out if perp_out else None
    pnl = None
    if spot_entry and perp_entry and (spot_out or perp_out) and not (ext or {}).get("unknown"):
        pnl = round((spot_proc - spot_entry * spot_out) + (perp_entry * perp_out - perp_cost), 6)
    slip = _slip_summary("sell", state["proceeds"] / state["sold"] if state["sold"] else None,
                         state["sold"], hedger.avg, hedger.hedged, ref)
    return {
        "price_pnl": pnl,
        "exit_slippage": slip,
        "exec": {
            "spot_entry": spot_entry, "perp_entry": perp_entry,
            "spot_exit": spot_exit, "perp_exit": perp_exit,
            "spot_qty_out": spot_out, "perp_qty_out": perp_out,
            "exit_basis_pct": round((perp_exit - spot_exit) / spot_exit * 100, 4)
            if spot_exit and perp_exit else None,
            "exit_ref_mids": ref, "exit_slippage": slip, "perp_external": ext,
        },
    }


def _close_cross(creds_by_exchange, position, dry_run, ctx):
    symbol = position.get("symbol", "")
    exposure = position.get("exposure", 0) or 0
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

    qty = position.get("perp_qty")
    long_real = _open_perp_size(long_cli, sym, "long")
    short_real = _open_perp_size(short_cli, sym, "short")
    if long_real is None or short_real is None:
        return {"ok": False, "msg": "No se pudieron leer las posiciones perp; cierre abortado sin operar"}
    if qty:
        long_amt, short_amt = min(qty, long_real), min(qty, short_real)
    else:
        long_px = _mid_price(long_cli, sym, position.get("entry_price", 0))
        short_px = _mid_price(short_cli, sym, position.get("entry_price", 0))
        long_amt = min(long_real, exposure / long_px) if long_px else long_real
        short_amt = min(short_real, exposure / short_px) if short_px else short_real

    if dry_run:
        return {"ok": True, "dry_run": True,
                "exit_fees_usd": round(exposure * 2 * 0.0006, 4), "order_ids": [],
                "legs": [
                    {"side": "sell", "kind": "perp", "exchange": long_ex, "symbol": sym, "amount": long_amt},
                    {"side": "buy", "kind": "perp", "exchange": short_ex, "symbol": sym, "amount": short_amt},
                ]}

    legs, fees, ids = [], 0.0, []
    for cli, ex, side, amt in ((long_cli, long_ex, "sell", long_amt),
                               (short_cli, short_ex, "buy", short_amt)):
        n = _norm_amount(cli, sym, amt)
        if n <= 0:
            continue
        try:
            o = cli.create_order(sym, "market", side, n, None,
                                 _with_coid(_perp_close_params(cli, sym), ctx.coid("c")))
        except Exception as e:
            done = "Long cerrado pero " if legs else ""
            return {"ok": False, "msg": f"{done}no se pudo cerrar en {ex}: {e} — REVISA MANUALMENTE"}
        f = order_fills(cli, sym, o["id"])
        fees += f["fee_usd"]
        ids.append(o.get("id"))
        legs.append({"side": side, "kind": "perp", "exchange": ex, "order_id": o.get("id"),
                     "amount": f["gross"], "price": f["avg"]})
    return {"ok": True, "dry_run": False, "exit_fees_usd": round(fees, 6),
            "order_ids": ids, "legs": legs, "complete": True}


# ── Reconciliation (job interrumpido por reinicio) ──────────────────────────
def reconcile_spot_perp(creds: dict, exchange: str, symbol: str, prefix: str,
                        since_ms: int, kind: str, position: dict = None) -> dict:
    """Recalcula lo ejecutado por un job desde el exchange y re-cubre el perp.

    Busca órdenes con clientOrderId que empiece por `prefix` desde `since_ms`.
    Regla: el short perp debe igualar el spot que el job dejó en manos de la
    posición (open: neto comprado; close: registrado − vendido).
    Devuelve {spot_qty, perp_qty, fees_usd, adjusted, msg}.
    """
    spot_sym = to_ccxt_symbol(symbol, "spot")
    perp_sym = to_ccxt_symbol(symbol, "swap")
    spot_cli = _build_client(exchange, creds, default_type="spot")
    perp_cli = _build_client(exchange, creds, default_type="swap")
    _ensure_markets(spot_cli)
    _ensure_markets(perp_cli)

    def _orders(cli, sym):
        try:
            return [o for o in cli.fetch_orders(sym, since=since_ms, limit=500)
                    if str(o.get("clientOrderId") or "").startswith(prefix)]
        except Exception as e:
            log.warning(f"reconcile fetch_orders {sym}: {e}")
            return []

    for cli, sym in ((spot_cli, spot_sym), (perp_cli, perp_sym)):
        for o in _orders(cli, sym):
            if o.get("status") == "open":
                _safe_cancel(cli, o["id"], sym)

    spot_net = perp_net = fees = 0.0
    for o in _orders(spot_cli, spot_sym):
        f = order_fills(spot_cli, spot_sym, o["id"])
        fees += f["fee_usd"]
        if o.get("side") == "buy":
            spot_net += f["gross"] - f["fee_base"]
        else:
            spot_net -= f["gross"]
    for o in _orders(perp_cli, perp_sym):
        f = order_fills(perp_cli, perp_sym, o["id"])
        fees += f["fee_usd"]
        perp_net += f["gross"] if o.get("side") == "sell" else -f["gross"]

    # perp_net = vendido − recomprado (en un cierre las recompras lo hacen negativo).
    if kind == "open":
        spot_hold, perp_hold = max(0.0, spot_net), max(0.0, perp_net)
    else:
        spot_hold = max(0.0, float(position.get("spot_qty") or 0) + spot_net)
        perp_hold = max(0.0, float(position.get("perp_qty") or 0) + perp_net)
    actual_short = _open_perp_size(perp_cli, perp_sym, "short")
    if actual_short is None:
        raise ExecError("No se pudo leer la posición perp para reconciliar")
    # El short real manda: nunca registrar (ni re-cubrir contra) más de lo que existe.
    perp_hold = min(perp_hold, actual_short)

    adjusted = None
    delta = spot_hold - perp_hold
    amt = _norm_amount(perp_cli, perp_sym, abs(delta))
    if amt > 0 and _min_ok(perp_cli, perp_sym, amt, _mid_price(perp_cli, perp_sym)):
        try:
            if delta > 0:
                o = perp_cli.create_order(perp_sym, "market", "sell", amt, None,
                                          _with_coid(_perp_open_params(perp_cli, perp_sym), None))
                perp_hold += amt * _contract_size(perp_cli, perp_sym)
                adjusted = f"short +{amt}"
            else:
                o = perp_cli.create_order(perp_sym, "market", "buy", amt, None,
                                          _perp_close_params(perp_cli, perp_sym))
                perp_hold -= amt * _contract_size(perp_cli, perp_sym)
                adjusted = f"short −{amt}"
            fees += order_fills(perp_cli, perp_sym, o["id"])["fee_usd"]
        except Exception as e:
            log.error(f"reconcile ajuste {perp_sym} falló: {e}")
            adjusted = f"ajuste FALLIDO ({e}) — delta spot−short {delta:g}"
    return {"spot_qty": spot_hold, "perp_qty": max(0.0, perp_hold), "fees_usd": fees,
            "adjusted": adjusted,
            "msg": f"Reconciliado: spot {spot_hold:g}, short {perp_hold:g}"
                   + (f" (ajuste {adjusted})" if adjusted else "")}
