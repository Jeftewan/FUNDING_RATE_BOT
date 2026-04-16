"""Pull real maker/taker fees from CCXT exchange instances.

This runs once at startup (and refreshes every 24 h on a best-effort basis)
and caches the per-exchange fees in memory.  analysis/fees.py's
get_exchange_fees_split() consults this cache before falling back to the
hardcoded Config.FEES table.

All values in the cache are percentages (to match Config.FEES).  CCXT returns
them as decimals (0.0005 = 0.05%), so we multiply by 100.
"""
import logging
import threading
import time

log = logging.getLogger("bot.fees")

# Cache: {display_name: {spot_maker, spot_taker, fut_maker, fut_taker, loaded_at}}
_fee_cache: dict = {}
_lock = threading.Lock()

# How long a loaded value is considered fresh
_TTL_SECONDS = 24 * 3600


def get_loaded_fees(exchange_display_name: str):
    """Return the cached fee dict for an exchange, or None if stale/missing."""
    with _lock:
        entry = _fee_cache.get(exchange_display_name)
        if not entry:
            return None
        if (time.time() - entry.get("loaded_at", 0)) > _TTL_SECONDS:
            return None
        # Return a shallow copy so callers can't mutate the cache
        return {k: v for k, v in entry.items() if k != "loaded_at"}


def _pct(value) -> float:
    """CCXT fees are decimals (0.001 = 0.1%). Convert to our % convention."""
    try:
        f = float(value)
    except (TypeError, ValueError):
        return None
    if f < 0:
        return None
    return f * 100.0


def _load_one(display_name: str, ccxt_instance) -> dict:
    """Best-effort: pull maker/taker from a CCXT exchange for spot + swap."""
    out = {}

    # Many exchanges expose a top-level `fees['trading']` block
    try:
        trading = getattr(ccxt_instance, "fees", {}).get("trading", {})
        maker = _pct(trading.get("maker"))
        taker = _pct(trading.get("taker"))
        if maker is not None:
            out["fut_maker"] = maker  # CCXT swap instance → these are perp fees
        if taker is not None:
            out["fut_taker"] = taker
    except Exception as e:
        log.debug(f"fee_loader: {display_name} top-level fees read failed: {e}")

    # Best-effort: walk a sample of swap markets and take the most common
    # maker/taker pair.  Works for exchanges whose trading fees are returned
    # per-market by CCXT (Binance, Bybit, OKX, Bitget all do this).
    try:
        markets = getattr(ccxt_instance, "markets", None)
        if markets:
            makers, takers = [], []
            for m in list(markets.values())[:50]:
                mk = _pct(m.get("maker"))
                tk = _pct(m.get("taker"))
                if mk is not None:
                    makers.append(mk)
                if tk is not None:
                    takers.append(tk)
            if makers:
                out.setdefault("fut_maker", sorted(makers)[len(makers) // 2])
            if takers:
                out.setdefault("fut_taker", sorted(takers)[len(takers) // 2])
    except Exception as e:
        log.debug(f"fee_loader: {display_name} market-level fees read failed: {e}")

    # Spot fees: all four CEXs use the same maker/taker for spot and perp
    # at base tier.  If we didn't learn them explicitly, mirror the perp
    # values (still better than the stale hardcoded table).
    if "fut_taker" in out:
        out.setdefault("spot_taker", out["fut_taker"])
    if "fut_maker" in out:
        out.setdefault("spot_maker", out["fut_maker"])

    return out


def load_fees_from_exchanges(exchanges: dict, name_map: dict = None):
    """Populate the cache from a {lowername: ccxt_instance} dict.

    `name_map` optionally maps the lowercase key → display name
    (e.g. {'binance': 'Binance'}).  Defaults to a title-case on the key.
    """
    if not exchanges:
        return 0

    loaded = 0
    for key, inst in exchanges.items():
        display = (name_map or {}).get(key, key.title())
        try:
            fees = _load_one(display, inst)
        except Exception as e:
            log.warning(f"fee_loader: {display}: extraction error: {e}")
            continue

        if not fees:
            continue

        # Only cache if we got at least one usable number
        if any(k in fees for k in ("spot_taker", "fut_taker")):
            fees["loaded_at"] = time.time()
            with _lock:
                _fee_cache[display] = fees
            loaded += 1
            log.info(
                f"fee_loader: {display} "
                f"spot_maker={fees.get('spot_maker')} spot_taker={fees.get('spot_taker')} "
                f"fut_maker={fees.get('fut_maker')} fut_taker={fees.get('fut_taker')}"
            )

    return loaded


def load_fees_async(exchanges: dict, name_map: dict = None):
    """Fire-and-forget wrapper — runs load_fees_from_exchanges in a thread
    so startup isn't blocked on CCXT network calls.
    """
    def _run():
        try:
            n = load_fees_from_exchanges(exchanges, name_map)
            log.info(f"fee_loader: cached real fees for {n} exchanges")
        except Exception as e:
            log.warning(f"fee_loader: async load failed: {e}")

    t = threading.Thread(target=_run, name="fee-loader", daemon=True)
    t.start()
    return t
