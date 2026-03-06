"""Fee estimation, slippage, and break-even calculation."""
from config import Config


def get_exchange_fees(exchange: str) -> dict:
    """Get fee structure for an exchange."""
    return Config.FEES.get(exchange, {"spot": 0.10, "fut": 0.05})


def estimate_slippage(volume_24h: float, position_size: float) -> float:
    """Estimate slippage percentage based on position size relative to volume."""
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


def calculate_spot_perp_fees(exchange: str, capital: float,
                             volume_24h: float) -> dict:
    """Calculate total fees for spot-perp hedge.

    Split: 50% spot buy + 50% futures short.
    Fees: spot buy fee + futures open + spot sell fee + futures close.
    """
    fi = get_exchange_fees(exchange)
    spot = capital / 2
    fut = capital / 2

    fee_in = spot * (fi["spot"] / 100) + fut * (fi["fut"] / 100)
    total_fees = fee_in * 2  # entry + exit

    slip = estimate_slippage(volume_24h, capital)
    slip_cost = capital * (slip / 100) * 2  # entry + exit

    return {
        "total_fees": total_fees,
        "slip_cost": slip_cost,
        "slip_pct": slip,
        "total_cost": total_fees + slip_cost,
        "spot_size": spot,
        "fut_size": fut,
    }


def calculate_cross_exchange_fees(long_exchange: str, short_exchange: str,
                                  capital: float, volume_24h: float) -> dict:
    """Calculate total fees for cross-exchange arbitrage.

    Each side: futures open + futures close.
    Capital split: 50% long side + 50% short side.
    """
    fi_long = get_exchange_fees(long_exchange)
    fi_short = get_exchange_fees(short_exchange)
    per_side = capital / 2

    fee_long = per_side * (fi_long["fut"] / 100) * 2  # open + close
    fee_short = per_side * (fi_short["fut"] / 100) * 2
    total_fees = fee_long + fee_short

    slip = estimate_slippage(volume_24h, per_side)
    slip_cost = capital * (slip / 100) * 2

    return {
        "total_fees": total_fees,
        "slip_cost": slip_cost,
        "slip_pct": slip,
        "total_cost": total_fees + slip_cost,
        "long_size": per_side,
        "short_size": per_side,
    }


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

    fi = get_exchange_fees(token["exchange"])
    fee_in = spot * (fi["spot"] / 100) + fut * (fi["fut"] / 100)
    total_fees = fee_in * 2

    slip = estimate_slippage(token["vol24h"], spot + fut)
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
