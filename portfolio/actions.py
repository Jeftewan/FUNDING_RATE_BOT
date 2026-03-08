"""Position calculation helpers — v8.0 (no auto-generated actions)."""
from analysis.fees import (calculate_spot_perp_fees, calculate_cross_exchange_fees,
                           calculate_break_even_hours)


def calculate_position_estimate(opportunity: dict, capital: float) -> dict:
    """Calculate estimated returns for a given opportunity and capital amount.

    Returns detailed breakdown for the frontend calculator.
    """
    mode = opportunity.get("mode", "spot_perp")
    fr = abs(opportunity.get("funding_rate", opportunity.get("rate_differential", 0)))
    ipd = opportunity.get("payments_per_day", 3)
    vol = opportunity.get("volume_24h", 1e6)

    if mode == "spot_perp":
        fees = calculate_spot_perp_fees(opportunity["exchange"], capital, vol)
        fut_size = capital / 2
    else:
        fees = calculate_cross_exchange_fees(
            opportunity.get("long_exchange", ""),
            opportunity.get("short_exchange", ""),
            capital, vol,
        )
        fut_size = capital / 2

    daily_income = fut_size * fr * ipd
    income_3day = daily_income * 3
    apr = (daily_income * 365 / capital * 100) if capital > 0 else 0
    hourly = daily_income / 24
    break_even_h = calculate_break_even_hours(fees["total_cost"], hourly)

    return {
        "capital": capital,
        "mode": mode,
        "funding_rate": fr,
        "daily_income": daily_income,
        "income_3day": income_3day,
        "income_7day": daily_income * 7,
        "apr": apr,
        "fees_total": fees["total_cost"],
        "fees_detail": {
            "trading_fees": fees["total_fees"],
            "slippage": fees["slip_cost"],
            "slip_pct": fees["slip_pct"],
        },
        "break_even_hours": break_even_h,
        "net_3day": income_3day - fees["total_cost"],
        "net_daily": daily_income - (fees["total_cost"] / 3),
    }
