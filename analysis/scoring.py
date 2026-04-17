"""Opportunity scoring v10.5 — normalized to 100 and mode-aware.

Changes from v10.4:
  * Weights scaled to sum to 100 (was 90). Ratios preserved from backtest.
  * Liquidity tier recalibrated for cross-exchange / DeFi (min of the two
    sides rather than a single leg).
  * Hard caps scaled: 35→39, 45→50, 55→61.
  * Z-penalties scaled proportionally (×10/9 → rounded).
  * Accepts params["mode"] so per-mode corrections apply (spot_perp,
    cross_exchange, defi). Defaults to "spot_perp".
  * Thin-history neutral defaults: when we have fewer than 5 rate samples
    (new symbol / DeFi bootstrap), skip consistency hard-cap and use a
    neutral consistency baseline rather than the harsh "else 3" bucket.

Base dimensions (100 pts max):
  1. Stability       (31 pts)
  2. Consistency     (44 pts)
  3. Liquidity       ( 4 pts)
  4. Yield           (13 pts)
  5. Fee Efficiency  ( 5 pts)
  6. Trend           ( 3 pts)
"""
import math
from analysis.indicators import compute_all_indicators


def opportunity_score(params: dict) -> int:
    """Unified scoring for any arbitrage opportunity (v10.5)."""
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

    # -- 1. STABILITY (31 pts) ------------------------------------
    if thin:
        sc += 15  # neutral
    elif cv < 0.2 and min_ratio > 0.5:   sc += 31
    elif cv < 0.3 and min_ratio > 0.3:   sc += 24
    elif cv < 0.3:                        sc += 20
    elif cv < 0.5:                        sc += 13
    elif cv < 0.8:                        sc += 8
    elif cv < 1.2:                        sc += 3
    else:                                 sc += 1

    # -- 2. CONSISTENCY (44 pts) ----------------------------------
    if thin:
        sc += 20  # neutral baseline
    elif streak >= 12 and pct >= 90:     sc += 44
    elif streak >= 8 and pct >= 85:      sc += 35
    elif streak >= 5 and pct >= 80:      sc += 29
    elif streak >= 3 and pct >= 70:      sc += 22
    elif pct >= 60:                      sc += 13
    else:                                sc += 3

    # -- 3. LIQUIDITY (4 pts) -------------------------------------
    # For cross-exchange and DeFi, volume is the MIN of the two sides
    # (constrained by the weakest side). Thresholds lowered for DeFi
    # since DeFi venues are thinner than CEX by nature.
    if mode == "defi":
        if volume >= 20e6:    sc += 4
        elif volume >= 10e6:  sc += 3
        elif volume >= 3e6:   sc += 2
        elif volume >= 500e3: sc += 1
        else:                 sc += 0
    elif mode == "cross_exchange":
        if volume >= 30e6:    sc += 4
        elif volume >= 10e6:  sc += 3
        elif volume >= 3e6:   sc += 2
        elif volume >= 1e6:   sc += 1
        else:                 sc += 0
    else:  # spot_perp
        if volume >= 50e6:    sc += 4
        elif volume >= 20e6:  sc += 3
        elif volume >= 5e6:   sc += 2
        elif volume >= 1e6:   sc += 1
        else:                 sc += 0

    # -- 4. YIELD (13 pts, non-monotonic) -------------------------
    yield_day_pct = settlement_avg * ppd * 100
    reality_penalty = False
    if settlement_avg > 0 and current_rate > settlement_avg * 2.0:
        reality_penalty = True

    if reality_penalty:
        if yield_day_pct >= 0.10:     sc += 3
        elif yield_day_pct >= 0.03:   sc += 5
        elif yield_day_pct >= 0.01:   sc += 3
        else:                         sc += 1
    else:
        if 0.03 <= yield_day_pct < 0.10:     sc += 13
        elif 0.10 <= yield_day_pct < 0.15:   sc += 10
        elif 0.01 <= yield_day_pct < 0.03:   sc += 9
        elif 0.15 <= yield_day_pct < 0.25:   sc += 4
        elif yield_day_pct >= 0.25:          sc += 1
        else:                                sc += 1

    # -- 5. FEE EFFICIENCY (5 pts) --------------------------------
    if fee_drag < 0.1:     sc += 5
    elif fee_drag < 0.2:   sc += 4
    elif fee_drag < 0.3:   sc += 2
    elif fee_drag < 0.5:   sc += 1
    else:                  sc += 0

    # -- 6. TREND (3 pts) -----------------------------------------
    indicators = compute_all_indicators(current_rate, rates)
    mom_pts  = min(2, indicators["momentum"]["points"])
    pctl_pts = min(1, indicators["percentile"]["points"])
    sc += mom_pts + pctl_pts

    # -- 7. MOMENTUM PENALTIES ------------------------------------
    mom_signal = indicators["momentum"].get("signal", "flat")
    if mom_signal == "accelerating":
        sc -= 6
    elif mom_signal == "decelerating":
        sc -= 8
    elif mom_signal == "negative":
        sc -= 3

    # -- 8. Z-SCORE PENALTY ---------------------------------------
    z_val = indicators["z_score"].get("z", 0)
    if z_val > 3.0:       sc -= 28
    elif z_val > 2.5:     sc -= 20
    elif z_val > 2.0:     sc -= 14
    elif z_val > 1.5:     sc -= 10
    elif z_val > 1.0:     sc -= 6
    elif z_val > 0.8:     sc -= 2

    # -- 9. HARD CAPS ---------------------------------------------
    percentile = indicators["percentile"].get("percentile", 0)

    if z_val > 2.5:
        sc = min(sc, 39)
    if not thin and streak < 3 and percentile >= 80:
        sc = min(sc, 50)
    if reality_penalty:
        sc = min(sc, 61)

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
