"""Opportunity scoring v9.0 — unified formula for spot-perp and cross-exchange.

Single opportunity_score() replaces risk_score() and score_cross_exchange().
Yield dimension uses avg(settlement_rates) instead of predicted/current rate.
"""
import math


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
      current_rate: float — current predicted rate (for reality check)
      # Trend
      rates:       list  — chronological rate/diff series for trend calc
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

    # ── 4. YIELD DIARIO — settlement-based (20 pts) ─────────────
    # Use historical settlement average, NOT current predicted rate
    yield_day_pct = settlement_avg * ppd * 100

    # Reality check: if current rate > 2x historical avg, penalize
    reality_penalty = False
    if settlement_avg > 0 and current_rate > settlement_avg * 2:
        reality_penalty = True

    if reality_penalty:
        # Cap yield points — current rate looks anomalous
        if yield_day_pct >= 0.15:
            sc += 12
        elif yield_day_pct >= 0.10:
            sc += 9
        elif yield_day_pct >= 0.06:
            sc += 6
        else:
            sc += 3
    else:
        if yield_day_pct >= 0.15:
            sc += 20
        elif yield_day_pct >= 0.10:
            sc += 17
        elif yield_day_pct >= 0.06:
            sc += 14
        elif yield_day_pct >= 0.03:
            sc += 10
        elif yield_day_pct >= 0.01:
            sc += 6
        else:
            sc += 2

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

    # ── 6. TENDENCIA (10 pts) ────────────────────────────────────
    if len(rates) >= 8:
        mid = len(rates) // 2
        older = rates[:mid]
        recent = rates[mid:]
        avg_old = sum(abs(r) for r in older) / len(older)
        avg_rec = sum(abs(r) for r in recent) / len(recent)
        if avg_old > 1e-10:
            trend = avg_rec / avg_old
            if trend >= 1.3:
                sc += 10
            elif trend >= 1.0:
                sc += 8
            elif trend >= 0.7:
                sc += 4
            else:
                sc += 1
        else:
            sc += 5
    else:
        sc += 5

    return min(sc, 100)


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

    # Each rate is one interval. Convert streak to days.
    # Assume ~3 payments/day on average (8h intervals)
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
