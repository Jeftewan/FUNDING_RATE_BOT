"""Opportunity scoring v10.6 — pesos re-optimizados (Optuna, 600 trials).

Adoptado desde scripts/scoring_optimizer.py. Ver reports/optimizer_20260610.md.

Changes from v10.5:
  * Re-optimizado sobre 90 días de funding_rate_snapshots. Objetivo:
    durabilidad + rentabilidad neta + monotonicity. Motivo de adopción: el
    Profit Rate del top (score ≥70) sube 4% → 11% en validación (~3× más
    rentable neto), con un top más selectivo (33.5% → 28% de observaciones).
  * Pesos: Stability 31→21, Consistency 44→46, Liquidity 4→6, Yield 13→17,
    Fee 5→8, Trend 3→1.
  * Yield reality-penalty threshold 2.0× → 2.6×.
  * Momentum penalties relajadas: accelerating/negative → 0; decelerating −8→−3.
  * Z-penalties recalibradas (umbrales 3.2/2.6/2.2/1.5/0.9/0.5).
  * Hard caps: z>2.5 39→47; streak-cap (streak<3 & pctl≥80 → ≤50) pasa a
    (streak<4 & pctl≥90 → ≤36); reality 61→54.
  * Mode-awareness y thin-history defaults se conservan de v10.5. La
    optimización valida solo spot_perp; cross_exchange/defi conservan la
    estructura de liquidez de v10.5 (solo escalan con el nuevo peso).

Base dimensions (99 pts max):
  1. Stability       (21 pts)
  2. Consistency     (46 pts)
  3. Liquidity       ( 6 pts)
  4. Yield           (17 pts)
  5. Fee Efficiency  ( 8 pts)
  6. Trend           ( 1 pts)
"""
import math
from analysis.indicators import compute_all_indicators


def opportunity_score(params: dict) -> int:
    """Unified scoring for any arbitrage opportunity (v10.6)."""
    sc = 0

    cv = params.get("cv", 999)
    min_ratio = params.get("min_ratio", 0)
    streak = params.get("streak", 0)
    pct = params.get("pct", 0)
    volume = params.get("volume", 0)
    settlement_avg = abs(params.get("settlement_avg", 0))
    ppd = params.get("payments_per_day", 3)
    fee_drag = params.get("fee_drag", 1)
    current_rate = abs(params.get("current_rate", 0))
    rates = params.get("rates", [])
    mode = params.get("mode", "spot_perp")

    # Thin-history detection: <5 samples means we can't trust consistency
    # metrics; use neutral fallbacks so new/DeFi symbols aren't scored 0.
    thin = len(rates) < 5

    # -- 1. STABILITY (21 pts) ------------------------------------
    if thin:
        sc += 15  # neutral
    elif cv < 0.2 and min_ratio > 0.5:   sc += 21
    elif cv < 0.3 and min_ratio > 0.3:   sc += 16
    elif cv < 0.3:                        sc += 14
    elif cv < 0.5:                        sc += 9
    elif cv < 0.8:                        sc += 5
    elif cv < 1.2:                        sc += 2
    else:                                 sc += 1

    # -- 2. CONSISTENCY (46 pts) ----------------------------------
    if thin:
        sc += 20  # neutral baseline
    elif streak >= 12 and pct >= 90:     sc += 46
    elif streak >= 8 and pct >= 85:      sc += 37
    elif streak >= 5 and pct >= 80:      sc += 30
    elif streak >= 3 and pct >= 70:      sc += 23
    elif pct >= 60:                      sc += 14
    else:                                sc += 3

    # -- 3. LIQUIDITY (6 pts) -------------------------------------
    # For cross-exchange and DeFi, volume is the MIN of the two sides
    # (constrained by the weakest side). Thresholds lowered for DeFi
    # since DeFi venues are thinner than CEX by nature. Optimización v10.6
    # valida solo spot_perp; cross/defi conservan estructura, escalan al peso.
    if mode == "defi":
        if volume >= 20e6:    sc += 6
        elif volume >= 10e6:  sc += 4
        elif volume >= 3e6:   sc += 3
        elif volume >= 500e3: sc += 2
        else:                 sc += 0
    elif mode == "cross_exchange":
        if volume >= 30e6:    sc += 6
        elif volume >= 10e6:  sc += 4
        elif volume >= 3e6:   sc += 3
        elif volume >= 1e6:   sc += 2
        else:                 sc += 0
    else:  # spot_perp
        if volume >= 50e6:    sc += 6
        elif volume >= 20e6:  sc += 4
        elif volume >= 5e6:   sc += 3
        elif volume >= 1e6:   sc += 2
        else:                 sc += 0

    # -- 4. YIELD (17 pts, non-monotonic) -------------------------
    yield_day_pct = settlement_avg * ppd * 100
    reality_penalty = False
    if settlement_avg > 0 and current_rate > settlement_avg * 2.6:
        reality_penalty = True

    if reality_penalty:
        if yield_day_pct >= 0.10:     sc += 4
        elif yield_day_pct >= 0.03:   sc += 7
        elif yield_day_pct >= 0.01:   sc += 4
        else:                         sc += 1
    else:
        if 0.03 <= yield_day_pct < 0.10:     sc += 17
        elif 0.10 <= yield_day_pct < 0.15:   sc += 13
        elif 0.01 <= yield_day_pct < 0.03:   sc += 12
        elif 0.15 <= yield_day_pct < 0.25:   sc += 5
        elif yield_day_pct >= 0.25:          sc += 1
        else:                                sc += 1

    # -- 5. FEE EFFICIENCY (8 pts) --------------------------------
    if fee_drag < 0.1:     sc += 8
    elif fee_drag < 0.2:   sc += 6
    elif fee_drag < 0.3:   sc += 3
    elif fee_drag < 0.5:   sc += 2
    else:                  sc += 0

    # -- 6. TREND (1 pt) ------------------------------------------
    indicators = compute_all_indicators(current_rate, rates)
    mom_pts  = min(2, indicators["momentum"]["points"])
    pctl_pts = min(1, indicators["percentile"]["points"])
    sc += round((mom_pts + pctl_pts) / 3.0)

    # -- 7. MOMENTUM PENALTIES ------------------------------------
    # accelerating / negative → sin penalización (optimizado en v10.6)
    mom_signal = indicators["momentum"].get("signal", "flat")
    if mom_signal == "decelerating":
        sc -= 3

    # -- 8. Z-SCORE PENALTY ---------------------------------------
    z_val = indicators["z_score"].get("z", 0)
    if z_val > 3.2:       sc -= 20
    elif z_val > 2.6:     sc -= 18
    elif z_val > 2.2:     sc -= 17
    elif z_val > 1.5:     sc -= 14
    elif z_val > 0.9:     sc -= 9
    elif z_val > 0.5:     sc -= 5

    # -- 9. HARD CAPS ---------------------------------------------
    percentile = indicators["percentile"].get("percentile", 0)

    if z_val > 2.5:
        sc = min(sc, 47)
    if not thin and streak < 4 and percentile >= 90:
        sc = min(sc, 36)
    if reality_penalty:
        sc = min(sc, 54)

    params["_indicators"] = indicators
    return max(0, min(sc, 100))


def stability_grade(score: int) -> str:
    if score >= 85: return "A"
    elif score >= 70: return "B"
    elif score >= 55: return "C"
    return "D"


def estimated_hold_days(hist: dict) -> int:
    streak = hist.get("streak", 0)
    pct = hist.get("pct", 0)
    rates = hist.get("_rates", [])
    if not rates:
        return 0
    intervals_per_day = 3
    streak_days = streak / intervals_per_day
    if pct >= 90 and streak_days >= 5:
        return min(int(streak_days * 0.7), 14)
    elif pct >= 80 and streak_days >= 3:
        return min(int(streak_days * 0.5), 10)
    elif pct >= 70 and streak_days >= 1:
        return min(int(streak_days * 0.4), 7)
    elif streak_days >= 1:
        return max(1, int(streak_days * 0.3))
    return 0


def calculate_rsi(closes: list, period: int = 14) -> float:
    if len(closes) < period + 1:
        return -1
    changes = [closes[i + 1] - closes[i] for i in range(len(closes) - 1)]
    gains = [max(c, 0) for c in changes]
    losses = [abs(min(c, 0)) for c in changes]
    avg_gain = sum(gains[:period]) / period
    avg_loss = sum(losses[:period]) / period
    for i in range(period, len(gains)):
        avg_gain = (avg_gain * (period - 1) + gains[i]) / period
        avg_loss = (avg_loss * (period - 1) + losses[i]) / period
    if avg_loss == 0:
        return 100
    rs = avg_gain / avg_loss
    return 100 - (100 / (1 + rs))
