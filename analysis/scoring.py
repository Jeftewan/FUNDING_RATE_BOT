"""Risk/opportunity scoring — migrated from v6.2 + extended for v7."""
import math


def risk_score(token: dict, hist: dict, is_aggressive: bool = False) -> int:
    """
    v7.0 scoring — preserves v6.2 logic:
    - Frecuencia:       25pts
    - Yield Diario:     20pts
    - Estabilidad:      20pts — CV + rate minimo
    - Consistencia:     15pts — streak + % favorable
    - Liquidez:         10pts — volumen 24h
    - Tendencia:        10pts — rate subiendo/estable/bajando
    """
    sc = 0
    afr = abs(token["fr"])
    ipd = token.get("ipd", 3)

    # 1. FRECUENCIA DE PAGO (25pts)
    if ipd >= 24:
        sc += 25
    elif ipd >= 12:
        sc += 20
    elif ipd >= 6:
        sc += 14
    elif ipd >= 3:
        sc += 7
    else:
        sc += 2

    # 2. YIELD DIARIO (20pts)
    yield_day_pct = afr * ipd * 100
    rate_per_iv = afr * 100

    if rate_per_iv > 0.15:
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

    # 3. ESTABILIDAD (20pts)
    stddev = hist.get("stddev", 999)
    avg = abs(hist.get("avg", 0))
    rates = hist.get("_rates", [])

    if avg > 0 and rates:
        cv = stddev / avg
        favorable = [abs(r) for r in rates
                     if (token["fr"] > 0 and r > 0) or (token["fr"] < 0 and r < 0)]
        min_rate_ratio = min(favorable) / avg if favorable and avg > 0 else 0

        if cv < 0.2 and min_rate_ratio > 0.5:
            sc += 20
        elif cv < 0.3 and min_rate_ratio > 0.3:
            sc += 17
        elif cv < 0.3:
            sc += 14
        elif cv < 0.5:
            sc += 10
        elif cv < 0.8:
            sc += 6
        elif cv < 1.2:
            sc += 3
        else:
            sc += 1
    else:
        sc += 1

    # 4. CONSISTENCIA (15pts)
    streak = hist.get("streak", 0)
    pct = hist.get("pct", 0)

    if streak >= 12:
        sc += 15
    elif streak >= 8:
        sc += 13
    elif streak >= 5 and pct > 80:
        sc += 11
    elif streak >= 3 and pct > 70:
        sc += 9
    elif pct > 60:
        sc += 6
    else:
        sc += 2

    # 5. LIQUIDEZ (10pts)
    vol = token["vol24h"]
    if vol >= 100e6:
        sc += 10
    elif vol >= 50e6:
        sc += 8
    elif vol >= 20e6:
        sc += 6
    elif vol >= 10e6:
        sc += 4
    elif vol >= 5e6:
        sc += 2
    else:
        sc += 1

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


def score_cross_exchange(differential: float, consistency: float,
                         volume_min: float, fee_ratio: float) -> int:
    """Score a cross-exchange opportunity.

    differential: rate differential between exchanges
    consistency: % of time the differential was favorable
    volume_min: minimum volume of the two exchanges
    fee_ratio: total fees / 3-day revenue (lower is better)
    """
    sc = 0

    # Differential magnitude (30pts)
    d_pct = abs(differential) * 100
    if d_pct >= 0.10:
        sc += 30
    elif d_pct >= 0.05:
        sc += 25
    elif d_pct >= 0.03:
        sc += 20
    elif d_pct >= 0.02:
        sc += 15
    elif d_pct >= 0.01:
        sc += 10
    else:
        sc += 5

    # Consistency (25pts)
    if consistency >= 90:
        sc += 25
    elif consistency >= 80:
        sc += 20
    elif consistency >= 70:
        sc += 15
    elif consistency >= 60:
        sc += 10
    else:
        sc += 5

    # Liquidity (25pts)
    if volume_min >= 100e6:
        sc += 25
    elif volume_min >= 50e6:
        sc += 20
    elif volume_min >= 20e6:
        sc += 15
    elif volume_min >= 10e6:
        sc += 10
    elif volume_min >= 5e6:
        sc += 5
    else:
        sc += 2

    # Fee efficiency (20pts) — lower fee_ratio = better
    if fee_ratio < 0.1:
        sc += 20
    elif fee_ratio < 0.2:
        sc += 16
    elif fee_ratio < 0.3:
        sc += 12
    elif fee_ratio < 0.5:
        sc += 8
    else:
        sc += 3

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
