"""Advanced statistical indicators for funding rate analysis v10.0.

Pure Python + math — zero external dependencies.
Designed to work with 15-30 historical funding rate data points.
"""
import math


def exponential_moving_average(values: list, span: int) -> list:
    """Calculate EMA with given span. Returns list same length as input."""
    if not values or span < 1:
        return []
    alpha = 2 / (span + 1)
    ema = [values[0]]
    for v in values[1:]:
        ema.append(alpha * v + (1 - alpha) * ema[-1])
    return ema


def rate_of_change(values: list, period: int = 4) -> float:
    """Rate of change: (latest - N periods ago) / |N periods ago|.

    Positive = rates increasing, negative = decreasing.
    """
    if len(values) < period + 1:
        return 0.0
    old = values[-(period + 1)]
    new = values[-1]
    if abs(old) < 1e-12:
        return 0.0
    return (new - old) / abs(old)


def momentum_score(rates: list) -> dict:
    """Compute momentum indicators from rate history.

    Returns dict with:
      - roc: rate of change (4-period)
      - ema_ratio: EMA(5) / EMA(15) of latest value
      - acceleration: ROC of ROC (2nd derivative)
      - signal: 'accelerating', 'decelerating', 'flat', 'negative'
      - points: 0-8 score
    """
    if len(rates) < 8:
        return {"roc": 0, "ema_ratio": 1, "acceleration": 0,
                "signal": "insufficient_data", "points": 4}

    abs_rates = [abs(r) for r in rates]

    # Rate of change (4-period)
    roc = rate_of_change(abs_rates, period=4)

    # EMA ratio: short vs long
    ema5 = exponential_moving_average(abs_rates, 5)
    ema15 = exponential_moving_average(abs_rates, min(15, len(abs_rates)))
    ema_ratio = ema5[-1] / ema15[-1] if ema15[-1] > 1e-12 else 1.0

    # Acceleration: ROC of two consecutive ROC windows
    if len(abs_rates) >= 10:
        roc_prev = rate_of_change(abs_rates[:-3], period=4)
        acceleration = roc - roc_prev
    else:
        acceleration = 0.0

    # Determine signal
    if roc > 0.05 and acceleration > 0:
        signal = "accelerating"
        points = 8
    elif roc > 0.05 and acceleration <= 0:
        signal = "decelerating"
        points = 4
    elif roc > -0.05:
        signal = "flat"
        points = 3
    else:
        signal = "negative"
        points = 1

    return {
        "roc": round(roc, 4),
        "ema_ratio": round(ema_ratio, 4),
        "acceleration": round(acceleration, 4),
        "signal": signal,
        "points": points,
    }


def z_score(current_rate: float, rates: list) -> dict:
    """Calculate Z-score of current rate vs historical distribution.

    High |z-score| means the rate is far from normal — likely to revert.

    Returns dict with:
      - z: the z-score value
      - risk: 'extreme', 'high', 'elevated', 'normal'
      - penalty: 0 to -5 points to subtract from score
    """
    if len(rates) < 5:
        return {"z": 0, "risk": "insufficient_data", "penalty": 0}

    abs_current = abs(current_rate)
    abs_rates = [abs(r) for r in rates]
    mean = sum(abs_rates) / len(abs_rates)
    variance = sum((r - mean) ** 2 for r in abs_rates) / len(abs_rates)
    std = math.sqrt(variance)

    if std < 1e-12:
        return {"z": 0, "risk": "normal", "penalty": 0}

    z = (abs_current - mean) / std

    if z > 3.0:
        risk = "extreme"
        penalty = -10
    elif z > 2.5:
        risk = "very_high"
        penalty = -7
    elif z > 2.0:
        risk = "high"
        penalty = -4
    elif z > 1.5:
        risk = "elevated"
        penalty = -2
    elif z > 1.0:
        risk = "slightly_elevated"
        penalty = -1
    else:
        risk = "normal"
        penalty = 0

    return {"z": round(z, 2), "risk": risk, "penalty": penalty}


def rate_percentile(current_rate: float, rates: list) -> dict:
    """Where the current rate sits vs historical range (0-100 percentile).

    Returns dict with:
      - percentile: 0-100
      - context: 'top', 'upper', 'middle', 'lower'
      - points: 1-5 score
    """
    if len(rates) < 5:
        return {"percentile": 50, "context": "insufficient_data", "points": 3}

    abs_current = abs(current_rate)
    abs_rates = sorted(abs(r) for r in rates)
    n = len(abs_rates)

    # Count how many historical rates are below current
    below = sum(1 for r in abs_rates if r < abs_current)
    percentile = (below / n) * 100

    if percentile >= 80:
        context = "top"
        points = 5
    elif percentile >= 50:
        context = "upper"
        points = 3
    else:
        context = "lower"
        points = 1

    return {
        "percentile": round(percentile, 1),
        "context": context,
        "points": points,
    }


def volatility_regime(rates: list, recent_window: int = 8) -> dict:
    """Detect if we're in a high/low volatility regime.

    Compares stddev of recent rates vs all rates.

    Returns dict with:
      - regime: 'high_vol', 'normal', 'low_vol'
      - ratio: recent_std / overall_std
      - points: 1-5 score
    """
    if len(rates) < recent_window + 2:
        return {"regime": "insufficient_data", "ratio": 1.0, "points": 3}

    abs_rates = [abs(r) for r in rates]
    recent = abs_rates[-recent_window:]

    def stddev(vals):
        if len(vals) < 2:
            return 0
        m = sum(vals) / len(vals)
        return math.sqrt(sum((v - m) ** 2 for v in vals) / len(vals))

    std_all = stddev(abs_rates)
    std_recent = stddev(recent)

    if std_all < 1e-12:
        return {"regime": "normal", "ratio": 1.0, "points": 3}

    ratio = std_recent / std_all

    if ratio > 1.5:
        regime = "high_vol"
        points = 5  # Bonanza period — high activity
    elif ratio >= 0.7:
        regime = "normal"
        points = 3
    else:
        regime = "low_vol"
        points = 1

    return {
        "regime": regime,
        "ratio": round(ratio, 2),
        "points": points,
    }


def acceleration_bonus(rates: list, window: int = 8) -> dict:
    """Detect if rates are accelerating upward (linear regression slope).

    Returns dict with:
      - slope: normalized slope
      - bonus: 0 or 2 points
    """
    if len(rates) < window:
        return {"slope": 0.0, "bonus": 0}

    recent = [abs(r) for r in rates[-window:]]
    n = len(recent)
    mean_val = sum(recent) / n

    if mean_val < 1e-12:
        return {"slope": 0.0, "bonus": 0}

    # Simple linear regression: y = a + b*x
    x_mean = (n - 1) / 2
    numerator = sum((i - x_mean) * (recent[i] - mean_val) for i in range(n))
    denominator = sum((i - x_mean) ** 2 for i in range(n))

    if abs(denominator) < 1e-12:
        return {"slope": 0.0, "bonus": 0}

    slope = numerator / denominator
    # Normalize slope relative to mean
    norm_slope = slope / mean_val

    # Bonus if slope is positive and significant (>50% of mean per window)
    if norm_slope > 0.05:
        bonus = 2
    else:
        bonus = 0

    return {
        "slope": round(norm_slope, 4),
        "bonus": bonus,
    }


def detect_exceptional(current_rate: float, rates: list,
                       current_score: int, score_history: list,
                       current_apr: float, apr_history: list) -> dict:
    """Detect if an opportunity is statistically exceptional.

    Criteria (ALL three required):
      1. Rate in percentile >= 90 of its history (top 10%)
      2. Score >= 80 (high quality)
      3. Score > 1.5x historical average score (significantly above normal)

    Bonus context (informational only, does NOT affect is_exceptional):
      - APR > 2x historical average APR

    Returns:
      {"is_exceptional": bool, "reasons": [...], "exceptional_score": 0-3}
    """
    reasons = []
    core_met = 0

    # 1. Rate percentile check (reuse existing function)
    pctl = rate_percentile(current_rate, rates)
    rate_pct = pctl["percentile"]
    if rate_pct >= 90:
        reasons.append(f"Tasa en percentil {rate_pct:.0f} (top 10% historico)")
        core_met += 1

    # 2. Score minimum threshold (raised from 75 to 80)
    if current_score >= 80:
        core_met += 1

    # 3. Score significantly above historical average (primary criterion)
    if score_history:
        avg_score = sum(score_history) / len(score_history)
        if avg_score > 0 and current_score > avg_score * 1.5:
            reasons.append(f"Score {current_score} >> promedio historico ({avg_score:.0f})")
            core_met += 1

    # Bonus context: APR vs historical average (does NOT affect is_exceptional)
    if apr_history:
        avg_apr = sum(apr_history) / len(apr_history)
        if avg_apr > 0 and current_apr > avg_apr * 2:
            reasons.append(f"APR {current_apr:.1f}% > 2x promedio ({avg_apr:.1f}%)")

    # Must meet ALL 3 core criteria to be exceptional
    is_exceptional = core_met >= 3

    return {
        "is_exceptional": is_exceptional,
        "reasons": reasons,
        "exceptional_score": core_met,
        "rate_percentile": rate_pct,
    }


def compute_all_indicators(current_rate: float, rates: list) -> dict:
    """Compute all advanced indicators in one call.

    Args:
        current_rate: the current/latest funding rate
        rates: chronological list of historical rates (oldest first)

    Returns dict with all indicator results + total_adjustment points.
    """
    mom = momentum_score(rates)
    zscore = z_score(current_rate, rates)
    percentile = rate_percentile(current_rate, rates)
    regime = volatility_regime(rates)
    accel = acceleration_bonus(rates)

    # Total points from new dimensions:
    # momentum (0-8) + percentile (1-5) + regime (1-5) + accel (0-2) + z-score penalty (0 to -5)
    total_new_points = (
        mom["points"] +
        percentile["points"] +
        regime["points"] +
        accel["bonus"] +
        zscore["penalty"]
    )

    return {
        "momentum": mom,
        "z_score": zscore,
        "percentile": percentile,
        "regime": regime,
        "acceleration": accel,
        "total_new_points": total_new_points,
        # Summary flags for frontend
        "is_spike_incoming": mom["signal"] == "accelerating" and zscore["risk"] == "normal",
        "is_spike_ending": mom["signal"] == "decelerating" or zscore["risk"] in ("extreme", "very_high", "high"),
        "is_bonanza": regime["regime"] == "high_vol" and mom["points"] >= 4,
    }
