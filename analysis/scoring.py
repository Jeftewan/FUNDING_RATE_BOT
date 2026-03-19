"""Opportunity scoring v8.0 — prioritizes 3+ day sustainability and high volume."""
import math


def risk_score(token: dict, hist: dict) -> int:
    """
    v8.0 scoring — rebalanced for sustainability:
    - Estabilidad:      25pts — CV + rate minimo (key for 3-day hold)
    - Consistencia:     20pts — streak + % favorable
    - Liquidez:         15pts — volumen 24h (high vol = less volatile)
    - Yield Diario:     20pts — APR neto despues de fees
    - Frecuencia:       10pts — pagos por dia
    - Tendencia:        10pts — rate subiendo/estable/bajando
    """
    sc = 0
    afr = abs(token["fr"])
    ipd = token.get("ipd", 3)

    # 1. ESTABILIDAD (25pts) — most important for 3-day holds
    stddev = hist.get("stddev", 999)
    avg = abs(hist.get("avg", 0))
    rates = hist.get("_rates", [])

    if avg > 0 and rates:
        cv = stddev / avg
        favorable = [abs(r) for r in rates
                     if (token["fr"] > 0 and r > 0) or (token["fr"] < 0 and r < 0)]
        min_rate_ratio = min(favorable) / avg if favorable and avg > 0 else 0

        if cv < 0.2 and min_rate_ratio > 0.5:
            sc += 25
        elif cv < 0.3 and min_rate_ratio > 0.3:
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
    else:
        sc += 1

    # 2. CONSISTENCIA (20pts) — streak of favorable payments
    streak = hist.get("streak", 0)
    pct = hist.get("pct", 0)

    if streak >= 12:
        sc += 20
    elif streak >= 8:
        sc += 17
    elif streak >= 5 and pct > 80:
        sc += 14
    elif streak >= 3 and pct > 70:
        sc += 11
    elif pct > 60:
        sc += 7
    else:
        sc += 2

    # 3. LIQUIDEZ / VOLUMEN (15pts) — high volume = more stable rates
    vol = token["vol24h"]
    if vol >= 100e6:
        sc += 15
    elif vol >= 50e6:
        sc += 12
    elif vol >= 20e6:
        sc += 9
    elif vol >= 10e6:
        sc += 6
    elif vol >= 5e6:
        sc += 3
    else:
        sc += 1

    # 4. YIELD DIARIO (20pts)
    yield_day_pct = afr * ipd * 100
    rate_per_iv = afr * 100

    if rate_per_iv > 0.15:
        # Very high rate — might not be sustainable, cap points
        if yield_day_pct >= 0.15:
            sc += 14
        elif yield_day_pct >= 0.10:
            sc += 11
        else:
            sc += 7
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

    # 5. FRECUENCIA DE PAGO (10pts)
    if ipd >= 24:
        sc += 10
    elif ipd >= 12:
        sc += 8
    elif ipd >= 6:
        sc += 6
    elif ipd >= 3:
        sc += 4
    else:
        sc += 2

    # 6. TENDENCIA (10pts)
    if len(rates) >= 8:
        recent = rates[-4:]
        older = rates[-8:-4]
        avg_rec = sum(abs(r) for r in recent) / len(recent)
        avg_old = sum(abs(r) for r in older) / len(older)
        if avg_old > 0:
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
    # If we have ipd info, use it; otherwise assume 3
    intervals_per_day = 3

    streak_days = streak / intervals_per_day

    # If consistency > 90% and streak > 3 days, estimate longer hold
    if pct >= 90 and streak_days >= 5:
        return min(int(streak_days * 0.7), 14)  # Conservative: 70% of streak
    elif pct >= 80 and streak_days >= 3:
        return min(int(streak_days * 0.5), 10)
    elif pct >= 70 and streak_days >= 1:
        return min(int(streak_days * 0.4), 7)
    elif streak_days >= 1:
        return max(1, int(streak_days * 0.3))
    return 0


def score_cross_exchange(differential: float, diff_hist: dict,
                         volume_min: float, fee_ratio: float) -> int:
    """Score a cross-exchange opportunity.

    Mirrors the structure of risk_score (spot-perp) with 6 dimensions:
    - Estabilidad:    20pts — CV of historical differential + min_ratio
    - Consistencia:   15pts — streak of favorable days + % favorable
    - Differential:   20pts — current rate differential magnitude
    - Liquidez:       15pts — minimum volume of both sides
    - Fee efficiency: 15pts — fees / revenue ratio
    - Tendencia:      15pts — is the differential improving or worsening?

    diff_hist: dict from _analyze_differential_history with keys:
      consistency_pct, streak, cv, min_ratio, trend, diff_series
    """
    sc = 0
    consistency = diff_hist.get("consistency_pct", 0)
    streak = diff_hist.get("streak", 0)
    cv = diff_hist.get("cv", 999)
    min_ratio = diff_hist.get("min_ratio", 0)
    trend = diff_hist.get("trend", 1.0)
    diff_series = diff_hist.get("diff_series", [])

    # 1. ESTABILIDAD (20pts) — CV of differential + worst day ratio
    #    Low CV = differential is predictable across days
    #    High min_ratio = even the worst day was decent
    if diff_series:
        if cv < 0.3 and min_ratio > 0.4:
            sc += 20
        elif cv < 0.4 and min_ratio > 0.3:
            sc += 17
        elif cv < 0.5 and min_ratio > 0.2:
            sc += 14
        elif cv < 0.7:
            sc += 10
        elif cv < 1.0:
            sc += 6
        elif cv < 1.5:
            sc += 3
        else:
            sc += 1
    else:
        sc += 1  # no history = lowest confidence

    # 2. CONSISTENCIA (15pts) — streak + % favorable
    if streak >= 5 and consistency >= 90:
        sc += 15
    elif streak >= 4 and consistency >= 85:
        sc += 13
    elif streak >= 3 and consistency >= 80:
        sc += 11
    elif streak >= 2 and consistency >= 70:
        sc += 9
    elif consistency >= 60:
        sc += 6
    elif consistency >= 50:
        sc += 3
    else:
        sc += 1

    # 3. DIFFERENTIAL magnitude (20pts) — current spread
    d_pct = abs(differential) * 100
    if d_pct >= 0.10:
        sc += 20
    elif d_pct >= 0.05:
        sc += 17
    elif d_pct >= 0.03:
        sc += 14
    elif d_pct >= 0.02:
        sc += 11
    elif d_pct >= 0.01:
        sc += 7
    else:
        sc += 3

    # 4. LIQUIDEZ (15pts)
    if volume_min >= 100e6:
        sc += 15
    elif volume_min >= 50e6:
        sc += 12
    elif volume_min >= 20e6:
        sc += 9
    elif volume_min >= 10e6:
        sc += 6
    elif volume_min >= 5e6:
        sc += 3
    else:
        sc += 1

    # 5. FEE EFFICIENCY (15pts) — lower fee_ratio = better
    if fee_ratio < 0.1:
        sc += 15
    elif fee_ratio < 0.2:
        sc += 12
    elif fee_ratio < 0.3:
        sc += 9
    elif fee_ratio < 0.5:
        sc += 6
    else:
        sc += 2

    # 6. TENDENCIA (15pts) — is the differential improving?
    if len(diff_series) >= 4:
        if trend >= 1.3:
            sc += 15   # strongly improving
        elif trend >= 1.0:
            sc += 12   # stable or slightly improving
        elif trend >= 0.7:
            sc += 6    # declining but still ok
        elif trend >= 0.4:
            sc += 3    # declining significantly
        else:
            sc += 1    # collapsing
    else:
        sc += 7  # not enough data, neutral

    return min(sc, 100)


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
