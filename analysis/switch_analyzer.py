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


def _compute_position_health(position: dict, current_market_rate: float,
                              current_rate: float) -> dict:
    """Compute position health metrics for decision-making.

    Returns a dict with health_score (0-100), fee_recovery_pct, trend, and reasons.
    """
    entry_fr = position.get("entry_fr", 0)
    earned = position.get("earned_real", 0)
    entry_fees = position.get("entry_fees", 0)
    est_fees = entry_fees * 2
    elapsed_h = position.get("elapsed_h", 0) or 0
    payments = position.get("payments") or []
    fr_reversed = ((entry_fr > 0 and current_market_rate < 0)
                   or (entry_fr < 0 and current_market_rate > 0))

    # Fee recovery percentage
    fee_recovery_pct = min(100, (earned / est_fees * 100) if est_fees > 0 else 100)

    # Trend from last 5 payments
    recent_rates = [p["rate"] for p in payments[-5:]] if payments else []
    trend = "unknown"
    trend_strength = 0
    if len(recent_rates) >= 2:
        diffs = [recent_rates[i] - recent_rates[i - 1]
                 for i in range(1, len(recent_rates))]
        avg_diff = sum(diffs) / len(diffs)
        if avg_diff > 0.000005:
            trend = "up"
            trend_strength = min(100, abs(avg_diff) / abs(entry_fr) * 100) if entry_fr else 50
        elif avg_diff < -0.000005:
            trend = "down"
            trend_strength = min(100, abs(avg_diff) / abs(entry_fr) * 100) if entry_fr else 50
        else:
            trend = "stable"
            trend_strength = 0

    # Rate retention: cfr vs entry_fr
    if entry_fr and entry_fr != 0:
        rate_retention = abs(current_market_rate) / abs(entry_fr) * 100
    else:
        rate_retention = 100

    # Health score components (0-100)
    reasons_positive = []
    reasons_negative = []
    score = 50  # Base

    # Fee recovery component (+/- 20)
    if fee_recovery_pct >= 100:
        score += 20
        reasons_positive.append("Fees recuperados")
    elif fee_recovery_pct >= 50:
        score += 10
    elif elapsed_h > 48 and fee_recovery_pct < 30:
        score -= 15
        reasons_negative.append(f"Solo {fee_recovery_pct:.0f}% fees tras {elapsed_h:.0f}h")

    # FR reversal (-30)
    if fr_reversed:
        score -= 30
        reasons_negative.append("FR cambio de signo")

    # Rate retention (+/- 20)
    if rate_retention >= 80:
        score += 15
        if rate_retention >= 100:
            reasons_positive.append("FR igual o mejor que entrada")
    elif rate_retention < 50:
        score -= 20
        reasons_negative.append(f"FR cayo a {rate_retention:.0f}% del original")
    elif rate_retention < 70:
        score -= 10

    # Trend component (+/- 15)
    if trend == "up":
        score += 10
        reasons_positive.append("Tendencia ascendente")
    elif trend == "down":
        score -= 15
        reasons_negative.append("Tendencia descendente")
    elif trend == "stable":
        score += 5

    # Time factor (very old positions get penalty)
    if elapsed_h > 288:
        score -= 10
        reasons_negative.append(f"Posicion antigua ({elapsed_h:.0f}h)")
    elif elapsed_h > 144:
        score -= 5

    score = max(0, min(100, score))

    return {
        "health_score": round(score),
        "fee_recovery_pct": round(fee_recovery_pct, 1),
        "trend": trend,
        "trend_strength": round(trend_strength, 1),
        "rate_retention": round(rate_retention, 1),
        "fr_reversed": fr_reversed,
        "reasons_positive": reasons_positive,
        "reasons_negative": reasons_negative,
    }


def analyze_switch(position: dict, opportunities: list,
                   all_data: list, db_persistence=None) -> dict:
    """Analyze if switching from current position to a better one is worthwhile.

    Returns:
      {
        "best_switch": {opportunity dict, switch_value, break_even_h, signal} or None,
        "alternatives": [...top 3 alternatives with metrics],
        "current_projected": float,
        "recommendation": "SWITCH" | "CONSIDER" | "HOLD",
        "position_health": {health_score, fee_recovery_pct, trend, reasons...},
        "decision_summary": str,
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

    # Compute position health
    position_health = _compute_position_health(
        position, current_market_rate, current_rate
    )

    # Current APR
    daily_current = exposure * abs(current_market_rate) * ppd
    current_apr = (daily_current * 365 / capital * 100) if capital > 0 else 0

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

        # APR of alternative
        daily_new = exposure * opp_settlement * opp_ppd
        alt_apr = (daily_new * 365 / capital * 100) if capital > 0 else 0

        # Improvement ratio (how much better is the new vs current)
        improvement_pct = 0
        if projected_current > 0:
            improvement_pct = ((projected_new - projected_current) / projected_current) * 100

        alternatives.append({
            "symbol": opp_sym,
            "exchange": opp_ex,
            "mode": opp.get("mode", "spot_perp"),
            "score": opp_score,
            "apr": opp.get("apr", 0) or round(alt_apr, 1),
            "switch_cost": switch_cost["total_cost"],
            "projected_gain_new": projected_new,
            "projected_gain_current": projected_current,
            "net_switch_value": net_switch_value,
            "adjusted_switch_value": adjusted_value,
            "break_even_h": be_switch_h,
            "mr_factor": mr_factor,
            "improvement_pct": round(improvement_pct, 1),
            "stability_grade": opp.get("stability_grade", "?"),
            "consistency": opp.get("history", {}).get("pct",
                           opp.get("history", {}).get("favorable_pct", 0)),
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

    # Factor in position health: if health is very low, lower threshold for switching
    if position_health["health_score"] < 30 and best:
        if best["adjusted_switch_value"] > 0 and best["break_even_h"] < 48:
            recommendation = "SWITCH"
        elif best["adjusted_switch_value"] > 0:
            recommendation = "CONSIDER"

    # Decision summary for the user
    decision_summary = _build_decision_summary(
        recommendation, position_health, best, current_apr, current_score
    )

    return {
        "best_switch": best,
        "alternatives": alternatives[:3],
        "current_projected": current_projected,
        "current_market_rate": current_market_rate,
        "current_apr": round(current_apr, 1),
        "current_score": current_score,
        "recommendation": recommendation,
        "position_health": position_health,
        "decision_summary": decision_summary,
    }


def _build_decision_summary(recommendation: str, health: dict,
                             best: dict, current_apr: float,
                             current_score: int) -> str:
    """Build a concise decision summary string for the UI."""
    h_score = health["health_score"]
    trend = health["trend"]
    fee_pct = health["fee_recovery_pct"]

    if recommendation == "SWITCH":
        alt = best["symbol"] if best else "?"
        be = best["break_even_h"] if best else 0
        imp = best.get("improvement_pct", 0)
        return (f"Cambiar a {alt} — mejora de {imp:.0f}%, "
                f"recuperas fees de switch en {be:.0f}h. "
                f"Posicion actual debilitada (salud {h_score}/100).")

    if recommendation == "CONSIDER":
        alt = best["symbol"] if best else "?"
        return (f"Alternativa disponible: {alt}. "
                f"Evaluar cambio — posicion actual con salud {h_score}/100, "
                f"tendencia {trend}, fees {fee_pct:.0f}% recuperados.")

    # HOLD
    reasons = health["reasons_positive"]
    summary = f"Mantener posicion — salud {h_score}/100"
    if trend != "unknown":
        summary += f", tendencia {trend}"
    if fee_pct < 100:
        summary += f", {fee_pct:.0f}% fees recuperados"
    elif reasons:
        summary += f". {reasons[0]}"
    summary += "."
    return summary
