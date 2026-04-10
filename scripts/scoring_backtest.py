#!/usr/bin/env python3
"""Scoring Backtest — validación histórica de 90 días de la fórmula de scoring.

Analiza si el score predice el rendimiento real de funding, qué componente
es más predictivo, cómo decaen las oportunidades top y si el bonus de
aceleración y el factor de mean reversion añaden valor.

Importa analysis.scoring, analysis.indicators directamente — cero duplicación.

Usage:
    DATABASE_URL=postgresql://... python scripts/scoring_backtest.py
    DATABASE_URL=postgresql://... python scripts/scoring_backtest.py --force-reload

Output:
    reports/backtest_YYYYMMDD.md          — markdown report con tablas
    reports/backtest_YYYYMMDD_features.csv — dataset completo de features + fwd returns
    cache/fr_snapshots.csv                — caché local (se invalida si >1h)
    cache/score_snapshots.csv             — caché local (se invalida si >1h)
"""

import argparse
import math
import os
import statistics
import sys
from collections import defaultdict
from datetime import datetime, timezone, timedelta
from pathlib import Path

# ── project root on sys.path so analysis.* imports work ──────────────────────
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import pandas as pd
from sqlalchemy import create_engine, text

from analysis.scoring import opportunity_score
from analysis.indicators import compute_all_indicators  # noqa: F401 (validate import)

# ── Constants ─────────────────────────────────────────────────────────────────
LOOKBACK  = 30           # historical intervals for feature computation
MIN_HIST  = 10           # minimum intervals required
HORIZONS  = [1, 8, 24, 72]
DECAY_STEPS = [1, 2, 4, 8, 12, 16, 24]   # checkpoints for decay curves
CACHE_DIR  = ROOT / "cache"
REPORT_DIR = ROOT / "reports"

SCORE_BUCKETS = [
    ("<40",   0,  40),
    ("40–55", 40,  55),
    ("55–70", 55,  70),
    ("70–85", 70,  85),
    ("85+",   85, 101),
]

# ── Database ──────────────────────────────────────────────────────────────────

def _get_engine():
    url = os.environ.get("DATABASE_URL", "")
    if not url:
        sys.exit("ERROR: DATABASE_URL no está configurada.")
    if url.startswith("postgres://"):
        url = "postgresql://" + url[len("postgres://"):]
    return create_engine(url, pool_pre_ping=True)


# ── Load data ─────────────────────────────────────────────────────────────────

def load_data(force_reload: bool = False) -> tuple:
    """Pull funding_rate_snapshots + score_snapshots.

    Caches results as CSV.  Returns (fr_df, ss_df).
    """
    CACHE_DIR.mkdir(exist_ok=True)
    cache_fr = CACHE_DIR / "fr_snapshots.csv"
    cache_ss = CACHE_DIR / "score_snapshots.csv"

    if not force_reload and cache_fr.exists() and cache_ss.exists():
        age_h = (datetime.now().timestamp() - cache_fr.stat().st_mtime) / 3600
        if age_h < 1.0:
            print(f"  Usando caché ({age_h:.1f}h de antigüedad)")
            fr = pd.read_csv(cache_fr, parse_dates=["captured_at"])
            ss = pd.read_csv(cache_ss, parse_dates=["captured_at"])
            return fr, ss

    print("  Consultando base de datos...")
    engine = _get_engine()
    cutoff = datetime.now(timezone.utc) - timedelta(days=90)

    try:
        with engine.connect() as conn:
            fr = pd.read_sql(
                text("""
                    SELECT symbol, exchange, rate, volume_24h, interval_hours,
                           funding_ts, captured_at
                    FROM   funding_rate_snapshots
                    WHERE  captured_at >= :cutoff
                    ORDER  BY symbol, exchange, captured_at
                """),
                conn, params={"cutoff": cutoff},
            )
            ss = pd.read_sql(
                text("""
                    SELECT symbol, exchange, mode, score, funding_rate, apr,
                           volume_24h, z_score, momentum_signal, captured_at
                    FROM   score_snapshots
                    ORDER  BY symbol, exchange, captured_at
                """),
                conn,
            )
    except Exception as exc:
        sys.exit(f"Error al consultar la DB: {exc}")

    print(f"  funding_rate_snapshots : {len(fr):,} filas")
    print(f"  score_snapshots        : {len(ss):,} filas")

    fr.to_csv(cache_fr, index=False)
    ss.to_csv(cache_ss, index=False)
    return fr, ss


# ── Scoring component helpers (mirrors scoring.py logic exactly) ──────────────

def _stab_pts(cv: float, min_ratio: float) -> int:
    if cv < 0.2 and min_ratio > 0.5: return 25
    if cv < 0.3 and min_ratio > 0.3: return 20
    if cv < 0.3:                     return 16
    if cv < 0.5:                     return 11
    if cv < 0.8:                     return  7
    if cv < 1.2:                     return  3
    return 1


def _cons_pts(streak: int, pct: float) -> int:
    if streak >= 12 and pct >= 90: return 20
    if streak >=  8 and pct >= 85: return 16
    if streak >=  5 and pct >= 80: return 13
    if streak >=  3 and pct >= 70: return 10
    if pct >= 60:                  return  6
    return 2


def _liq_pts(volume: float) -> int:
    if volume >= 100e6: return 15
    if volume >=  50e6: return 12
    if volume >=  20e6: return  9
    if volume >=  10e6: return  6
    if volume >=   5e6: return  3
    return 1


def _yield_pts(settlement_avg: float, ppd: float, reality_penalty: bool) -> int:
    yd = settlement_avg * ppd * 100
    if reality_penalty:
        if yd >= 0.15: return 13
        if yd >= 0.10: return  9
        if yd >= 0.06: return  6
        return 2
    else:
        if yd >= 0.15: return 20
        if yd >= 0.10: return 17
        if yd >= 0.06: return 13
        if yd >= 0.03: return  9
        if yd >= 0.01: return  5
        return 1


def _fee_pts(fee_drag: float) -> int:
    if fee_drag < 0.1: return 10
    if fee_drag < 0.2: return  8
    if fee_drag < 0.3: return  6
    if fee_drag < 0.5: return  4
    return 1


def _estimate_fee_drag(settlement_avg: float, ppd: float,
                       hold_days: int = 30) -> float:
    """Estimate fee_drag = round-trip fees / expected revenue over hold_days.

    Round-trip: ~0.30% (spot 0.10% ×2 + perp 0.05% ×2).
    """
    revenue = abs(settlement_avg) * ppd * hold_days
    if revenue < 1e-10:
        return 1.0
    return min(0.003 / revenue, 1.0)


# ── Build features ────────────────────────────────────────────────────────────

def build_features(fr_df: pd.DataFrame) -> pd.DataFrame:
    """Compute scoring parameters and forward returns for each data point.

    For every snapshot at index T (with enough lookback + forward data):
      - Recomputes all scoring dimensions from the lookback window
      - Calls opportunity_score() for the total score
      - Adds per-component breakdown
      - Adds forward APR% at HORIZONS intervals
      - Adds decay ratio at DECAY_STEPS (rate(T+k)/rate(T))
    """
    rows = []
    max_horizon = max(HORIZONS)
    max_decay   = max(DECAY_STEPS)

    for (symbol, exchange), grp in fr_df.groupby(["symbol", "exchange"]):
        grp     = grp.sort_values("captured_at").reset_index(drop=True)
        rates   = grp["rate"].tolist()
        volumes = grp["volume_24h"].fillna(0).tolist()
        intervs = grp["interval_hours"].fillna(8).tolist()
        times   = grp["captured_at"].tolist()
        n       = len(grp)

        # Need MIN_HIST lookback + max of horizons forward
        min_start = MIN_HIST
        max_end   = n - max(max_horizon, max_decay)
        if max_end <= min_start:
            continue

        for i in range(min_start, max_end):
            lb_start = max(0, i - LOOKBACK)
            hist     = rates[lb_start:i]      # lookback window (oldest→newest)
            curr     = rates[i]
            vol      = volumes[i]
            ih       = max(float(intervs[i]), 1.0)
            ppd      = 24.0 / ih

            # Absolute rates for statistics
            abs_hist = [abs(r) for r in hist if abs(r) > 1e-12]
            if len(abs_hist) < 5:
                continue

            mean_h = statistics.mean(abs_hist)
            std_h  = statistics.stdev(abs_hist) if len(abs_hist) > 1 else 0.0
            cv     = std_h / mean_h if mean_h > 1e-12 else 999.0

            # min_ratio: minimum absolute rate / mean absolute rate
            min_ratio = min(abs_hist) / mean_h if mean_h > 1e-12 else 0.0

            # streak: consecutive positive from end
            streak = 0
            for r in reversed(hist):
                if r > 0:
                    streak += 1
                else:
                    break

            pct            = sum(1 for r in hist if r > 0) / len(hist) * 100
            settlement_avg = mean_h
            fee_drag       = _estimate_fee_drag(settlement_avg, ppd)
            reality_penalty = settlement_avg > 0 and abs(curr) > settlement_avg * 2

            params = {
                "cv":              cv,
                "min_ratio":       min_ratio,
                "streak":          streak,
                "pct":             pct,
                "volume":          vol,
                "settlement_avg":  settlement_avg,
                "payments_per_day": ppd,
                "fee_drag":        fee_drag,
                "current_rate":    curr,
                "rates":           hist,
            }
            score = opportunity_score(params)
            ind   = params.get("_indicators", {})

            mom   = ind.get("momentum",     {})
            pctl  = ind.get("percentile",   {})
            regm  = ind.get("regime",       {})
            accel = ind.get("acceleration", {})
            zscore = ind.get("z_score",     {})

            # Component breakdown
            stab_c  = _stab_pts(cv, min_ratio)
            cons_c  = _cons_pts(streak, pct)
            liq_c   = _liq_pts(vol)
            yld_c   = _yield_pts(settlement_avg, ppd, reality_penalty)
            fee_c   = _fee_pts(fee_drag)
            trend_c = (min(5, mom.get("points",  0)) +
                       min(3, pctl.get("points", 0)) +
                       min(2, regm.get("points", 0)))
            acc_bonus = accel.get("bonus",   0)
            z_pen     = zscore.get("penalty", 0)

            # Forward returns: APR% at each horizon
            fwd_cols: dict = {}
            for h in HORIZONS:
                future = rates[i + 1 : i + 1 + h]
                if len(future) == h:
                    total = sum(future)
                    avg_daily = (total / h) * ppd           # avg daily rate
                    apr_pct   = avg_daily * 365 * 100       # APR%
                    fwd_cols[f"fwd_{h}_apr"]   = apr_pct
                    fwd_cols[f"fwd_{h}_total"] = total * 100  # cumulative %
                    fwd_cols[f"fwd_{h}_pos"]   = int(total > 0)
                else:
                    fwd_cols[f"fwd_{h}_apr"]   = None
                    fwd_cols[f"fwd_{h}_total"] = None
                    fwd_cols[f"fwd_{h}_pos"]   = None

            # Decay ratios: rate(T+k) / rate(T) at each decay checkpoint
            abs_curr = abs(curr) if abs(curr) > 1e-12 else None
            decay_cols: dict = {}
            for k in DECAY_STEPS:
                if i + k < n and abs_curr is not None:
                    decay_cols[f"decay_{k}"] = abs(rates[i + k]) / abs_curr
                else:
                    decay_cols[f"decay_{k}"] = None

            rows.append({
                "symbol":         symbol,
                "exchange":       exchange,
                "captured_at":    times[i],
                "interval_h":     ih,
                # ── Score
                "score":          score,
                "current_rate":   curr * 100,      # pct per interval
                "volume":         vol,
                # ── Components (for correlation analysis)
                "c_stability":    stab_c,
                "c_consistency":  cons_c,
                "c_liquidity":    liq_c,
                "c_yield":        yld_c,
                "c_fee":          fee_c,
                "c_trend":        trend_c,
                "c_accel_bonus":  acc_bonus,
                "c_z_penalty":    z_pen,
                # ── Indicator detail
                "z_value":        zscore.get("z",       0.0),
                "z_risk":         zscore.get("risk",    "normal"),
                "mom_signal":     mom.get("signal",     ""),
                "mom_roc":        mom.get("roc",        0.0),
                "accel_slope":    accel.get("slope",    0.0),
                "regime":         regm.get("regime",    "normal"),
                "percentile":     pctl.get("percentile", 50.0),
                # ── Raw features
                "cv":             round(cv, 4),
                "min_ratio":      round(min_ratio, 4),
                "streak":         streak,
                "pct_positive":   round(pct, 1),
                "settlement_avg": round(settlement_avg * 100, 6),  # pct
                "fee_drag":       round(fee_drag, 4),
                # ── Forward returns
                **fwd_cols,
                # ── Decay ratios
                **decay_cols,
            })

    df = pd.DataFrame(rows)
    n_pairs = df.groupby(["symbol", "exchange"]).ngroups if len(df) else 0
    print(f"  {len(df):,} filas de features en {n_pairs} pares")
    return df


# ── Markdown helpers ──────────────────────────────────────────────────────────

def _md_table(headers: list, rows_data: list) -> str:
    sep = "|" + "|".join(" :---: " for _ in headers) + "|"
    head = "| " + " | ".join(str(h) for h in headers) + " |"
    body = "\n".join(
        "| " + " | ".join(str(v) for v in row) + " |"
        for row in rows_data
    )
    return "\n".join([head, sep, body])


def _f(v, d: int = 2) -> str:
    """Format a number or return '—' for None/NaN."""
    if v is None:
        return "—"
    if isinstance(v, float) and math.isnan(v):
        return "—"
    return f"{v:.{d}f}"


def _enough(df: pd.DataFrame, col: str, min_n: int = 20) -> bool:
    return col in df.columns and df[col].notna().sum() >= min_n


# ── Analysis 1: Score vs Future Returns ──────────────────────────────────────

def analysis_1_score_vs_future(df: pd.DataFrame) -> str:
    lines = [
        "## Análisis 1: Score vs Rendimiento Futuro",
        "",
        "> ¿Los scores más altos predicen mayor APR real?  "
        "Hit Rate = % de ventanas con yield positivo.",
        "",
    ]

    for h in HORIZONS:
        col_apr = f"fwd_{h}_apr"
        col_pos = f"fwd_{h}_pos"
        if not _enough(df, col_apr, 20):
            continue

        sub = df.dropna(subset=[col_apr])
        rows_data = []
        for label, lo, hi in SCORE_BUCKETS:
            bucket = sub[(sub["score"] >= lo) & (sub["score"] < hi)]
            if len(bucket) < 5:
                continue
            apr_v = bucket[col_apr].tolist()
            pos_v = bucket[col_pos].dropna().tolist()
            hit   = pos_v.count(1) / len(pos_v) * 100 if pos_v else 0
            rows_data.append([
                label,
                len(bucket),
                _f(statistics.mean(apr_v)),
                _f(statistics.median(apr_v)),
                f"{hit:.1f}%",
            ])

        if rows_data:
            # approximate days assuming 8h intervals
            approx_d = h * 8 / 24
            label_h  = f"t+{h} ({approx_d:.0f}d)" if approx_d >= 1 else f"t+{h} intervalos"
            lines.append(f"### Horizonte {label_h}")
            lines.append("")
            lines.append(_md_table(
                ["Score bucket", "N", "APR% avg", "APR% median", "Hit Rate"],
                rows_data,
            ))
            lines.append("")

    return "\n".join(lines)


# ── Analysis 2: Component Correlations ───────────────────────────────────────

def analysis_2_component_correlations(df: pd.DataFrame) -> str:
    lines = [
        "## Análisis 2: Correlaciones por Componente",
        "",
        "> Correlación de Pearson entre cada dimensión del score y el APR futuro.  "
        "Valor absoluto más alto = más predictivo.",
        "",
    ]

    components = {
        "c_stability":   "Estabilidad   (0-25)",
        "c_consistency": "Consistencia  (0-20)",
        "c_liquidity":   "Liquidez      (0-15)",
        "c_yield":       "Yield         (0-20)",
        "c_fee":         "Fee Efficiency(0-10)",
        "c_trend":       "Trend         (0-10)",
        "c_accel_bonus": "Accel bonus   (+2)",
        "c_z_penalty":   "MR penalty    (-10/0)",
        "score":         "★ Score Total (0-100)",
    }

    for h in [8, 24, 72]:
        col = f"fwd_{h}_apr"
        if not _enough(df, col, 20):
            continue
        sub = df.dropna(subset=[col])

        rows_data = []
        for comp_col, label in components.items():
            if comp_col not in sub.columns:
                continue
            try:
                r = sub[comp_col].corr(sub[col], method="pearson")
                if math.isnan(r):
                    continue
                # Spearman as secondary
                rs = sub[comp_col].corr(sub[col], method="spearman")
                rows_data.append([label, _f(r, 3), _f(rs, 3)])
            except Exception:
                pass

        rows_data.sort(key=lambda x: abs(float(x[1])) if x[1] != "—" else 0,
                       reverse=True)

        if rows_data:
            approx_d = h * 8 / 24
            lines.append(f"### Horizonte t+{h} ({approx_d:.0f}d)")
            lines.append("")
            lines.append(_md_table(
                ["Componente", "Pearson r", "Spearman ρ"],
                rows_data,
            ))
            lines.append("")

    return "\n".join(lines)


# ── Analysis 3: Decay Curves ──────────────────────────────────────────────────

def analysis_3_decay_curves(df: pd.DataFrame) -> str:
    """Average decay of rate relative to entry for top-scored opportunities."""
    lines = [
        "## Análisis 3: Curvas de Decaimiento",
        "",
        "> Para oportunidades con score ≥ 70, ¿cómo evoluciona el rate relativo?  "
        "decay_k = rate(T+k) / rate(T).  1.0 = sin cambio, <1 = cae.",
        "",
    ]

    top = df[df["score"] >= 70].copy()
    if len(top) < 5:
        lines.append("_Menos de 5 oportunidades score≥70 disponibles._")
        return "\n".join(lines)

    decay_series: dict[str, list] = defaultdict(list)

    for _, row in top.iterrows():
        bucket = "85+" if row["score"] >= 85 else "70–84"
        vals = []
        for k in DECAY_STEPS:
            col = f"decay_{k}"
            v   = row.get(col)
            vals.append(v if (v is not None and not (isinstance(v, float) and math.isnan(v))) else None)

        if all(v is None for v in vals):
            continue
        decay_series[bucket].append(vals)

    if not decay_series:
        lines.append("_Sin datos de decaimiento suficientes._")
        return "\n".join(lines)

    for bucket in sorted(decay_series.keys()):
        series_list = decay_series[bucket]
        n = len(series_list)

        rows_data = []
        for j, k in enumerate(DECAY_STEPS):
            vals = [s[j] for s in series_list if s[j] is not None]
            if not vals:
                continue
            avg = statistics.mean(vals)
            trend = "▼ revertido" if avg < 0.7 else ("≈ estable" if avg < 1.2 else "▲ subió")
            rows_data.append([
                f"T+{k}",
                f"~{k * 8 / 24:.1f}d",
                f"{avg:.2f}x",
                trend,
            ])

        if rows_data:
            lines.append(f"### Score bucket {bucket}  (N={n})")
            lines.append("")
            lines.append(_md_table(
                ["Paso", "Tiempo (~8h/intervalo)", "Rate relativo", "Estado"],
                rows_data,
            ))
            lines.append("")

    return "\n".join(lines)


# ── Analysis 4: Mean Reversion Factor ────────────────────────────────────────

def analysis_4_mean_reversion(df: pd.DataFrame) -> str:
    lines = [
        "## Análisis 4: Factor de Mean Reversion (Z-Score)",
        "",
        "> ¿Un z-score alto (spike) predice menor rendimiento futuro?  "
        "Esto valida la penalización de -10 pts.",
        "",
    ]

    z_buckets = [
        ("Normal  z<1",    0.0,  1.0),
        ("Elevated 1–2",   1.0,  2.0),
        ("High    2–3",    2.0,  3.0),
        ("Extreme z>3",    3.0, 999.0),
    ]

    for h in [8, 24]:
        col = f"fwd_{h}_apr"
        if not _enough(df, col, 20):
            continue
        sub = df.dropna(subset=[col, "z_value"])

        rows_data = []
        for label, zlo, zhi in z_buckets:
            bkt = sub[(sub["z_value"] >= zlo) & (sub["z_value"] < zhi)]
            if len(bkt) < 5:
                continue
            apr_v = bkt[col].tolist()
            rows_data.append([
                label,
                len(bkt),
                _f(statistics.mean(apr_v)),
                _f(statistics.median(apr_v)),
            ])

        if rows_data:
            approx_d = h * 8 / 24
            lines.append(f"### Horizonte t+{h} ({approx_d:.0f}d)")
            lines.append("")
            lines.append(_md_table(
                ["Z-Score Range", "N", "APR% avg", "APR% median"],
                rows_data,
            ))
            lines.append("")

    # Control: same score range (55-75), z normal vs z alto
    lines.append("### Control: Score 55–75 — z normal vs z alto")
    lines.append("")
    ctrl = df[(df["score"] >= 55) & (df["score"] <= 75)].dropna(subset=["z_value"])
    for h in [24]:
        col = f"fwd_{h}_apr"
        sub2 = ctrl.dropna(subset=[col])
        if len(sub2) < 10:
            continue
        low_z  = sub2[sub2["z_value"] <  1.5][col]
        high_z = sub2[sub2["z_value"] >= 1.5][col]
        if len(low_z) >= 5 and len(high_z) >= 5:
            lines.append(_md_table(
                ["Grupo", "N", f"APR% avg fwd_{h}"],
                [
                    [f"z < 1.5 (sin penalización)",  len(low_z),  _f(low_z.mean())],
                    [f"z ≥ 1.5 (penalizado)",        len(high_z), _f(high_z.mean())],
                ],
            ))
            lines.append("")

    return "\n".join(lines)


# ── Analysis 5: Acceleration Bonus Value ─────────────────────────────────────

def analysis_5_acceleration_value(df: pd.DataFrame) -> str:
    lines = [
        "## Análisis 5: Valor del Bonus de Aceleración (+2 pts)",
        "",
        "> ¿Las oportunidades con aceleración (slope positivo) ofrecen mayor yield?",
        "",
    ]

    acc_on  = df[df["c_accel_bonus"] == 2]
    acc_off = df[df["c_accel_bonus"] == 0]

    # APR comparison
    rows_apr = []
    for h in HORIZONS:
        col = f"fwd_{h}_apr"
        on_v  = acc_on.dropna(subset=[col])[col]
        off_v = acc_off.dropna(subset=[col])[col]
        if len(on_v) < 5 or len(off_v) < 5:
            continue
        rows_apr.append([
            f"t+{h}",
            len(on_v),
            _f(on_v.mean()),
            len(off_v),
            _f(off_v.mean()),
            _f(on_v.mean() - off_v.mean()),
        ])

    if rows_apr:
        lines.append("### APR% promedio: aceleración ON vs OFF")
        lines.append("")
        lines.append(_md_table(
            ["Horizonte", "N (ON)", "APR% ON", "N (OFF)", "APR% OFF", "Δ APR%"],
            rows_apr,
        ))
        lines.append("")

    # Hit rate comparison
    rows_hr = []
    for h in [1, 8]:
        col = f"fwd_{h}_pos"
        on_v  = acc_on.dropna(subset=[col])[col]
        off_v = acc_off.dropna(subset=[col])[col]
        if len(on_v) < 5 or len(off_v) < 5:
            continue
        rows_hr.append([
            f"t+{h}",
            f"{on_v.mean()*100:.1f}%",
            f"{off_v.mean()*100:.1f}%",
        ])

    if rows_hr:
        lines.append("### Hit Rate (% intervalos positivos)")
        lines.append("")
        lines.append(_md_table(
            ["Horizonte", "Hit Rate (ON)", "Hit Rate (OFF)"],
            rows_hr,
        ))
        lines.append("")

    # Breakdown by score tier to control for score level
    lines.append("### Por tier de score (control)")
    lines.append("")
    for label, lo, hi in SCORE_BUCKETS:
        col = "fwd_8_apr"
        tier = df[(df["score"] >= lo) & (df["score"] < hi)].dropna(subset=[col])
        on_v  = tier[tier["c_accel_bonus"] == 2][col]
        off_v = tier[tier["c_accel_bonus"] == 0][col]
        if len(on_v) >= 5 and len(off_v) >= 5:
            lines.append(f"- **{label}**: accel ON `{_f(on_v.mean())}%` vs OFF `{_f(off_v.mean())}%`"
                         f"  (N={len(on_v)}vs{len(off_v)})")

    lines.append("")
    return "\n".join(lines)


# ── Write report ──────────────────────────────────────────────────────────────

def write_report(sections: list, feat_df: pd.DataFrame) -> Path:
    REPORT_DIR.mkdir(exist_ok=True)
    today = datetime.now().strftime("%Y%m%d")
    report_path = REPORT_DIR / f"backtest_{today}.md"
    csv_path    = REPORT_DIR / f"backtest_{today}_features.csv"

    n_pairs = feat_df.groupby(["symbol", "exchange"]).ngroups if len(feat_df) else 0
    header = (
        f"# Backtest Report: Validación del Scoring Formula\n\n"
        f"**Fecha:** {datetime.now().strftime('%Y-%m-%d %H:%M')}  \n"
        f"**Ventana:** 90 días  \n"
        f"**Lookback para features:** {LOOKBACK} intervalos  \n"
        f"**Horizontes evaluados:** t+{HORIZONS}  \n"
        f"**Total data points:** {len(feat_df):,}  \n"
        f"**Pares analizados:** {n_pairs}  \n\n"
        f"---\n\n"
    )

    with open(report_path, "w", encoding="utf-8") as f:
        f.write(header)
        for section in sections:
            f.write(section)
            f.write("\n\n---\n\n")

    feat_df.to_csv(csv_path, index=False)
    print(f"  Reporte  : {report_path}")
    print(f"  CSV      : {csv_path}")
    return report_path


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="90-day backtest de la fórmula de scoring de funding rate",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="Ejemplo:\n  DATABASE_URL=postgresql://... python scripts/scoring_backtest.py",
    )
    parser.add_argument(
        "--force-reload", action="store_true",
        help="Ignorar caché y re-consultar la base de datos",
    )
    args = parser.parse_args()

    print("\n=== Scoring Backtest ===\n")

    print("[1/4] Cargando datos...")
    fr_df, ss_df = load_data(force_reload=args.force_reload)

    if len(fr_df) == 0:
        print("\nERROR: No hay datos en funding_rate_snapshots.")
        print("El bot necesita correr varios días acumulando snapshots antes de hacer backtest.")
        sys.exit(1)
    if len(fr_df) < 200:
        print(f"\nADVERTENCIA: Solo {len(fr_df)} filas — resultados pueden ser poco representativos.")
        print("Se recomienda esperar al menos 7 días de operación continua.\n")

    print("[2/4] Calculando features...")
    feat_df = build_features(fr_df)

    if len(feat_df) < 50:
        print(f"\nADVERTENCIA: Solo {len(feat_df)} filas de features.")
        print("Se necesitan más datos históricos para análisis robustos.\n")
        if len(feat_df) == 0:
            print("Generando reporte vacío.")
            write_report(["_Sin datos suficientes para ningún análisis._"], feat_df)
            sys.exit(0)

    print("[3/4] Ejecutando análisis...")
    sections = [
        analysis_1_score_vs_future(feat_df),
        analysis_2_component_correlations(feat_df),
        analysis_3_decay_curves(feat_df),
        analysis_4_mean_reversion(feat_df),
        analysis_5_acceleration_value(feat_df),
    ]

    print("[4/4] Escribiendo reporte...")
    report_path = write_report(sections, feat_df)

    print(f"\nListo! Abre: {report_path}\n")


if __name__ == "__main__":
    main()
