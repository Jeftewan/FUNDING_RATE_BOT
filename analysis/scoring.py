"""Opportunity scoring v10.0 — enhanced with predictive indicators.

Unified formula for spot-perp and cross-exchange opportunities.
v10 adds: momentum, z-score mean-reversion, rate percentile,
volatility regime detection, and acceleration bonus.

Dimensions (max 100 pts):
  1. Stability      (25 pts) — CV + min_ratio
  2. Consistency     (20 pts) — streak + favorable %
  3. Liquidity       (15 pts) — 24h volume
  4. Yield           (15 pts) — settlement-based (reduced from 20)
  5. Fee Efficiency  (10 pts) — fee_drag ratio
  6. Momentum         (8 pts) — ROC + EMA ratio + acceleration
  7. Rate Percentile  (5 pts) — current vs historical range
  8. Volatility Regime(5 pts) — recent vs overall stddev
  9. Acceleration     (2 pts) — linear slope bonus
 10. Mean Reversion  (-5 pts) — z-score penalty for unsustainable rates
"""
import math
from analysis.indicators import compute_all_indicators


def opportunity_score(params: dict) -> int:
    """Unified scoring for any arbitrage opportunity.

    params keys:
      # Stability
      cv:          float — coefficient of variation of historical rates/diffs
      min_ratio:   float — worst favorable period / avg (0-1+)
      # Consistency
      streak:      int   — consecutive favorable periods
      pct:         float — % of periods that were favorable (0-100)
      # Liquidity
      volume:      float — relevant volume (24h USD)
      # Yield (settlement-based)
      settlement_avg: float — avg absolute settlement rate per interval
      payments_per_day: float — payment frequency
      fee_drag:    float — fees / gross_revenue ratio (0-1)
      current_rate: float — current predicted rate (for reality check + indicators)
      # Trend / Indicators
      rates:       list  — chronological rate/diff series
    """
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

    # ── 1. ESTABILIDAD (25 pts) ──────────────────────────────────
    if cv < 0.2 and min_ratio > 0.5:
        sc += 25
    elif cv < 0.3 and min_ratio > 0.3:
        sc += 21
    elif cv < 0.3:
        sc += 17
    elif cv < 0.5:
        sc += 12
    elif cv < 0.8:
        sc += 7
    elif cv < 1.2:
        sc += 3
    else:
        sc += 1

    # ── 2. CONSISTENCIA (20 pts) ─────────────────────────────────
    if streak >= 12 and pct >= 90:
        sc += 20
    elif streak >= 8 and pct >= 85:
        sc += 17
    elif streak >= 5 and pct >= 80:
        sc += 14
    elif streak >= 3 and pct >= 70:
        sc += 11
    elif pct >= 60:
        sc += 7
    else:
        sc += 2

    # ── 3. LIQUIDEZ (15 pts) ─────────────────────────────────────
    if volume >= 100e6:
        sc += 15
    elif volume >= 50e6:
        sc += 12
    elif volume >= 20e6:
        sc += 9
    elif volume >= 10e6:
        sc += 6
    elif volume >= 5e6:
        sc += 3
    else:
        sc += 1

    # ── 4. YIELD DIARIO — settlement-based (15 pts, reduced from 20) ──
    yield_day_pct = settlement_avg * ppd * 100

    reality_penalty = False
    if settlement_avg > 0 and current_rate > settlement_avg * 2:
        reality_penalty = True

    if reality_penalty:
        if yield_day_pct >= 0.15:
            sc += 10
        elif yield_day_pct >= 0.10:
            sc += 7
        elif yield_day_pct >= 0.06:
            sc += 5
        else:
            sc += 2
    else:
        if yield_day_pct >= 0.15:
            sc += 15
        elif yield_day_pct >= 0.10:
            sc += 13
        elif yield_day_pct >= 0.06:
            sc += 10
        elif yield_day_pct >= 0.03:
            sc += 7
        elif yield_day_pct >= 0.01:
            sc += 4
        else:
            sc += 1

    # ── 5. FEE EFFICIENCY (10 pts) ───────────────────────────────
    if fee_drag < 0.1:
        sc += 10
    elif fee_drag < 0.2:
        sc += 8
    elif fee_drag < 0.3:
        sc += 6
    elif fee_drag < 0.5:
        sc += 4
    else:
        sc += 1

    # ── 6-10. ADVANCED INDICATORS (20 pts max, -5 penalty) ──────
    # Replaces the old simple trend dimension (was 10 pts)
    indicators = compute_all_indicators(current_rate, rates)

    sc += indicators["momentum"]["points"]        # 0-8 pts
    sc += indicators["percentile"]["points"]       # 1-5 pts
    sc += indicators["regime"]["points"]           # 1-5 pts
    sc += indicators["acceleration"]["bonus"]      # 0-2 pts
    sc += indicators["z_score"]["penalty"]         # 0 to -5 pts

    # Store indicators in params for caller to access
    params["_indicators"] = indicators

    return max(0, min(sc, 100))


def stability_grade(score: int) -> str:
    """Return letter grade based on score."""
    if score >= 85:
        return "A"
    elif score >= 70:
        return "B"
    elif score >= 55:
        return "C"
    return "D"


def estimated_hold_days(hist: dict) -> int:
    """Estimate how many days the rate should stay favorable.

    Based on streak length and consistency percentage.
    """
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
    """RSI-14 calculation from closing prices."""
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
