"""Opportunity scoring v10.3 — recalibrated from 90-day backtest results.

Unified formula for spot-perp and cross-exchange opportunities.

v10.3 changes (from v10.2):
  * Acceleration bonus REMOVED — backtest showed it was inverted signal
    (accel ON: −79% APR avg, OFF: −20%). Slope is still computed for
    telemetry but no longer awards points (see indicators.py).
  * Z-score penalty strengthened: new scale 0 to −30 (was 0 to −10).
    Control group showed z≥1.5 predicts −264% APR — old penalty of −2
    was not disuasive.
  * Weights rebalanced toward validated predictors: Consistency and
    Stability had the only positive Spearman correlations (0.55 / 0.31).
    Yield and Liquidity had NEGATIVE Pearson (−0.35 / −0.28) so their
    weight was reduced and Yield was made non-monotonic.
  * Hard caps added for anti-spike scenarios (high z, immature streak
    at high percentile, current rate > 1.5x historical mean).
  * Reality penalty trigger lowered from 2.0x to 1.5x.
  * Bug fix: the extra −5 penalty for z>3 was reading the wrong key
    (`value` vs `z`) and never fired. Removed (now subsumed in new scale).

Base dimensions (100 pts max):
  1. Stability       (30 pts) — CV + min_ratio
  2. Consistency     (30 pts) — streak + favorable %
  3. Liquidity       (10 pts) — 24h volume (filter, not differentiator)
  4. Yield           (15 pts) — non-monotonic, sweet-spot penalizes spikes
  5. Fee Efficiency  (10 pts) — fee_drag ratio
  6. Trend           ( 5 pts) — momentum + regime + percentile (tie-breaker)

Overlay (applied to final score, clamped to 0-100):
  - Mean Reversion penalty (0 to −30 pts via z-score)
  - Hard caps when spike conditions detected
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

    # ── 1. ESTABILIDAD (30 pts) ──────────────────────────────────
    if cv < 0.2 and min_ratio > 0.5:
        sc += 30
    elif cv < 0.3 and min_ratio > 0.3:
        sc += 24
    elif cv < 0.3:
        sc += 19
    elif cv < 0.5:
        sc += 13
    elif cv < 0.8:
        sc += 8
    elif cv < 1.2:
        sc += 4
    else:
        sc += 1

    # ── 2. CONSISTENCIA (30 pts) ─────────────────────────────────
    if streak >= 12 and pct >= 90:
        sc += 30
    elif streak >= 8 and pct >= 85:
        sc += 24
    elif streak >= 5 and pct >= 80:
        sc += 20
    elif streak >= 3 and pct >= 70:
        sc += 15
    elif pct >= 60:
        sc += 9
    else:
        sc += 3

    # ── 3. LIQUIDEZ (10 pts) ─────────────────────────────────────
    # Used as a filter more than a differentiator (Pearson was negative).
    if volume >= 50e6:
        sc += 10
    elif volume >= 20e6:
        sc += 8
    elif volume >= 10e6:
        sc += 6
    elif volume >= 5e6:
        sc += 4
    elif volume >= 1e6:
        sc += 2
    else:
        sc += 0

    # ── 4. YIELD DIARIO — settlement-based, non-monotonic (15 pts)
    # Sweet spot 0.03–0.10% daily; extreme yields are penalized because
    # they strongly correlate with imminent mean reversion.
    yield_day_pct = settlement_avg * ppd * 100

    # Reality check: current rate is abnormally high vs historical avg
    reality_penalty = False
    if settlement_avg > 0 and current_rate > settlement_avg * 1.5:
        reality_penalty = True

    if reality_penalty:
        # Treat as suspect: dampen all yield points
        if yield_day_pct >= 0.10:
            sc += 4
        elif yield_day_pct >= 0.03:
            sc += 6
        elif yield_day_pct >= 0.01:
            sc += 4
        else:
            sc += 1
    else:
        if 0.03 <= yield_day_pct < 0.10:
            sc += 15   # sweet spot
        elif 0.10 <= yield_day_pct < 0.15:
            sc += 12
        elif 0.01 <= yield_day_pct < 0.03:
            sc += 10
        elif 0.15 <= yield_day_pct < 0.25:
            sc += 6    # zona de sospecha
        elif yield_day_pct >= 0.25:
            sc += 2    # probable spike
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

    # ── 6. TREND (5 pts) — momentum + regime + percentile ───────
    # Reduced from 10 to 5 pts: Pearson correlations were negative, so
    # these serve only as tie-breakers among otherwise-equivalent picks.
    indicators = compute_all_indicators(current_rate, rates)

    mom_pts = min(3, indicators["momentum"]["points"])
    pctl_pts = min(1, indicators["percentile"]["points"])
    reg_pts = min(1, indicators["regime"]["points"])
    sc += mom_pts + pctl_pts + reg_pts

    # ── Overlay: Mean Reversion penalty (0 to -30 pts) ───────────
    # Accel bonus removed — see indicators.acceleration_bonus() docstring.
    sc += indicators["z_score"]["penalty"]

    # ── Hard caps: anti-spike safety brakes ──────────────────────
    # These override the score when known failure modes are detected.
    z_val = indicators["z_score"].get("z", 0)
    percentile = indicators["percentile"].get("percentile", 0)

    # Cap 1: z-score high → spike in progress, mean reversion imminent
    if z_val > 2.0:
        sc = min(sc, 40)
    # Cap 2: immature trade sitting at a historical high → likely peak
    if streak < 3 and percentile >= 80:
        sc = min(sc, 45)
    # Cap 3: current rate is >1.5x the historical mean → suspect
    if reality_penalty:
        sc = min(sc, 50)

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
