"""Order-book driven slippage estimator.

For a given (exchange, symbol, side, usd_amount), walks the top N levels of
the live order book and computes the VWAP the order would actually fill at,
expressed as a percentage away from the mid price.

All results are cached for a short TTL so the scanner's many fee estimates
don't hammer the exchange API.  Any failure (network, missing symbol, CCXT
exception, shallow book) returns None so analysis/fees.py falls back to the
coarse step function.
"""
import logging
import threading
import time

log = logging.getLogger("bot.slippage")

# Cache: {(display_name, symbol, side) -> (ts, pct)}
_cache: dict = {}
_spot_cache: dict = {}
_cache_lock = threading.Lock()
_TTL = 60      # seconds — orderbook slippage
_SPOT_TTL = 30  # seconds — spot price

# Cache of the exchange manager so we don't pass it around everywhere
_exchange_manager = None


def bind_exchange_manager(manager):
    """Wire the slippage module to the live ExchangeManager instance.

    analysis/fees.py looks up exchanges by display name ('Binance', 'Bybit'...).
    The manager stores them by lowercase key, so we also cache a display→key
    mapping derived from exchanges.manager.EXCHANGE_NAMES.
    """
    global _exchange_manager
    _exchange_manager = manager


def fetch_spot_price(exchange_display: str, symbol: str):
    """Return the latest spot price for symbol on exchange, or None on failure.

    Cached 30 s. Used to compute spot-perp basis in entry strategy metrics.
    """
    if not symbol or not exchange_display:
        return None
    base = symbol.upper().replace("USDT", "").replace("USD", "").replace("/", "")
    cache_key = (exchange_display, base)
    now = time.time()
    with _cache_lock:
        entry = _spot_cache.get(cache_key)
        if entry and (now - entry[0]) < _SPOT_TTL:
            return entry[1]
    inst = _lookup_ccxt(exchange_display)
    if inst is None:
        return None
    for sym in (f"{base}/USDT", f"{base}/USD"):
        try:
            ticker = inst.fetch_ticker(sym)
            price = float(ticker.get("last") or ticker.get("close") or 0)
            if price > 0:
                with _cache_lock:
                    _spot_cache[cache_key] = (now, price)
                return price
        except Exception:
            continue
    return None


# Display-name ('Binance') → lowercase key ('binance')
def _lookup_ccxt(display_name: str):
    if _exchange_manager is None:
        return None
    try:
        from exchanges.manager import EXCHANGE_NAMES
    except Exception:
        return None
    key = None
    for k, v in EXCHANGE_NAMES.items():
        if v == display_name:
            key = k
            break
    if not key:
        return None
    return _exchange_manager.get_exchange(key)


def _candidate_symbols(base: str):
    """Produce CCXT symbol variants to try for a given base asset."""
    if not base:
        return []
    base = base.upper().replace("USDT", "").replace("USD", "").replace("/", "")
    return [
        f"{base}/USDT:USDT",   # CCXT unified swap symbol
        f"{base}/USDT",        # spot
        f"{base}/USD:USD",
        f"{base}/USD",
    ]


def estimate_orderbook_slippage(exchange_display: str, symbol: str,
                                side: str, usd_amount: float):
    """Walk the order book and return slippage % vs mid.

    side: "buy" (walk asks) | "sell" (walk bids)
    usd_amount: how much USD notional we want to execute.

    Returns the slippage as a percentage (0.03 == 0.03%), or None on any
    failure so the caller can fall back to the step function.
    """
    if usd_amount <= 0 or not symbol or side not in ("buy", "sell"):
        return None

    cache_key = (exchange_display, symbol.upper(), side)
    now = time.time()

    with _cache_lock:
        entry = _cache.get(cache_key)
        if entry and (now - entry[0]) < _TTL:
            return entry[1]

    inst = _lookup_ccxt(exchange_display)
    if inst is None:
        return None

    # Try the most likely symbol formats until one works.
    book = None
    used_symbol = None
    for candidate in _candidate_symbols(symbol):
        try:
            book = inst.fetch_order_book(candidate, limit=50)
            used_symbol = candidate
            break
        except Exception as e:
            log.debug(f"slippage: {exchange_display} {candidate} fetch failed: {e}")
            continue

    if not book:
        return None

    bids = book.get("bids") or []
    asks = book.get("asks") or []
    if not bids or not asks:
        return None

    best_bid = float(bids[0][0])
    best_ask = float(asks[0][0])
    mid = (best_bid + best_ask) / 2.0
    if mid <= 0:
        return None

    levels = asks if side == "buy" else bids

    filled_usd = 0.0
    weighted_price_qty = 0.0  # Σ(price * qty)
    filled_qty = 0.0
    for level in levels:
        try:
            price = float(level[0])
            qty = float(level[1])
        except (TypeError, ValueError, IndexError):
            continue
        if price <= 0 or qty <= 0:
            continue
        level_usd = price * qty
        remaining = usd_amount - filled_usd
        if remaining <= 0:
            break
        take_usd = min(level_usd, remaining)
        take_qty = take_usd / price
        weighted_price_qty += price * take_qty
        filled_qty += take_qty
        filled_usd += take_usd

    if filled_usd <= 0 or filled_qty <= 0:
        return None

    # If the book was too shallow to fill the full order, penalise a bit.
    shallow = filled_usd < usd_amount * 0.999
    vwap = weighted_price_qty / filled_qty
    if side == "buy":
        pct = ((vwap / mid) - 1.0) * 100.0
    else:
        pct = ((mid / vwap) - 1.0) * 100.0
    pct = max(0.0, pct)
    if shallow:
        pct += 0.10  # flat 0.10% penalty for a shallow fill

    with _cache_lock:
        _cache[cache_key] = (now, pct)

    log.debug(
        f"slippage: {exchange_display} {used_symbol} {side} "
        f"${usd_amount:.0f} vwap={vwap:.6f} mid={mid:.6f} pct={pct:.4f}"
    )
    return pct
