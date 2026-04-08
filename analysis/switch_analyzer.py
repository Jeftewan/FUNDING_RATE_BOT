"""Switch analysis — should a position be closed for a better opportunity?

Inspired by Leung & Li (2015) optimal mean reversion trading and
No-Trade Region theory (transaction costs create zones where rebalancing
is NOT optimal). Funding rates are mean-reverting with positive bias (~0.01%).

Decision thresholds:
  SWITCH:  adjusted_switch_value > 0 AND break_even < 24h AND score_new > score + 15
  CONSIDER: adjusted_switch_value > 0 AND break_even < 48h
  HOLD:    anything else
"""
import math
import logging
from analysis.fees import (calculate_spot_perp_fees, calculate_cross_exchange_fees,
                           calculate_break_even_hours)

log = logging.getLogger("bot")


def calculate_switch_cost(current_pos: dict, new_opp: dict,
                          capital: float) -> dict:
    """Calculate total cost of switching: exit current + enter new.

    Reuses existing fee calculation functions.
    """
    mode_cur = current_pos.get("mode", "spot_perp")
    mode_new = new_opp.get("mode", "spot_perp")
    vol_cur = 1e6  # Conservative volume estimate for exit
    vol_new = new_opp.get("volume_24h", 1e6) or 1e6

    # Exit fees for current position
    if mode_cur == "spot_perp":
        exit_fees = calculate_spot_perp_fees(
            current_pos.get("exchange", ""), capital, vol_cur
        )
    else:
        exit_fees = calculate_cross_exchange_fees(
            current_pos.get("long_exchange", ""),
            current_pos.get("short_exchange", ""),
            capital, vol_cur
        )

    # Entry fees for new position
    if mode_new == "spot_perp":
        entry_fees = calculate_spot_perp_fees(
            new_opp.get("exchange", ""), capital, vol_new
        )
    else:
        entry_fees = calculate_cross_exchange_fees(
            new_opp.get("long_exchange", ""),
            new_opp.get("short_exchange", ""),
            capital, vol_new
        )

    return {
        "exit_cost": exit_fees["total_cost"],
        "entry_cost": entry_fees["total_cost"],
        "total_cost": exit_fees["total_cost"] + entry_fees["total_cost"],
    }


def calculate_projected_earnings(rate: float, ppd: float,
                                 exposure: float, hours: float) -> float:
    """Project earnings based on rate, payments per day, and time."""
    if rate <= 0 or ppd <= 0 or exposure <= 0 or hours <= 0:
        return 0
    daily_income = exposure * abs(rate) * ppd
    return daily_income * (hours / 24)


def mean_reversion_factor(current_rate: float, avg_rate: float,
                          rates_history: list) -> float:
    """Discount factor for mean reversion probability.

    Inspired by Leung & Li (2015) OU process properties:
    - If current < avg: rate may recover upward -> discount switch benefit
    - If current >= avg: no recovery expected -> no discount
    - Uses z-score magnitude to calibrate discount

    Returns factor in [0.5, 1.0] — lower = more likely to revert up (don't switch).
    """
    if not rates_history or avg_rate <= 0:
        return 0.85  # Conservative default

    abs_current = abs(current_rate)
    abs_avg = abs(avg_rate)

    if abs_current >= abs_avg:
        # Rate is at or above average — no mean reversion benefit
        return 1.0

    # Rate is below average — may revert upward
    # Calculate how far below average we are
    abs_rates = [abs(r) for r in rates_history if r != 0]
    if not abs_rates:
        return 0.85

    mean = sum(abs_rates) / len(abs_rates)
    variance = sum((r - mean) ** 2 for r in abs_rates) / len(abs_rates)
    std = math.sqrt(variance) if variance > 0 else 1e-10

    if std < 1e-12:
        return 0.85

    # Z-score: how many stds below mean
    z = (mean - abs_current) / std

    # More below average = stronger reversion pull = bigger discount
    if z > 2.0:
        return 0.5   # Very far below — strong reversion expected
    elif z > 1.5:
        return 0.6
    elif z > 1.0:
        return 0.7
    elif z > 0.5:
        return 0.8
    else:
        return 0.9


def analyze_switch(position: dict, opportunities: list,
                   all_data: list, db_persistence=None) -> dict:
    """Analyze if switching from current position to a better one is worthwhile.

    Returns:
      {
        "best_switch": {opportunity dict, switch_value, break_even_h, signal} or None,
        "alternatives": [...top 3 alternatives with metrics],
        "current_projected": float,
        "recommendation": "SWITCH" | "CONSIDER" | "HOLD",
      }
    """
    capital = position.get("capital_used", 0)
    exposure = position.get("exposure", capital / 2)
    mode = position.get("mode", "spot_perp")
    ih = position.get("ih", 8)
    ppd = 24 / ih

    # Current position rate: use avg_rate if available, else last known
    current_rate = position.get("avg_rate", 0) or position.get("last_fr_used", 0)
    if not current_rate:
        current_rate = position.get("entry_fr", 0)

    # Find current market rate for position
    sym = position["symbol"]
    ex = position.get("exchange", "")
    current_market_rate = current_rate
    for d in all_data:
        if d.get("symbol") == sym and d.get("exchange") == ex:
            current_market_rate = d.get("fr", current_rate)
            break

    # Current projected earnings (next 72h at current rate)
    hours_remaining = 72
    current_projected = calculate_projected_earnings(
        abs(current_market_rate), ppd, exposure, hours_remaining
    )

    # Get historical stats for mean reversion analysis
    hist_rates = []
    if db_persistence:
        try:
            hist = db_persistence.get_historical_stats(sym, ex)
            hist_rates = hist.get("rates", [])
        except Exception:
            pass

    current_score = 0
    for opp in opportunities:
        if (opp.get("symbol") == sym and
                opp.get("exchange", opp.get("short_exchange", "")) == ex):
            current_score = opp.get("score", 0)
            break

    # Evaluate top opportunities as switch candidates
    alternatives = []
    pos_sym_ex = f"{sym}_{ex}"

    for opp in opportunities[:15]:  # Top 15 by score
        opp_sym = opp.get("symbol", "")
        opp_ex = opp.get("exchange", opp.get("short_exchange", ""))
        opp_key = f"{opp_sym}_{opp_ex}"

        # Skip if same as current position
        if opp_key == pos_sym_ex:
            continue

        opp_score = opp.get("score", 0)
        opp_rate = abs(opp.get("funding_rate", opp.get("rate_differential", 0)))
        opp_ppd = opp.get("payments_per_day", 3)
        opp_hold_days = opp.get("estimated_hold_days", 3)

        # Calculate switching cost
        switch_cost = calculate_switch_cost(position, opp, capital)

        # Projected gains
        opp_settlement = abs(opp.get("settlement_avg", opp_rate))
        new_hours = opp_hold_days * 24
        projected_new = calculate_projected_earnings(
            opp_settlement, opp_ppd, exposure, new_hours
        )
        projected_current = calculate_projected_earnings(
            abs(current_market_rate), ppd, exposure, new_hours
        )

        # Net switch value
        net_switch_value = (projected_new - projected_current) - switch_cost["total_cost"]

        # Mean reversion discount (current position may recover)
        mr_factor = mean_reversion_factor(current_market_rate, current_rate, hist_rates)
        # New opportunity risk discount
        new_opp_risk = 0.8

        adjusted_value = net_switch_value * mr_factor * new_opp_risk

        # Break-even for switch
        hourly_current = calculate_projected_earnings(
            abs(current_market_rate), ppd, exposure, 1
        )
        hourly_new = calculate_projected_earnings(
            opp_settlement, opp_ppd, exposure, 1
        )
        hourly_diff = hourly_new - hourly_current
        if hourly_diff > 0:
            be_switch_h = switch_cost["total_cost"] / hourly_diff
        else:
            be_switch_h = 999

        alternatives.append({
            "symbol": opp_sym,
            "exchange": opp_ex,
            "mode": opp.get("mode", "spot_perp"),
            "score": opp_score,
            "apr": opp.get("apr", 0),
            "switch_cost": switch_cost["total_cost"],
            "projected_gain_new": projected_new,
            "projected_gain_current": projected_current,
            "net_switch_value": net_switch_value,
            "adjusted_switch_value": adjusted_value,
            "break_even_h": be_switch_h,
            "mr_factor": mr_factor,
            "_id": opp.get("_id", ""),
        })

    # Sort by adjusted switch value descending
    alternatives.sort(key=lambda a: a["adjusted_switch_value"], reverse=True)

    # Determine recommendation based on best alternative
    best = alternatives[0] if alternatives else None
    recommendation = "HOLD"

    if best and best["adjusted_switch_value"] > 0:
        if (best["break_even_h"] < 24 and
                best["score"] > current_score + 15):
            recommendation = "SWITCH"
        elif best["break_even_h"] < 48:
            recommendation = "CONSIDER"

    return {
        "best_switch": best,
        "alternatives": alternatives[:3],
        "current_projected": current_projected,
        "current_market_rate": current_market_rate,
        "recommendation": recommendation,
    }
