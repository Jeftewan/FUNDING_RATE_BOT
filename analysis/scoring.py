"""Opportunity scoring v11.0 — re-optimizado hacia ganancia neta (Optuna, 600 trials).

Adoptado desde scripts/scoring_optimizer.py. Ver reports/optimizer_20260617.md.

Cambio de objetivo vs v10.6: el optimizer ahora maximiza **APR-neto (velocidad de
capital)** neto de fees, ajustado a riesgo y predictivo — no la durabilidad. Motivo:
v10.6 premiaba la estabilidad y SUB-premiaba/invertía el yield (daba más score a
0.05%/día que a 0.18%/día, castigando justo las oportunidades más rentables). En
validación el yield queda monótono (más yield = más score), el bucket de score alto
pasa de Net APR negativo (−10.9) a positivo (+34.3) y el decil top deja de empatar
con el tier 2.

Cambios vs v10.6:
  * YIELD: no-monotónico (sweet-spot 0.03–0.10%/día) → MONOTÓNICO con saturación
    (más yield = más score hasta 0.44%/día). El reality-guard pasa de hard-cap a
    un multiplicador suave (×0.90 si current > 4× settlement).
  * Pesos: Stability 21→4 (cv es peso muerto, ρ≈0.05 vs net), Consistency 46→50,
    Yield 17→30, Fee 8→16, Trend 1→0, Liquidity 6→0 (solo spot_perp; ver abajo).
  * Momentum penalties: accel 0→−1, decel −3→−8, neg 0→−4.
  * Z-penalties y hard caps recalibrados (z>2.0→47, streak<3 & pctl≥85→36; el
    reality hard-cap desaparece, ahora vive en el multiplicador de yield).
  * Thin-history defaults re-escalados a los pesos nuevos (stability 15→3,
    consistency 20→22) para no sobre-acreditar símbolos nuevos/DeFi.
  * LIQUIDITY: la optimización valida solo spot_perp (→0). Para cross_exchange/defi
    se CONSERVA la estructura de liquidez de v10.6 (6 pts), para no degradar una
    dimensión que este run no re-validó.

Base dimensions:
  spot_perp (100 pts): Consistency 50 | Yield 30 | Fee 16 | Stability 4 | Liq 0 | Trend 0
  cross_exchange/defi: + Liquidity (6 pts, estructura v10.6 conservada)
"""
import math
from analysis.indicators import compute_all_indicators


def opportunity_score(params: dict) -> int:
    """Unified scoring for any arbitrage opportunity (v11.0)."""
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
    # Defaults re-escalados a los pesos v11.0 (stability máx 4, consistency 50).
    thin = len(rates) < 5

    # -- 1. STABILITY (4 pts) -------------------------------------
    if thin:
        sc += 3  # neutral (re-escalado)
    elif cv < 0.2 and min_ratio > 0.5:   sc += 4
    elif cv < 0.3 and min_ratio > 0.3:   sc += 3
    elif cv < 0.3:                        sc += 3
    elif cv < 0.5:                        sc += 2
    elif cv < 0.8:                        sc += 1
    elif cv < 1.2:                        sc += 0
    else:                                 sc += 0

    # -- 2. CONSISTENCY (50 pts) ----------------------------------
    if thin:
        sc += 22  # neutral baseline (re-escalado)
    elif streak >= 12 and pct >= 90:     sc += 50
    elif streak >= 8 and pct >= 85:      sc += 40
    elif streak >= 5 and pct >= 80:      sc += 33
    elif streak >= 3 and pct >= 70:      sc += 25
    elif pct >= 60:                      sc += 15
    else:                                sc += 3

    # -- 3. LIQUIDITY (cross/defi: 6 pts v10.6; spot_perp: 0 por optimización) --
    # La optimización v11.0 valida solo spot_perp y mandó liquidez a 0 (volumen
    # sin poder predictivo, ρ≈0.07 vs net). Para cross_exchange/defi se conserva
    # la estructura v10.6 para no degradar una dimensión no re-validada.
    if mode == "defi":
        if volume >= 20e6:    sc += 6
        elif volume >= 10e6:  sc += 4
        elif volume >= 3e6:   sc += 3
        elif volume >= 500e3: sc += 2
    elif mode == "cross_exchange":
        if volume >= 30e6:    sc += 6
        elif volume >= 10e6:  sc += 4
        elif volume >= 3e6:   sc += 3
        elif volume >= 1e6:   sc += 2
    # else spot_perp: +0 (liquidez optimizada a 0)

    # -- 4. YIELD (30 pts, MONOTÓNICO con saturación) -------------
    # Más yield = más score hasta saturar; guard suave para spikes no sostenibles.
    yield_day_pct = settlement_avg * ppd * 100
    reality_penalty = settlement_avg > 0 and current_rate > settlement_avg * 4.0
    if   yield_day_pct >= 0.44:  yf = 1.0
    elif yield_day_pct >= 0.19:  yf = 0.90
    elif yield_day_pct >= 0.08:  yf = 0.60
    elif yield_day_pct >= 0.025: yf = 0.40
    else:                        yf = 0.25
    sc += round(30 * yf * (0.90 if reality_penalty else 1.0))

    # -- 5. FEE EFFICIENCY (16 pts) -------------------------------
    if fee_drag < 0.1:     sc += 16
    elif fee_drag < 0.2:   sc += 13
    elif fee_drag < 0.3:   sc += 6
    elif fee_drag < 0.5:   sc += 3
    else:                  sc += 0

    # -- 6. TREND (0 pts) -----------------------------------------
    # w_trend optimizado a 0; los indicadores se computan para las penalizaciones.
    indicators = compute_all_indicators(current_rate, rates)

    # -- 7. MOMENTUM PENALTIES ------------------------------------
    mom_signal = indicators["momentum"].get("signal", "flat")
    if mom_signal == "accelerating":   sc -= 1
    elif mom_signal == "decelerating": sc -= 8
    elif mom_signal == "negative":     sc -= 4

    # -- 8. Z-SCORE PENALTY ---------------------------------------
    z_val = indicators["z_score"].get("z", 0)
    if z_val > 3.3:       sc -= 23
    elif z_val > 2.6:     sc -= 17
    elif z_val > 1.8:     sc -= 13
    elif z_val > 1.5:     sc -= 5
    elif z_val > 0.9:     sc -= 4

    # -- 9. HARD CAPS (z + streak; reality vía el multiplicador de yield) --
    percentile = indicators["percentile"].get("percentile", 0)
    if z_val > 2.0:
        sc = min(sc, 47)
    if not thin and streak < 3 and percentile >= 85:
        sc = min(sc, 36)

    params["_indicators"] = indicators
    return max(0, min(sc, 100))


def stability_grade(score: int) -> str:
    if score >= 85: return "A"
    elif score >= 70: return "B"
    elif score >= 55: return "C"
    return "D"


# Umbrales de grade sobre Net APR predicho por el modelo ML (% anual neto de fees).
# Se usan cuando el modelo está cargado; el score 0–100 calibrado es un percentil
# contra el train y se comprime arriba (todo "A"), así que no sirve para gradar.
# Calibrados sobre la distribución real de model_prediction en prod (spot_perp:
# p50≈30%, p75≈37%, p90≈87%): el opp típico queda B y el top ~25% queda A.
NET_APR_GRADE_A = 40.0
NET_APR_GRADE_B = 20.0
NET_APR_GRADE_C = 8.0


def grade_from_net_apr(net_apr: float) -> str:
    """Grade A/B/C/D según el Net APR predicho (no el score percentil)."""
    if net_apr >= NET_APR_GRADE_A: return "A"
    elif net_apr >= NET_APR_GRADE_B: return "B"
    elif net_apr >= NET_APR_GRADE_C: return "C"
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
