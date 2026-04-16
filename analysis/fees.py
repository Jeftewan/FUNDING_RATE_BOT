"""Fee estimation, slippage, and break-even calculation.

This module exposes the two public helpers used by the rest of the codebase:

  * calculate_spot_perp_fees(exchange, capital, volume_24h, symbol=None)
  * calculate_cross_exchange_fees(long_ex, short_ex, capital, volume_24h, symbol=None)

Both return a fully-broken-down dict:

    {
        "entry_fees": float,   # trading fees to OPEN the hedge (no slippage)
        "exit_fees":  float,   # trading fees to CLOSE the hedge (no slippage)
        "entry_slip": float,   # slippage cost on entry
        "exit_slip":  float,   # slippage cost on exit
        "total_fees": float,   # entry_fees + exit_fees
        "slip_cost":  float,   # entry_slip + exit_slip
        "total_cost": float,   # total_fees + slip_cost  (round-trip, kept for back-compat)
        "slip_pct":   float,   # per-side slippage percentage
        "source":     str,     # "orderbook" | "table"
        ... sizing keys (spot_size, fut_size, long_size, short_size)
    }

The old `total_cost` / `total_fees` / `slip_cost` / `slip_pct` keys are preserved
for existing callers that only look at the round-trip number.  New code should
prefer the explicit entry_/exit_ split because it lets us (a) fix the long-
standing double-count bug around `entry_fees * 2` and (b) store real per-side
fees once a position is open.
"""
from config import Config


# ── Base fee table ──────────────────────────────────────────────────────

def get_exchange_fees(exchange: str) -> dict:
    """Legacy getter — returns {spot, fut} (taker values).

    Kept for backwards compatibility with any caller that still reads the
    single-number shape.  Prefer get_exchange_fees_split() below.
    """
    fi = Config.FEES.get(exchange, {"spot": 0.10, "fut": 0.05})
    return {"spot": fi.get("spot", fi.get("spot_taker", 0.10)),
            "fut": fi.get("fut", fi.get("fut_taker", 0.05))}


def get_exchange_fees_split(exchange: str) -> dict:
    """Full maker/taker view — falls back to table if loader hasn't run."""
    # fee_loader may have cached a per-exchange override; import lazily to
    # avoid circular imports and to make this module usable without CCXT.
    try:
        from analysis.fee_loader import get_loaded_fees
        loaded = get_loaded_fees(exchange)
        if loaded:
            return loaded
    except Exception:
        pass

    fi = Config.FEES.get(exchange, {})
    return {
        "spot_maker": fi.get("spot_maker", fi.get("spot", 0.10)),
        "spot_taker": fi.get("spot_taker", fi.get("spot", 0.10)),
        "fut_maker": fi.get("fut_maker", fi.get("fut", 0.05)),
        "fut_taker": fi.get("fut_taker", fi.get("fut", 0.05)),
    }


# ── Slippage ────────────────────────────────────────────────────────────

def estimate_slippage(volume_24h: float, position_size: float) -> float:
    """Coarse slippage estimator (step function) — used as fallback when the
    order book is unavailable or fetches fail.
    """
    if volume_24h <= 0:
        return 0.5
    ratio = position_size / volume_24h
    if ratio < 0.00001:
        return 0.01
    if ratio < 0.0001:
        return 0.03
    if ratio < 0.001:
        return 0.05
    if ratio < 0.01:
        return 0.10
    return 0.20


def _orderbook_slippage(exchange: str, symbol: str, side: str,
                        usd_amount: float):
    """Try to get real slippage from the live order book.  Returns None on
    any failure so the caller can fall back to the step function.
    """
    if not symbol or usd_amount <= 0:
        return None
    try:
        from analysis.slippage import estimate_orderbook_slippage
        return estimate_orderbook_slippage(exchange, symbol, side, usd_amount)
    except Exception:
        return None


def _resolve_slippage_pct(exchange: str, symbol, side: str,
                          per_side_usd: float, volume_24h: float) -> tuple:
    """Return (slip_pct, source) where source is 'orderbook' or 'table'."""
    if symbol:
        real = _orderbook_slippage(exchange, symbol, side, per_side_usd)
        if real is not None and real >= 0:
            return real, "orderbook"
    return estimate_slippage(volume_24h, per_side_usd), "table"


# ── Spot-Perp ───────────────────────────────────────────────────────────

def calculate_spot_perp_fees(exchange: str, capital: float,
                             volume_24h: float, symbol: str = None) -> dict:
    """Calculate fees for spot-perp hedge.

    Split: 50% spot buy + 50% futures short (delta-neutral without leverage).
    Entry legs: spot BUY + futures SELL (taker).
    Exit  legs: spot SELL + futures BUY  (taker).
    """
    fi = get_exchange_fees_split(exchange)
    spot = capital / 2
    fut = capital / 2

    # Entry trading fees
    entry_spot_fee = spot * (fi["spot_taker"] / 100)
    entry_fut_fee = fut * (fi["fut_taker"] / 100)
    entry_fees = entry_spot_fee + entry_fut_fee

    # Exit trading fees (symmetric — same notional, same taker rate)
    exit_fees = entry_fees
    total_fees = entry_fees + exit_fees

    # Slippage: same symbol/exchange both sides, buy on entry / sell on exit
    slip_pct, slip_source = _resolve_slippage_pct(
        exchange, symbol, "buy", spot, volume_24h
    )
    entry_slip = (spot + fut) * (slip_pct / 100)
    exit_slip = (spot + fut) * (slip_pct / 100)
    slip_cost = entry_slip + exit_slip

    return {
        "entry_fees": entry_fees,
        "exit_fees": exit_fees,
        "entry_slip": entry_slip,
        "exit_slip": exit_slip,
        "total_fees": total_fees,
        "slip_cost": slip_cost,
        "slip_pct": slip_pct,
        "total_cost": total_fees + slip_cost,
        "spot_size": spot,
        "fut_size": fut,
        "source": slip_source,
    }


# ── Cross-exchange ──────────────────────────────────────────────────────

def calculate_cross_exchange_fees(long_exchange: str, short_exchange: str,
                                  capital: float, volume_24h: float,
                                  symbol: str = None) -> dict:
    """Calculate fees for cross-exchange arbitrage (futures vs futures).

    Each side: open + close (taker).  Capital is split 50/50.
    """
    fi_long = get_exchange_fees_split(long_exchange)
    fi_short = get_exchange_fees_split(short_exchange)
    per_side = capital / 2

    # Entry: open long on A + open short on B
    entry_long_fee = per_side * (fi_long["fut_taker"] / 100)
    entry_short_fee = per_side * (fi_short["fut_taker"] / 100)
    entry_fees = entry_long_fee + entry_short_fee

    # Exit: close each side (same notional, same taker)
    exit_fees = entry_fees
    total_fees = entry_fees + exit_fees

    # Slippage: evaluate each side independently, then sum for entry and exit
    long_slip_pct, long_src = _resolve_slippage_pct(
        long_exchange, symbol, "buy", per_side, volume_24h
    )
    short_slip_pct, short_src = _resolve_slippage_pct(
        short_exchange, symbol, "sell", per_side, volume_24h
    )
    avg_slip_pct = (long_slip_pct + short_slip_pct) / 2

    entry_slip = per_side * (long_slip_pct / 100) + per_side * (short_slip_pct / 100)
    exit_slip = entry_slip
    slip_cost = entry_slip + exit_slip

    source = "orderbook" if (long_src == "orderbook" or short_src == "orderbook") else "table"

    return {
        "entry_fees": entry_fees,
        "exit_fees": exit_fees,
        "entry_slip": entry_slip,
        "exit_slip": exit_slip,
        "total_fees": total_fees,
        "slip_cost": slip_cost,
        "slip_pct": avg_slip_pct,
        "total_cost": total_fees + slip_cost,
        "long_size": per_side,
        "short_size": per_side,
        "source": source,
    }


# ── Break-even + worthiness ─────────────────────────────────────────────

def calculate_break_even_hours(total_cost: float,
                               hourly_income: float) -> float:
    """Hours to break even after fees."""
    if hourly_income <= 0:
        return 999
    return total_cost / hourly_income


def calculate_returns(token: dict, capital: float) -> dict:
    """Calculate returns for a token (backward compatible with v5/v6).

    token: dict with keys fr, price, symbol, exchange, vol24h, ih, ipd
    """
    is_pos = token["fr"] > 0
    ih = token.get("ih", 8)
    ipd = token.get("ipd", 24 / ih)

    spot = capital / 2 if is_pos else 0
    fut = capital / 2 if is_pos else capital

    fi = get_exchange_fees_split(token["exchange"])
    entry_fees_val = spot * (fi["spot_taker"] / 100) + fut * (fi["fut_taker"] / 100)
    total_fees = entry_fees_val * 2  # entry + exit

    slip = estimate_slippage(token.get("vol24h", 0), spot + fut)
    slip_cost = (spot + fut) * (slip / 100) * 2
    total_cost = total_fees + slip_cost

    afr = abs(token["fr"])
    fpi = fut * afr
    fd = fpi * ipd
    fa = fd * 365
    apr = (fa / capital) * 100 if capital > 0 else 0
    be = total_cost / fd if fd > 0 else 999

    carry = "Positive" if is_pos else "Reverse"
    mdp = (fd / fut * 100) if (not is_pos and fut > 0) else 0
    sl_pct = (fd / capital * 100) if capital > 0 else 0

    return {
        "spot": spot, "fut": fut,
        "total_fees": total_fees, "slip_cost": slip_cost,
        "total_cost": total_cost, "slip_pct": slip,
        "fpi": fpi, "fd": fd, "apr": apr, "be": be,
        "carry": carry, "ih": ih, "ipd": ipd,
        "mdp": mdp, "sl_pct": sl_pct,
        "worthwhile": be < 5 and apr > 5,
    }
