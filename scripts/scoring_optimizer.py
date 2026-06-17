#!/usr/bin/env python3
"""Scoring Optimizer — re-optimiza los pesos del scoring de funding arbitrage.

OPERACIÓN LOCAL Y MANUAL. No corre en Railway; corre en tu PC y lee la DB
PostgreSQL de Railway en SOLO LECTURA (vía DATABASE_URL) para descargar el
histórico de funding_rate_snapshots. No modifica nada en la DB ni despliega.

Qué hace:
  - Reconstruye los features históricos (reusa scripts/_scoring_data.py).
  - Define un scoring PARAMÉTRICO que espeja la estructura de tiers de
    analysis/scoring.py vigente; Optuna busca los pesos/umbrales que maximizan
    durabilidad + rentabilidad neta + monotonicity (no APR forward crudo, que
    premia spikes que revierten).
  - Compara el baseline de producción contra el candidato en un split temporal
    train/val, con guard de overfitting.

NO aplica cambios: solo genera un candidato + reporte para revisión humana.
Si el reporte convence, se copia el candidato a analysis/scoring.py a mano.

Alcance v1: optimiza sobre datos por-pierna (spot_perp), igual que el backtest.
La recalibración de liquidity para cross_exchange/defi queda fuera de v1.

Requiere: pip install -r requirements-dev.txt

Uso:
    python scripts/scoring_optimizer.py                 # run completo
    python scripts/scoring_optimizer.py --trials 20     # prueba rápida
    python scripts/scoring_optimizer.py --force-reload  # ignora caché

Output (en reports/, ninguno auto-aplicado):
    optimizer_YYYYMMDD.md            — comparación baseline vs candidato + veredicto
    scoring_candidate_YYYYMMDD.py    — código candidato listo para revisión
    optimizer_YYYYMMDD_trials.csv    — historial de trials de Optuna
"""

import argparse
import math
import os
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path

# ── project root on sys.path so analysis.* imports work ──────────────────────
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import pandas as pd

try:
    import optuna
    optuna.logging.set_verbosity(optuna.logging.WARNING)
except ImportError:
    sys.exit("ERROR: falta optuna. Corre: pip install -r requirements-dev.txt")

from analysis.indicators import compute_all_indicators
from _scoring_data import load_fr_snapshots, base_window_features

REPORT_DIR = ROOT / "reports"

# ── Optimizer settings ───────────────────────────────────────────────────────
DEFAULT_TRIALS     = 600       # más trials = mejor búsqueda (más lento)
SEARCH_SAMPLE      = 150_000   # filas de train usadas en la búsqueda (perf)
TOTAL_DAYS         = 90        # ventana de datos
OBJECTIVE_HORIZON  = 168       # horizonte forward de REFERENCIA (reporte), en HORAS
LOOKBACK           = 30
MIN_HIST           = 10
MAX_SCORE          = 100
FWD_HOURS          = [72, 168, 336]     # horizontes forward de reporte en horas (3d/7d/14d)
ROUND_TRIP_FEE     = 0.003              # spot 0.10%×2 + perp 0.05%×2 (= tu 0.30% taker)
HOLD_MAX_HOURS     = 336                # tope del hold sostenible medido (14d)
NET_APR_CAP        = 400.0              # techo del APR-neto (evita artefacto holds ultra-cortos)
NET_APR_FLOOR      = -100.0             # piso del APR-neto (no-hold / pérdida acotada)

# ── Tier ratios (shape fija del scoring, linaje v10.5) ───────────────────────
# Los tiers se derivan como round(weight * ratio) para que (a) el baseline
# reproduzca el scoring de producción exacto y (b) el código generado sea legible.
STAB_RATIOS  = [31/31, 24/31, 20/31, 13/31, 8/31, 3/31, 1/31]   # 7 tiers
CONS_RATIOS  = [44/44, 35/44, 29/44, 22/44, 13/44, 3/44]        # 6 tiers
LIQ_RATIOS   = [1.0, 0.75, 0.5, 0.25]                           # spot_perp, +0
YIELD_NORM   = [13/13, 10/13, 9/13, 4/13, 1/13, 1/13]           # 6 buckets
YIELD_REAL   = [3/13, 5/13, 3/13, 1/13]                         # reality penalty
FEE_RATIOS   = [1.0, 0.8, 0.4, 0.2]                             # +0


# ══════════════════════════════════════════════════════════════════════════════
# ── BASELINE (producción actual) ─────────────────────────────────────────────
# ══════════════════════════════════════════════════════════════════════════════
# Espeja analysis/scoring.py vigente. ACTUALIZAR aquí cada vez que se adopte un
# candidato, para que el reporte compare contra lo que está realmente en prod.
BASELINE_LABEL = "v10.6"

BASELINE_PARAMS = {
    "w_stab": 21, "w_cons": 46, "w_liq": 6, "w_yield": 17, "w_fee": 8, "w_trend": 1,
    "z_t1": 0.5, "z_p1": -5,
    "z_t2": 0.9, "z_p2": -9,
    "z_t3": 1.5, "z_p3": -14,
    "z_t4": 2.2, "z_p4": -17,
    "z_t5": 2.6, "z_p5": -18,
    "z_t6": 3.2, "z_p6": -20,
    "mom_accel": 0, "mom_decel": -3, "mom_neg": 0,
    "caps_enabled": True,
    "cap_z_thresh": 2.5, "cap_z_val": 47,
    "cap_streak_thresh": 4, "cap_streak_pctl": 90, "cap_streak_val": 36,
    "reality_thresh": 2.6, "cap_reality_val": 54,
}


# ══════════════════════════════════════════════════════════════════════════════
# ── FEATURE EXTRACTION (pre-compute once, reuse for all trials) ──────────────
# ══════════════════════════════════════════════════════════════════════════════

def extract_features(fr_df: pd.DataFrame) -> pd.DataFrame:
    """Features crudos + indicadores + forward returns + durabilidad por fila.

    Reusa base_window_features para los crudos; añade lo que el optimizador
    necesita y el backtest no (forward en horas, durabilidad, net_apr).
    """
    rows = []
    groups = fr_df.groupby(["symbol", "exchange"])
    total = len(groups)
    print(f"  Extrayendo features de {total} pares (horizontes {FWD_HOURS}h)...")

    for idx, ((symbol, exchange), grp) in enumerate(groups):
        if (idx + 1) % 100 == 0:
            print(f"    {idx+1}/{total}...")

        grp     = grp.sort_values("captured_at").reset_index(drop=True)
        rates   = grp["rate"].tolist()
        volumes = grp["volume_24h"].fillna(0).tolist()
        intervs = grp["interval_hours"].fillna(8).tolist()
        times   = grp["captured_at"].tolist()
        n       = len(grp)

        if n <= MIN_HIST + 1:
            continue
        ih_pair = max(float(grp["interval_hours"].fillna(8).median()), 1.0)
        obj_intervals = max(1, round(OBJECTIVE_HORIZON / ih_pair))

        max_end = n - obj_intervals
        if max_end <= MIN_HIST:
            continue

        for i in range(MIN_HIST, max_end):
            bf = base_window_features(rates, volumes, intervs, i, LOOKBACK)
            if bf is None:
                continue

            curr = bf["current_rate"]
            hist = bf["hist"]
            ppd  = bf["ppd"]
            ih   = bf["interval_h"]

            indicators = compute_all_indicators(curr, hist)
            mom  = indicators.get("momentum", {})
            zsc  = indicators.get("z_score", {})
            pctl = indicators.get("percentile", {})

            # ── Forward returns en horas → intervalos ──
            fwd = {}
            for fwd_h in FWD_HOURS:
                n_int = max(1, round(fwd_h / ih))
                future = rates[i + 1: i + 1 + n_int]
                if len(future) == n_int:
                    total_r = sum(future)
                    apr = (total_r / n_int) * ppd * 365 * 100
                    fwd[f"fwd_{fwd_h}h_apr"] = apr
                    fwd[f"fwd_{fwd_h}h_pos"] = int(total_r > 0)
                    fwd[f"fwd_{fwd_h}h_survival"] = \
                        sum(1 for r in future if r > 0) / n_int

            obj_key = f"fwd_{OBJECTIVE_HORIZON}h_apr"
            if obj_key not in fwd:
                continue

            # ── Hold sostenible: intervalos positivos consecutivos tras entrar ──
            #   net_apr = velocidad de capital neta de fees = lo que el usuario maximiza.
            #   El fee se descuenta UNA vez (round-trip); el APR se acota arriba y abajo
            #   para que el optimizer no persiga el artefacto de holds ultra-cortos.
            max_look_int = min(n - i - 1, round(HOLD_MAX_HOURS / ih))
            consec_pos, cumul_rate = 0, 0.0
            for j in range(1, max_look_int + 1):
                if rates[i + j] > 0:
                    consec_pos += 1
                    cumul_rate += rates[i + j]
                else:
                    break

            duration_hours = consec_pos * ih
            hold_days = duration_hours / 24.0 if duration_hours > 0 else 0
            net_revenue = cumul_rate - ROUND_TRIP_FEE
            if hold_days > 0:
                net_apr = (net_revenue / max(hold_days, 1 / ppd)) * 365 * 100
                net_apr = max(NET_APR_FLOOR, min(net_apr, NET_APR_CAP))
            else:
                net_apr = NET_APR_FLOOR  # no-hold (negativo inmediato): pérdida acotada

            fwd["duration_hours"] = duration_hours
            fwd["hold_surv"] = consec_pos / max_look_int if max_look_int > 0 else 0
            fwd["cumul_rate"] = cumul_rate           # para recomputar net a otros fees
            fwd["hold_days"] = hold_days
            fwd["net_apr"] = net_apr
            fwd["is_profitable"] = int(net_revenue > 0)

            sa = bf["settlement_avg"]
            rows.append({
                "symbol": symbol, "exchange": exchange,
                "captured_at": times[i],
                # crudos para el scoring paramétrico
                "cv": bf["cv"], "min_ratio": bf["min_ratio"],
                "streak": bf["streak"], "pct": bf["pct"],
                "volume": bf["volume"],
                "settlement_avg": sa, "ppd": ppd,
                "fee_drag": bf["fee_drag"],
                "current_rate": curr,
                "reality_ratio": abs(curr) / sa if sa > 1e-12 else 0,
                # indicadores (computados una vez)
                "z_value": zsc.get("z", 0),
                "mom_signal": mom.get("signal", "flat"),
                "mom_points": mom.get("points", 3),
                "pctl_percentile": pctl.get("percentile", 50),
                "pctl_points": pctl.get("points", 3),
                **fwd,
            })

    df = pd.DataFrame(rows)
    df["captured_at"] = pd.to_datetime(df["captured_at"])
    print(f"  {len(df):,} filas de features extraídas")
    return df


# ══════════════════════════════════════════════════════════════════════════════
# ── PARAMETRIC SCORING (espeja analysis/scoring.py) ──────────────────────────
# ══════════════════════════════════════════════════════════════════════════════

def score_v106_baseline(row, p):
    """BASELINE CONGELADO — espeja analysis/scoring.py v10.6 EXACTO.

    Estructura de tiers fija (yield sweet-spot no-monotónico + reality hard-cap).
    Con p == BASELINE_PARAMS reproduce el scoring de producción vigente. NO se
    modifica: es la referencia contra la que se compara el candidato.
    """
    sc = 0.0

    # -- 1. STABILITY --------------------------------------------------
    cv, mr, w = row["cv"], row["min_ratio"], p["w_stab"]
    if   cv < 0.2 and mr > 0.5: sc += w * STAB_RATIOS[0]
    elif cv < 0.3 and mr > 0.3: sc += w * STAB_RATIOS[1]
    elif cv < 0.3:              sc += w * STAB_RATIOS[2]
    elif cv < 0.5:              sc += w * STAB_RATIOS[3]
    elif cv < 0.8:              sc += w * STAB_RATIOS[4]
    elif cv < 1.2:              sc += w * STAB_RATIOS[5]
    else:                       sc += w * STAB_RATIOS[6]

    # -- 2. CONSISTENCY ------------------------------------------------
    streak, pct, w = row["streak"], row["pct"], p["w_cons"]
    if   streak >= 12 and pct >= 90: sc += w * CONS_RATIOS[0]
    elif streak >= 8 and pct >= 85:  sc += w * CONS_RATIOS[1]
    elif streak >= 5 and pct >= 80:  sc += w * CONS_RATIOS[2]
    elif streak >= 3 and pct >= 70:  sc += w * CONS_RATIOS[3]
    elif pct >= 60:                  sc += w * CONS_RATIOS[4]
    else:                            sc += w * CONS_RATIOS[5]

    # -- 3. LIQUIDITY (spot_perp) --------------------------------------
    vol, w = row["volume"], p["w_liq"]
    if   vol >= 50e6: sc += w * LIQ_RATIOS[0]
    elif vol >= 20e6: sc += w * LIQ_RATIOS[1]
    elif vol >= 5e6:  sc += w * LIQ_RATIOS[2]
    elif vol >= 1e6:  sc += w * LIQ_RATIOS[3]
    # else: +0

    # -- 4. YIELD (non-monotonic + reality penalty) --------------------
    settlement_avg = abs(row["settlement_avg"])
    current_rate = abs(row["current_rate"])
    yd = settlement_avg * row["ppd"] * 100
    reality_penalty = (settlement_avg > 0 and
                       current_rate > settlement_avg * p["reality_thresh"])
    w = p["w_yield"]
    if reality_penalty:
        if   yd >= 0.10: sc += w * YIELD_REAL[0]
        elif yd >= 0.03: sc += w * YIELD_REAL[1]
        elif yd >= 0.01: sc += w * YIELD_REAL[2]
        else:            sc += w * YIELD_REAL[3]
    else:
        if   0.03 <= yd < 0.10: sc += w * YIELD_NORM[0]
        elif 0.10 <= yd < 0.15: sc += w * YIELD_NORM[1]
        elif 0.01 <= yd < 0.03: sc += w * YIELD_NORM[2]
        elif 0.15 <= yd < 0.25: sc += w * YIELD_NORM[3]
        elif yd >= 0.25:        sc += w * YIELD_NORM[4]
        else:                   sc += w * YIELD_NORM[5]

    # -- 5. FEE EFFICIENCY ---------------------------------------------
    fd, w = row["fee_drag"], p["w_fee"]
    if   fd < 0.1: sc += w * FEE_RATIOS[0]
    elif fd < 0.2: sc += w * FEE_RATIOS[1]
    elif fd < 0.3: sc += w * FEE_RATIOS[2]
    elif fd < 0.5: sc += w * FEE_RATIOS[3]
    # else: +0

    # -- 6. TREND ------------------------------------------------------
    mom_pts  = min(2, row["mom_points"])
    pctl_pts = min(1, row["pctl_points"])
    sc += (mom_pts + pctl_pts) / 3.0 * p["w_trend"]

    # -- 7. MOMENTUM PENALTIES -----------------------------------------
    sig = row["mom_signal"]
    if   sig == "accelerating": sc += p["mom_accel"]
    elif sig == "decelerating": sc += p["mom_decel"]
    elif sig == "negative":     sc += p["mom_neg"]

    # -- 8. Z-SCORE PENALTY --------------------------------------------
    z = row["z_value"]
    if   z > p["z_t6"]: sc += p["z_p6"]
    elif z > p["z_t5"]: sc += p["z_p5"]
    elif z > p["z_t4"]: sc += p["z_p4"]
    elif z > p["z_t3"]: sc += p["z_p3"]
    elif z > p["z_t2"]: sc += p["z_p2"]
    elif z > p["z_t1"]: sc += p["z_p1"]

    # -- 9. HARD CAPS --------------------------------------------------
    if p["caps_enabled"]:
        if z > p["cap_z_thresh"]:
            sc = min(sc, p["cap_z_val"])
        if streak < p["cap_streak_thresh"] and row["pctl_percentile"] >= p["cap_streak_pctl"]:
            sc = min(sc, p["cap_streak_val"])
        if reality_penalty:
            sc = min(sc, p["cap_reality_val"])

    return max(0, min(sc, MAX_SCORE))


def parametric_score_candidate(row, p):
    """CANDIDATO — yield MONOTÓNICO con saturación + guard anti-reversión suave.

    Diferencias vs baseline v10.6:
      * Sección 4 (yield): non-monotónico sweet-spot → monotónico-creciente con
        umbrales/ratios searchables (y_t1..y_sat, y_r0..y_r3). Más yield = más
        score hasta saturar; nunca castiga el yield alto.
      * Guard anti-reversión: en vez del HARD CAP de reality, un MULTIPLICADOR
        `reality_mult ∈ [0.6,1.0]` que descuenta SOLO el aporte de yield cuando
        el rate actual es un spike (current >> settlement). Premia magnitud
        sostenida, frena solo el pico no sostenible.
      * Resto de dimensiones y penalizaciones idénticas a la estructura v10.x.
    """
    sc = 0.0

    # -- 1. STABILITY --------------------------------------------------
    cv, mr, w = row["cv"], row["min_ratio"], p["w_stab"]
    if   cv < 0.2 and mr > 0.5: sc += w * STAB_RATIOS[0]
    elif cv < 0.3 and mr > 0.3: sc += w * STAB_RATIOS[1]
    elif cv < 0.3:              sc += w * STAB_RATIOS[2]
    elif cv < 0.5:              sc += w * STAB_RATIOS[3]
    elif cv < 0.8:              sc += w * STAB_RATIOS[4]
    elif cv < 1.2:              sc += w * STAB_RATIOS[5]
    else:                       sc += w * STAB_RATIOS[6]

    # -- 2. CONSISTENCY ------------------------------------------------
    streak, pct, w = row["streak"], row["pct"], p["w_cons"]
    if   streak >= 12 and pct >= 90: sc += w * CONS_RATIOS[0]
    elif streak >= 8 and pct >= 85:  sc += w * CONS_RATIOS[1]
    elif streak >= 5 and pct >= 80:  sc += w * CONS_RATIOS[2]
    elif streak >= 3 and pct >= 70:  sc += w * CONS_RATIOS[3]
    elif pct >= 60:                  sc += w * CONS_RATIOS[4]
    else:                            sc += w * CONS_RATIOS[5]

    # -- 3. LIQUIDITY (spot_perp) --------------------------------------
    vol, w = row["volume"], p["w_liq"]
    if   vol >= 50e6: sc += w * LIQ_RATIOS[0]
    elif vol >= 20e6: sc += w * LIQ_RATIOS[1]
    elif vol >= 5e6:  sc += w * LIQ_RATIOS[2]
    elif vol >= 1e6:  sc += w * LIQ_RATIOS[3]

    # -- 4. YIELD (MONOTÓNICO con saturación + guard suave) ------------
    settlement_avg = abs(row["settlement_avg"])
    current_rate = abs(row["current_rate"])
    yd = settlement_avg * row["ppd"] * 100
    reality_penalty = (settlement_avg > 0 and
                       current_rate > settlement_avg * p["reality_thresh"])
    if   yd >= p["y_sat"]: frac = 1.0
    elif yd >= p["y_t3"]:  frac = p["y_r3"]
    elif yd >= p["y_t2"]:  frac = p["y_r2"]
    elif yd >= p["y_t1"]:  frac = p["y_r1"]
    else:                  frac = p["y_r0"]
    rm = p["reality_mult"] if reality_penalty else 1.0
    sc += p["w_yield"] * frac * rm

    # -- 5. FEE EFFICIENCY ---------------------------------------------
    fd, w = row["fee_drag"], p["w_fee"]
    if   fd < 0.1: sc += w * FEE_RATIOS[0]
    elif fd < 0.2: sc += w * FEE_RATIOS[1]
    elif fd < 0.3: sc += w * FEE_RATIOS[2]
    elif fd < 0.5: sc += w * FEE_RATIOS[3]

    # -- 6. TREND ------------------------------------------------------
    mom_pts  = min(2, row["mom_points"])
    pctl_pts = min(1, row["pctl_points"])
    sc += (mom_pts + pctl_pts) / 3.0 * p["w_trend"]

    # -- 7. MOMENTUM PENALTIES -----------------------------------------
    sig = row["mom_signal"]
    if   sig == "accelerating": sc += p["mom_accel"]
    elif sig == "decelerating": sc += p["mom_decel"]
    elif sig == "negative":     sc += p["mom_neg"]

    # -- 8. Z-SCORE PENALTY --------------------------------------------
    z = row["z_value"]
    if   z > p["z_t6"]: sc += p["z_p6"]
    elif z > p["z_t5"]: sc += p["z_p5"]
    elif z > p["z_t4"]: sc += p["z_p4"]
    elif z > p["z_t3"]: sc += p["z_p3"]
    elif z > p["z_t2"]: sc += p["z_p2"]
    elif z > p["z_t1"]: sc += p["z_p1"]

    # -- 9. HARD CAPS (z + streak; reality YA gestionado por reality_mult) --
    if p["caps_enabled"]:
        if z > p["cap_z_thresh"]:
            sc = min(sc, p["cap_z_val"])
        if streak < p["cap_streak_thresh"] and row["pctl_percentile"] >= p["cap_streak_pctl"]:
            sc = min(sc, p["cap_streak_val"])

    return max(0, min(sc, MAX_SCORE))


# ══════════════════════════════════════════════════════════════════════════════
# ── EVALUATION ───────────────────────────────────────────────────────────────
# ══════════════════════════════════════════════════════════════════════════════

def _mono_score(vals):
    vals = [v for v in vals if not (isinstance(v, float) and math.isnan(v))]
    if len(vals) < 2:
        return 1.0
    ok = sum(1 for i in range(len(vals) - 1) if vals[i + 1] > vals[i])
    return ok / (len(vals) - 1)


def _spearman(scores, target_rank):
    """Spearman ρ vía Pearson sobre rangos (evita la dependencia de scipy).

    `target_rank` ya viene rankeado (constante entre trials); solo rankeamos
    los scores. Pearson sobre rangos == Spearman.
    """
    return scores.rank().corr(target_rank)


def attach_ranks(df, horizon=OBJECTIVE_HORIZON):
    """Precomputa los rangos de los targets (constantes entre trials)."""
    df["_r_apr"] = df[f"fwd_{horizon}h_apr"].rank()
    df["_r_net"] = df["net_apr"].rank()
    df["_r_dur"] = df["duration_hours"].rank()
    return df


def _sharpe_clip(series):
    """Sharpe-like (media/desvío) acotado a [-1.5,1.5]→/1.5 → [-1,1] (escala ~Spearman).

    Permite negativo: a fee alto el net_apr del top puede ser negativo, y un Sharpe
    menos-negativo (mayor media / menor desvío) DEBE seguir siendo discriminable.
    """
    s = series.dropna()
    if len(s) < 2:
        return 0.0
    sd = s.std()
    if sd is None or sd == 0 or math.isnan(sd):
        return 0.0
    sh = s.mean() / sd
    return max(-1.5, min(sh, 1.5)) / 1.5


def evaluate_params(feat_df, p, scorer=parametric_score_candidate,
                    horizon=OBJECTIVE_HORIZON):
    """Puntúa todas las filas con (scorer, p) y devuelve métricas para arbitraje.

    Objetivo = APR-neto (velocidad de capital) ajustado a riesgo. La monotonicidad
    se mide sobre net_apr (no sobre APR bruto). `scorer` permite comparar la
    función baseline-v10.6 contra la candidata con la misma maquinaria.
    """
    col_apr  = f"fwd_{horizon}h_apr"
    col_pos  = f"fwd_{horizon}h_pos"
    col_surv = f"fwd_{horizon}h_survival"

    scores = feat_df.apply(lambda row: scorer(row, p), axis=1)

    # Spearman vía rangos precomputados (ver attach_ranks); sin scipy.
    sp_apr = _spearman(scores, feat_df["_r_apr"]) if "_r_apr" in feat_df else 0
    sp_net = _spearman(scores, feat_df["_r_net"]) if "_r_net" in feat_df else 0
    sp_dur = _spearman(scores, feat_df["_r_dur"]) if "_r_dur" in feat_df else 0

    buckets = pd.cut(scores, bins=[0, 40, 55, 70, 85, 101],
                     labels=["<40", "40-55", "55-70", "70-85", "85+"],
                     include_lowest=True)
    b_net  = feat_df.groupby(buckets, observed=True)["net_apr"].mean()
    b_prof = feat_df.groupby(buckets, observed=True)["is_profitable"].mean()

    # Monotonicidad orientada a profit: net_apr y % rentable deben crecer con el score.
    mono_net  = _mono_score(b_net.values)
    mono_prof = _mono_score(b_prof.values)
    monotonicity = (mono_net + mono_prof) / 2.0

    # Métricas de profit sobre el TOP FIJO POR RANGO (15%) — game-proof: el
    # optimizer no puede inflar el net del top encogiendo el set ≥70; solo mejora
    # rankeando las oportunidades rentables arriba.
    srank = scores.rank(pct=True)
    qtop = srank >= 0.85    # top 15% por score
    qbot = srank <= 0.40    # bottom 40%
    nqt, nqb = int(qtop.sum()), int(qbot.sum())

    hit_top = feat_df.loc[qtop, col_pos].mean() if nqt > 10 else 0.5
    hit_bot = feat_df.loc[qbot, col_pos].mean() if nqb > 10 else 0.5
    apr_top = feat_df.loc[qtop, col_apr].mean() if nqt > 10 else 0
    apr_bot = feat_df.loc[qbot, col_apr].mean() if nqb > 10 else 0
    dur_top = feat_df.loc[qtop, "duration_hours"].mean() if nqt > 10 else 0
    profit_top  = feat_df.loc[qtop, "is_profitable"].mean() if nqt > 10 else 0
    net_apr_top = feat_df.loc[qtop, "net_apr"].mean() if nqt > 10 else 0
    # Nivel de ganancia del top normalizado a ~[-1,1] (rango realista ±100 APR) —
    # término "lo máximo posible" del objetivo.
    napr_norm = max(-1.0, min(net_apr_top / 100.0, 1.0))
    # Sharpe del APR-neto en el top: "lo más consistente posible".
    sharpe_top  = _sharpe_clip(feat_df.loc[qtop, "net_apr"]) if nqt > 10 else 0
    surv_top = feat_df.loc[qtop, col_surv].mean() if (nqt > 10 and col_surv in feat_df) else 0

    # Fracción que supera el umbral ABSOLUTO ≥70 — solo para la penalización de
    # selectividad (usabilidad del umbral en la app), no para las métricas de profit.
    pct_top = int((scores >= 70).sum()) / len(scores) * 100
    sel_pen = 0
    if   pct_top < 5:  sel_pen = -0.15
    elif pct_top < 8:  sel_pen = -0.05
    elif pct_top > 35: sel_pen = -0.20
    elif pct_top > 25: sel_pen = -0.10

    def _z(v):  # nan-safe
        return 0 if (isinstance(v, float) and math.isnan(v)) else v

    return {
        "spearman_apr": _z(sp_apr), "spearman_net": _z(sp_net),
        "spearman_dur": _z(sp_dur),
        "monotonicity": monotonicity,
        "mono_net": mono_net, "mono_prof": mono_prof,
        "hit_top": hit_top, "hit_bot": hit_bot, "hit_spread": hit_top - hit_bot,
        "apr_top": apr_top, "apr_bot": apr_bot,
        "dur_top": dur_top, "profit_top": profit_top,
        "net_apr_top": net_apr_top, "napr_norm": napr_norm,
        "sharpe_top": sharpe_top, "surv_top": surv_top,
        "pct_top": pct_top, "selectivity_penalty": sel_pen,
        "scores": scores,
    }


# ══════════════════════════════════════════════════════════════════════════════
# ── OBJECTIVE ────────────────────────────────────────────────────────────────
# ══════════════════════════════════════════════════════════════════════════════

def objective(trial, train_df):
    # Rangos liberados (Etapa 2): w_stab puede caer a 0 (cv es peso muerto),
    # w_yield puede subir a 40 (yield = mejor predictor de APR-neto).
    w_stab  = trial.suggest_int("w_stab",  0,  25)
    w_cons  = trial.suggest_int("w_cons",  25, 50)
    w_liq   = trial.suggest_int("w_liq",   0,  10)
    w_yield = trial.suggest_int("w_yield", 5,  40)
    w_fee   = trial.suggest_int("w_fee",   0,  10)
    w_trend = trial.suggest_int("w_trend", 0,  8)

    total_w = w_stab + w_cons + w_liq + w_yield + w_fee + w_trend
    if total_w == 0:
        return -999
    scale = 100.0 / total_w
    p = {
        "w_stab": w_stab * scale, "w_cons": w_cons * scale,
        "w_liq": w_liq * scale, "w_yield": w_yield * scale,
        "w_fee": w_fee * scale, "w_trend": w_trend * scale,
    }

    p["z_t1"] = trial.suggest_float("z_t1", 0.5, 1.0, step=0.1)
    p["z_t2"] = trial.suggest_float("z_t2", 0.8, 1.3, step=0.1)
    p["z_t3"] = trial.suggest_float("z_t3", 1.2, 1.8, step=0.1)
    p["z_t4"] = trial.suggest_float("z_t4", 1.7, 2.3, step=0.1)
    p["z_t5"] = trial.suggest_float("z_t5", 2.2, 2.8, step=0.1)
    p["z_t6"] = trial.suggest_float("z_t6", 2.8, 3.5, step=0.1)
    if not (p["z_t1"] < p["z_t2"] < p["z_t3"] < p["z_t4"] < p["z_t5"] < p["z_t6"]):
        return -999

    p["z_p1"] = trial.suggest_int("z_p1", -5, 0)
    p["z_p2"] = trial.suggest_int("z_p2", -10, -2)
    p["z_p3"] = trial.suggest_int("z_p3", -16, -5)
    p["z_p4"] = trial.suggest_int("z_p4", -22, -10)
    p["z_p5"] = trial.suggest_int("z_p5", -28, -15)
    p["z_p6"] = trial.suggest_int("z_p6", -36, -20)
    if not (p["z_p1"] > p["z_p2"] > p["z_p3"] > p["z_p4"] > p["z_p5"] > p["z_p6"]):
        return -999

    p["mom_accel"] = trial.suggest_int("mom_accel", -15, 0)
    p["mom_decel"] = trial.suggest_int("mom_decel", -12, 0)
    p["mom_neg"]   = trial.suggest_int("mom_neg", -8, 0)

    p["caps_enabled"]       = trial.suggest_categorical("caps_enabled", [True, False])
    p["cap_z_thresh"]       = trial.suggest_float("cap_z_thresh", 2.0, 3.0, step=0.25)
    p["cap_z_val"]          = trial.suggest_int("cap_z_val", 30, 50)
    p["cap_streak_thresh"]  = trial.suggest_int("cap_streak_thresh", 2, 5)
    p["cap_streak_pctl"]    = trial.suggest_int("cap_streak_pctl", 70, 95, step=5)
    p["cap_streak_val"]     = trial.suggest_int("cap_streak_val", 35, 60)

    # Guard anti-reversión SUAVE (reemplaza el hard cap de reality):
    # reality_thresh más permisivo; reality_mult descuenta solo el aporte de yield.
    p["reality_thresh"]     = trial.suggest_float("reality_thresh", 1.8, 4.0, step=0.1)
    p["reality_mult"]       = trial.suggest_float("reality_mult", 0.6, 1.0, step=0.05)

    # Yield MONOTÓNICO: umbrales (%/día) calibrados a la distribución real
    # (mediana 0.03, p90 0.13, p95 0.22) y ratios crecientes (más yield = más score).
    p["y_t1"]  = trial.suggest_float("y_t1",  0.01, 0.04, step=0.005)
    p["y_t2"]  = trial.suggest_float("y_t2",  0.05, 0.12, step=0.01)
    p["y_t3"]  = trial.suggest_float("y_t3",  0.12, 0.22, step=0.01)
    p["y_sat"] = trial.suggest_float("y_sat", 0.20, 0.45, step=0.01)
    if not (p["y_t1"] < p["y_t2"] < p["y_t3"] < p["y_sat"]):
        return -999
    p["y_r0"] = trial.suggest_float("y_r0", 0.0,  0.30, step=0.05)
    p["y_r1"] = trial.suggest_float("y_r1", 0.20, 0.50, step=0.05)
    p["y_r2"] = trial.suggest_float("y_r2", 0.45, 0.75, step=0.05)
    p["y_r3"] = trial.suggest_float("y_r3", 0.70, 0.95, step=0.05)
    if not (p["y_r0"] < p["y_r1"] < p["y_r2"] < p["y_r3"]):
        return -999

    m = evaluate_params(train_df, p)  # scorer = candidato (default)

    # Gate duro: si no ordena por net_apr, está roto.
    if m["monotonicity"] < 0.5:
        return -999

    # Objetivo: APR-neto (velocidad de capital), predictivo + máximo + consistente.
    #   spearman_net = ranking predictivo  | napr_norm = nivel de ganancia del top
    #   profit_top   = fiabilidad (% rentable) | sharpe_top = ajuste a riesgo
    return (
        0.25 * m["monotonicity"] +
        0.30 * m["spearman_net"] +
        0.20 * m["napr_norm"] +
        0.15 * m["profit_top"] +
        0.10 * m["sharpe_top"] +
        m["selectivity_penalty"]
    )


def best_params_from_study(study):
    bp = study.best_trial.params
    total_w = (bp["w_stab"] + bp["w_cons"] + bp["w_liq"] +
               bp["w_yield"] + bp["w_fee"] + bp["w_trend"])
    scale = 100.0 / total_w
    p = {k: bp[k] * scale for k in
         ("w_stab", "w_cons", "w_liq", "w_yield", "w_fee", "w_trend")}
    for k in ("z_t1", "z_p1", "z_t2", "z_p2", "z_t3", "z_p3", "z_t4", "z_p4",
              "z_t5", "z_p5", "z_t6", "z_p6", "mom_accel", "mom_decel", "mom_neg",
              "caps_enabled", "cap_z_thresh", "cap_z_val", "cap_streak_thresh",
              "cap_streak_pctl", "cap_streak_val", "reality_thresh", "reality_mult",
              "y_t1", "y_t2", "y_t3", "y_sat", "y_r0", "y_r1", "y_r2", "y_r3"):
        p[k] = bp[k]
    return p


# ══════════════════════════════════════════════════════════════════════════════
# ── CODE GENERATION (candidato drop-in para analysis/scoring.py) ─────────────
# ══════════════════════════════════════════════════════════════════════════════

def generate_candidate_code(p, m_train, m_val):
    w_stab  = round(p["w_stab"]);  w_cons = round(p["w_cons"])
    w_liq   = round(p["w_liq"]);   w_yield = round(p["w_yield"])
    w_fee   = round(p["w_fee"]);   w_trend = round(p["w_trend"])
    total_w = w_stab + w_cons + w_liq + w_yield + w_fee + w_trend

    s = [round(w_stab * r) for r in STAB_RATIOS]
    c = [round(w_cons * r) for r in CONS_RATIOS]
    l = [round(w_liq * r) for r in LIQ_RATIOS]
    fp = [round(w_fee * r) for r in FEE_RATIOS]

    caps = ""
    if p["caps_enabled"]:
        caps = f"""
    # -- 9. HARD CAPS (z + streak; reality ya gestionado por reality_mult) --
    percentile = indicators["percentile"].get("percentile", 0)
    if z_val > {p['cap_z_thresh']:.2f}:
        sc = min(sc, {round(p['cap_z_val'])})
    if not thin and streak < {round(p['cap_streak_thresh'])} and percentile >= {round(p['cap_streak_pctl'])}:
        sc = min(sc, {round(p['cap_streak_val'])})
"""
    else:
        caps = "\n    # Hard caps DESHABILITADOS por el optimizador\n"

    return f'''"""Opportunity scoring CANDIDATE — auto-optimizado ({TOTAL_DAYS}d backtest).

Generado por scripts/scoring_optimizer.py el {datetime.now().strftime('%Y-%m-%d %H:%M')}.
NO es producción: revisa reports/optimizer_*.md y, si convence, copia esta
función sobre analysis/scoring.py:opportunity_score.

Validación (val set):
  Spearman net APR : {m_val["spearman_net"]:.3f}  (train {m_train["spearman_net"]:.3f})
  Sharpe top       : {m_val["sharpe_top"]:.3f}  (train {m_train["sharpe_top"]:.3f})
  Monotonicity     : {m_val["monotonicity"]:.2f}   (train {m_train["monotonicity"]:.2f})

Dimensiones ({total_w} pts): Stability {w_stab} | Consistency {w_cons} | Liquidity {w_liq} | Yield {w_yield} | Fee {w_fee} | Trend {w_trend}
"""
import math
from analysis.indicators import compute_all_indicators


def opportunity_score(params: dict) -> int:
    """Unified scoring (candidato optimizado)."""
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
    thin = len(rates) < 5

    # -- 1. STABILITY ({w_stab} pts) ------------------------------
    if thin:
        sc += 15
    elif cv < 0.2 and min_ratio > 0.5:   sc += {s[0]}
    elif cv < 0.3 and min_ratio > 0.3:   sc += {s[1]}
    elif cv < 0.3:                        sc += {s[2]}
    elif cv < 0.5:                        sc += {s[3]}
    elif cv < 0.8:                        sc += {s[4]}
    elif cv < 1.2:                        sc += {s[5]}
    else:                                 sc += {s[6]}

    # -- 2. CONSISTENCY ({w_cons} pts) ----------------------------
    if thin:
        sc += 20
    elif streak >= 12 and pct >= 90:     sc += {c[0]}
    elif streak >= 8 and pct >= 85:      sc += {c[1]}
    elif streak >= 5 and pct >= 80:      sc += {c[2]}
    elif streak >= 3 and pct >= 70:      sc += {c[3]}
    elif pct >= 60:                      sc += {c[4]}
    else:                                sc += {c[5]}

    # -- 3. LIQUIDITY ({w_liq} pts, mode-aware) -------------------
    # Optimizado para spot_perp; cross_exchange/defi conservan v10.5.
    if mode == "defi":
        if volume >= 20e6:    sc += {l[0]}
        elif volume >= 10e6:  sc += {l[1]}
        elif volume >= 3e6:   sc += {l[2]}
        elif volume >= 500e3: sc += {l[3]}
    elif mode == "cross_exchange":
        if volume >= 30e6:    sc += {l[0]}
        elif volume >= 10e6:  sc += {l[1]}
        elif volume >= 3e6:   sc += {l[2]}
        elif volume >= 1e6:   sc += {l[3]}
    else:  # spot_perp
        if volume >= 50e6:    sc += {l[0]}
        elif volume >= 20e6:  sc += {l[1]}
        elif volume >= 5e6:   sc += {l[2]}
        elif volume >= 1e6:   sc += {l[3]}

    # -- 4. YIELD ({w_yield} pts, MONOTÓNICO con saturación) ------
    # Más yield = más score hasta saturar; guard suave reality_mult para spikes.
    yield_day_pct = settlement_avg * ppd * 100
    reality_penalty = settlement_avg > 0 and current_rate > settlement_avg * {p['reality_thresh']:.1f}
    if yield_day_pct >= {p['y_sat']:.2f}:    _yf = 1.0
    elif yield_day_pct >= {p['y_t3']:.2f}:   _yf = {p['y_r3']:.2f}
    elif yield_day_pct >= {p['y_t2']:.2f}:   _yf = {p['y_r2']:.2f}
    elif yield_day_pct >= {p['y_t1']:.3f}:   _yf = {p['y_r1']:.2f}
    else:                                    _yf = {p['y_r0']:.2f}
    sc += round({w_yield} * _yf * ({p['reality_mult']:.2f} if reality_penalty else 1.0))

    # -- 5. FEE EFFICIENCY ({w_fee} pts) --------------------------
    if fee_drag < 0.1:     sc += {fp[0]}
    elif fee_drag < 0.2:   sc += {fp[1]}
    elif fee_drag < 0.3:   sc += {fp[2]}
    elif fee_drag < 0.5:   sc += {fp[3]}

    # -- 6. TREND ({w_trend} pts) ---------------------------------
    indicators = compute_all_indicators(current_rate, rates)
    mom_pts = min(2, indicators["momentum"]["points"])
    pctl_pts = min(1, indicators["percentile"]["points"])
    sc += round((mom_pts + pctl_pts) / 3.0 * {w_trend})

    # -- 7. MOMENTUM PENALTIES ------------------------------------
    mom_signal = indicators["momentum"].get("signal", "flat")
    if mom_signal == "accelerating":   sc += {round(p['mom_accel'])}
    elif mom_signal == "decelerating": sc += {round(p['mom_decel'])}
    elif mom_signal == "negative":     sc += {round(p['mom_neg'])}

    # -- 8. Z-SCORE PENALTY ---------------------------------------
    z_val = indicators["z_score"].get("z", 0)
    if z_val > {p['z_t6']:.1f}:    sc += {round(p['z_p6'])}
    elif z_val > {p['z_t5']:.1f}:  sc += {round(p['z_p5'])}
    elif z_val > {p['z_t4']:.1f}:  sc += {round(p['z_p4'])}
    elif z_val > {p['z_t3']:.1f}:  sc += {round(p['z_p3'])}
    elif z_val > {p['z_t2']:.1f}:  sc += {round(p['z_p2'])}
    elif z_val > {p['z_t1']:.1f}:  sc += {round(p['z_p1'])}
{caps}
    params["_indicators"] = indicators
    return max(0, min(sc, 100))
'''


# ══════════════════════════════════════════════════════════════════════════════
# ── REPORT ───────────────────────────────────────────────────────────────────
# ══════════════════════════════════════════════════════════════════════════════

def _f(v, d=3):
    if v is None or (isinstance(v, float) and math.isnan(v)):
        return "—"
    return f"{v:.{d}f}"


def _md_table(headers, rows_data):
    sep = "|" + "|".join(" :---: " for _ in headers) + "|"
    head = "| " + " | ".join(str(h) for h in headers) + " |"
    body = "\n".join("| " + " | ".join(str(v) for v in row) + " |"
                     for row in rows_data)
    return "\n".join([head, sep, body])


def generate_report(p, m_train, m_val, base_train, base_val,
                    train_df, val_df, study):
    sp_drop = m_train["spearman_net"] - m_val["spearman_net"]
    lines = [
        f"# Optimizer Report — baseline {BASELINE_LABEL} vs candidato",
        "",
        f"**Fecha:** {datetime.now().strftime('%Y-%m-%d %H:%M')}",
        f"**Trials:** {len(study.trials)}",
        f"**Train:** {len(train_df):,} rows (hasta {train_df['captured_at'].max().date()}) | "
        f"**Val:** {len(val_df):,} rows (desde {val_df['captured_at'].min().date()})",
        f"**Objetivo:** APR-neto (velocidad de capital) ajustado a riesgo | "
        f"**Hold máx:** {HOLD_MAX_HOURS}h | **Fee:** {ROUND_TRIP_FEE*100:.2f}% | **Alcance:** spot_perp",
        "",
        "> El optimizador SOLO genera este reporte + un candidato. No aplica "
        "nada. Revisa abajo y, si convence, copia el candidato a "
        "`analysis/scoring.py` a mano.",
        "",
        "## 1. Métricas (validación)",
        "",
        _md_table(
            ["Métrica", f"{BASELINE_LABEL} Train", f"{BASELINE_LABEL} Val",
             "Cand. Train", "Cand. Val", "Δ Val"],
            [
                ["Spearman (Net APR)", _f(base_train["spearman_net"]), _f(base_val["spearman_net"]),
                 _f(m_train["spearman_net"]), _f(m_val["spearman_net"]),
                 _f(m_val["spearman_net"] - base_val["spearman_net"])],
                ["Sharpe top (APR-neto)", _f(base_train["sharpe_top"]), _f(base_val["sharpe_top"]),
                 _f(m_train["sharpe_top"]), _f(m_val["sharpe_top"]),
                 _f(m_val["sharpe_top"] - base_val["sharpe_top"])],
                ["Net APR top", _f(base_train["net_apr_top"], 1), _f(base_val["net_apr_top"], 1),
                 _f(m_train["net_apr_top"], 1), _f(m_val["net_apr_top"], 1),
                 _f(m_val["net_apr_top"] - base_val["net_apr_top"], 1)],
                ["Monotonicity", _f(base_train["monotonicity"], 2), _f(base_val["monotonicity"], 2),
                 _f(m_train["monotonicity"], 2), _f(m_val["monotonicity"], 2),
                 _f(m_val["monotonicity"] - base_val["monotonicity"], 2)],
                ["Profit Rate Top", f'{base_train["profit_top"]*100:.0f}%', f'{base_val["profit_top"]*100:.0f}%',
                 f'{m_train["profit_top"]*100:.0f}%', f'{m_val["profit_top"]*100:.0f}%',
                 f'{(m_val["profit_top"]-base_val["profit_top"])*100:+.0f}pp'],
                ["Duración Top (h)", _f(base_train["dur_top"], 0), _f(base_val["dur_top"], 0),
                 _f(m_train["dur_top"], 0), _f(m_val["dur_top"], 0),
                 _f(m_val["dur_top"] - base_val["dur_top"], 0)],
                ["% en Top (≥70)", f'{base_train["pct_top"]:.1f}%', f'{base_val["pct_top"]:.1f}%',
                 f'{m_train["pct_top"]:.1f}%', f'{m_val["pct_top"]:.1f}%', ""],
            ],
        ),
        "",
    ]
    if sp_drop > 0.05:
        lines.append(f"⚠️ **Overfitting:** Spearman Net cae {sp_drop:.3f} de train a val.\n")

    def _wrow(label, key):
        b = round(BASELINE_PARAMS[key]); c = round(p[key])
        return [label, b, c, f'{c - b:+d}']

    lines += [
        f"## 2. Pesos: {BASELINE_LABEL} vs candidato",
        "",
        _md_table(
            ["Dimensión", BASELINE_LABEL, "Candidato", "Δ"],
            [
                _wrow("Stability",   "w_stab"),
                _wrow("Consistency", "w_cons"),
                _wrow("Liquidity",   "w_liq"),
                _wrow("Yield",       "w_yield"),
                _wrow("Fee Eff.",    "w_fee"),
                _wrow("Trend",       "w_trend"),
            ],
        ),
        "",
        "## 3. Score buckets en validación",
        "",
    ]
    col_apr = f"fwd_{OBJECTIVE_HORIZON}h_apr"
    for label, scores_s in [(BASELINE_LABEL, base_val["scores"]), ("Candidato", m_val["scores"])]:
        lines += [f"### {label}", ""]
        rows = []
        for lo, hi, lbl in [(0, 40, "<40"), (40, 55, "40-55"), (55, 70, "55-70"),
                            (70, 85, "70-85"), (85, 101, "85+")]:
            mask = (scores_s >= lo) & (scores_s < hi)
            sub = val_df[mask]
            if len(sub) < 5:
                continue
            rows.append([lbl, len(sub), _f(sub[col_apr].mean(), 1),
                         f'{sub["is_profitable"].mean()*100:.0f}%',
                         _f(sub["duration_hours"].mean(), 0) + "h",
                         _f(sub["net_apr"].mean(), 1)])
        if rows:
            lines.append(_md_table(
                ["Score", "N", "APR% avg", "Profitable%", "Duración", "Net APR%"], rows))
        lines.append("")

    # Sensibilidad al fee (bucket top del candidato): a partir de qué fee el
    # switching deja de pagar. Recomputa net APR variando solo el fee round-trip.
    lines += ["## 3b. Sensibilidad al fee — top 15% del candidato (val)",
              "",
              "Mismo top-15% por rango que la sección 1. El ranking no cambia con el fee; "
              "cambian los niveles absolutos. Muestra a qué fee el top deja de ser rentable "
              "(→ holdear más o subir el umbral de score).",
              ""]
    _sr = pd.Series(m_val["scores"].values, index=val_df.index).rank(pct=True)
    top = val_df[_sr >= 0.85]
    if len(top) > 10:
        hd = top["hold_days"]; ppd_s = top["ppd"]
        denom = hd.clip(lower=1.0 / ppd_s)
        frows = []
        for f in (0.0015, 0.0020, 0.0030, 0.0040):
            nr = top["cumul_rate"] - f
            apr = ((nr / denom) * 365 * 100).clip(lower=NET_APR_FLOOR, upper=NET_APR_CAP)
            apr = apr.where(hd > 0, NET_APR_FLOOR)
            frows.append([f"{f*100:.2f}%", _f(apr.mean(), 1),
                          f'{(nr > 0).mean()*100:.0f}%'])
        lines.append(_md_table(["Fee round-trip", "Net APR top", "Profitable%"], frows))
    else:
        lines.append("_(muestra insuficiente en el top para la tabla de fees)_")
    lines.append("")

    # Veredicto
    lines += ["## 4. Veredicto", ""]
    mono_ok = m_val["monotonicity"] >= 0.75
    net_better = m_val["spearman_net"] > base_val["spearman_net"]
    profit_better = m_val["net_apr_top"] > base_val["net_apr_top"]
    if mono_ok and net_better and profit_better and sp_drop <= 0.05:
        lines.append("✅ **ADOPTAR** — el candidato mejora el ranking (Spearman) y el APR-neto "
                     "del top manteniendo monotonicity, sin overfitting. Copia a `analysis/scoring.py`.")
    elif mono_ok and (net_better or profit_better):
        lines.append("⚠️ **REVISAR** — mejora parcial (ranking o nivel de ganancia, no ambos). "
                     "Evalúa si compensa el cambio antes de adoptar.")
    else:
        lines.append(f"❌ **MANTENER {BASELINE_LABEL}** — el candidato no mejora de forma "
                     "robusta (o pierde monotonicity / overfittea).")
    lines.append("")
    return "\n".join(lines)


# ══════════════════════════════════════════════════════════════════════════════
# ── MAIN ─────────────────────────────────────────────────────────────────────
# ══════════════════════════════════════════════════════════════════════════════

def _load_dotenv():
    """Carga ROOT/.env (KEY=VALUE por línea) sin pisar lo que ya esté en env."""
    env_path = ROOT / ".env"
    if not env_path.exists():
        return
    for line in env_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, val = line.split("=", 1)
        key, val = key.strip(), val.strip().strip('"').strip("'")
        os.environ.setdefault(key, val)


def main():
    global ROUND_TRIP_FEE
    ap = argparse.ArgumentParser(description="Optimizador local del scoring (read-only sobre la DB)")
    ap.add_argument("--trials", type=int, default=DEFAULT_TRIALS, help="trials de Optuna")
    ap.add_argument("--days", type=int, default=TOTAL_DAYS, help="ventana de datos")
    ap.add_argument("--force-reload", action="store_true", help="ignorar caché de la DB")
    ap.add_argument("--fee", type=float, default=ROUND_TRIP_FEE,
                    help="fee round-trip (fracción) para el target net APR (default 0.003 = 0.30%%)")
    args = ap.parse_args()

    ROUND_TRIP_FEE = args.fee

    # Consola de Windows (cp1252) no imprime acentos ni emojis: forzar UTF-8.
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass

    # Auto-cargar .env (para correr desde cmd/PowerShell sin setear env vars).
    _load_dotenv()

    print("\n" + "=" * 60)
    print(f"  SCORING OPTIMIZER (local, read-only) — baseline {BASELINE_LABEL}")
    print("=" * 60 + "\n")

    print("[1/6] Cargando datos...")
    fr_df = load_fr_snapshots(force_reload=args.force_reload, days=args.days)
    if len(fr_df) < 500:
        sys.exit(f"Insuficientes datos: {len(fr_df)} filas. El bot necesita acumular snapshots.")

    print("[2/6] Extrayendo features...")
    feat_df = extract_features(fr_df)
    if len(feat_df) < 1000:
        sys.exit(f"Insuficientes features: {len(feat_df)} filas.")

    print("[3/6] Split train/val temporal...")
    date_min, date_max = feat_df["captured_at"].min(), feat_df["captured_at"].max()
    val_days = round((date_max - date_min).days / 3)
    cutoff = date_max - timedelta(days=val_days)
    train_df = feat_df[feat_df["captured_at"] < cutoff].copy()
    val_df   = feat_df[feat_df["captured_at"] >= cutoff].copy()
    print(f"  Train: {len(train_df):,} | Val: {len(val_df):,} (cutoff {cutoff.date()})")
    if len(train_df) < 500 or len(val_df) < 200:
        sys.exit("Datos insuficientes para un split robusto.")

    # Rangos precomputados para Spearman (constantes entre trials).
    attach_ranks(train_df); attach_ranks(val_df)

    # La búsqueda usa una muestra del train (basta para estimar correlaciones);
    # el reporte final evalúa baseline y candidato sobre el train/val completos.
    if len(train_df) > SEARCH_SAMPLE:
        search_df = train_df.sample(n=SEARCH_SAMPLE, random_state=42)
        print(f"  Búsqueda sobre muestra de {SEARCH_SAMPLE:,} filas del train")
    else:
        search_df = train_df

    print(f"[4/6] Evaluando baseline {BASELINE_LABEL}...")
    base_train = evaluate_params(train_df, BASELINE_PARAMS, scorer=score_v106_baseline)
    base_val   = evaluate_params(val_df,   BASELINE_PARAMS, scorer=score_v106_baseline)
    print(f"  {BASELINE_LABEL} Val: Sp(net)={base_val['spearman_net']:.3f} "
          f"Sharpe_top={base_val['sharpe_top']:.3f} Mono={base_val['monotonicity']:.2f}")

    print(f"\n[5/6] Optimizando ({args.trials} trials)...\n")
    study = optuna.create_study(
        direction="maximize",
        sampler=optuna.samplers.TPESampler(seed=42),
        pruner=optuna.pruners.MedianPruner(),
    )
    t0 = time.time()
    study.optimize(lambda t: objective(t, search_df), n_trials=args.trials,
                   show_progress_bar=True)
    print(f"\n  Completado en {(time.time()-t0)/60:.1f} min")

    best_p = best_params_from_study(study)

    print("[6/6] Evaluando candidato...")
    m_train = evaluate_params(train_df, best_p)   # scorer = candidato (default)
    m_val   = evaluate_params(val_df,   best_p)
    print(f"  Cand. Val: Sp(net)={m_val['spearman_net']:.3f} "
          f"Sharpe_top={m_val['sharpe_top']:.3f} Mono={m_val['monotonicity']:.2f}")

    REPORT_DIR.mkdir(exist_ok=True)
    today = datetime.now().strftime("%Y%m%d")

    report = generate_report(best_p, m_train, m_val, base_train, base_val,
                             train_df, val_df, study)
    (REPORT_DIR / f"optimizer_{today}.md").write_text(report, encoding="utf-8")

    code = generate_candidate_code(best_p, m_train, m_val)
    (REPORT_DIR / f"scoring_candidate_{today}.py").write_text(code, encoding="utf-8")

    study.trials_dataframe().to_csv(
        REPORT_DIR / f"optimizer_{today}_trials.csv", index=False)

    print(f"\n  Reporte:   reports/optimizer_{today}.md")
    print(f"  Candidato: reports/scoring_candidate_{today}.py")
    print(f"  Trials:    reports/optimizer_{today}_trials.csv")

    mono_ok = m_val["monotonicity"] >= 0.75
    net_better = m_val["spearman_net"] > base_val["spearman_net"]
    profit_better = m_val["net_apr_top"] > base_val["net_apr_top"]
    sp_drop = m_train["spearman_net"] - m_val["spearman_net"]
    print()
    if mono_ok and net_better and profit_better and sp_drop <= 0.05:
        print("  → ✅ ADOPTAR: mejora robusta en val. Revisa el reporte y copia el candidato.")
    elif mono_ok and (net_better or profit_better):
        print("  → ⚠️ REVISAR: mejora parcial. Decide según el reporte.")
    else:
        print(f"  → ❌ MANTENER {BASELINE_LABEL}: el candidato no mejora de forma robusta.")
    print()


if __name__ == "__main__":
    main()
