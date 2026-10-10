#!/usr/bin/env python3
"""ML health — ¿el modelo de scoring VIVO sigue funcionando? (local, read-only).

Evalúa el modelo desplegado (`models/scoring_model.joblib`) sobre datos
POSTERIORES a su ventana de entrenamiento: fuera de muestra real, sin depender
de `score_snapshots` (que se recorta a 30 filas por par y solo conserva
predicciones ≥14d de pares que dejaron de aparecer → sesgo de supervivencia).

Mide, sobre las mismas filas:
  - rank-IC modelo vs heurístico v11.0 (+ IC bootstrap por bloques diarios)
  - net_apr del top-decil / top-1% de cada ranking (significancia económica)
  - lo anterior con el label actual (salida "oráculo": corta ANTES del primer
    pago ≤0) y con un label REALISTA (sale DESPUÉS de observar el pago ≤0 y lo paga)
  - IC por semana (¿se degrada con la edad del modelo?)
  - drift de features (PSI train-reciente vs OOS)
  - calibración del score 0–100 fuera de muestra
  - validación vía score_snapshots (la de ml_train) + su sesgo de supervivencia
  - PnL realizado de posiciones cerradas (user_history)

NO escribe en la DB ni toca producción. Output: reports/ml_health_YYYYMMDD.md

Uso:
    python scripts/ml_health.py --refresh-cache
    python scripts/ml_health.py                 # usa cache/fr_snapshots.csv
"""
import argparse
import bisect
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

import numpy as np
import pandas as pd
import joblib

import scoring_optimizer as opt
from ml_diagnostic import TARGET, md_table, reconstruct_v11_params, spearman
from ml_train import (CACHE_FR, FEATURE_BASE_COLS, build_feature_matrix,
                      validate_live_predictions)
from analysis.ml_features import FEATURE_NAMES
from _scoring_data import split_on_gaps, strip_gap_segments

REPORT_DIR = ROOT / "reports"
MODEL_PATH = ROOT / "models" / "scoring_model.joblib"
LABEL_HORIZON_DAYS = 14          # HOLD_MAX_HOURS del label (336h)
WARMUP_DAYS = 12                 # 30 snapshots × 8h ≈ 10d de ventana completa
PSI_REF_DAYS = 21                # referencia = últimos 21d del train
N_BOOT = 200
MIN_WEEK_ROWS = 10_000


# ── Label realista ────────────────────────────────────────────────────────────

def realistic_labels(fr: pd.DataFrame, feat: pd.DataFrame) -> pd.Series:
    """net_apr si el bot sale DESPUÉS de ver el primer pago ≤0 (y lo paga).

    El label de entrenamiento (scoring_optimizer.extract_features) corta justo
    ANTES del primer pago ≤0 — información que en vivo no existe. Aquí el hold
    incluye ese pago (negativo o cero) y su intervalo. Mismo fee, cap y piso.
    """
    series = {}
    for (sym, ex), g in fr.groupby(["symbol", "exchange"]):
        g = g.sort_values("captured_at")
        ih = max(float(g["interval_hours"].fillna(8).median()), 1.0)
        series[(sym, ex)] = (g["captured_at"].values, g["rate"].to_numpy(float), ih)

    out = np.full(len(feat), np.nan)
    cap_h = opt.HOLD_MAX_HOURS
    for k, r in enumerate(feat[["symbol", "exchange", "captured_at"]].itertuples(index=False)):
        s = series.get((r.symbol, r.exchange))
        if s is None:
            continue
        times, rates, ih = s
        i = int(np.searchsorted(times, np.datetime64(r.captured_at.tz_convert(None))))
        if i >= len(times):
            continue
        look = min(len(rates) - i - 1, round(cap_h / ih))
        if look <= 0:
            continue
        cumul, periods = 0.0, 0
        for j in range(1, look + 1):
            cumul += rates[i + j]
            periods = j
            if rates[i + j] <= 0:
                break
        hold_days = periods * ih / 24.0
        net = (cumul - opt.ROUND_TRIP_FEE) / max(hold_days, ih / 24.0) * 365 * 100
        out[k] = max(opt.NET_APR_FLOOR, min(net, opt.NET_APR_CAP))
    return pd.Series(out, index=feat.index)


# ── Métricas ──────────────────────────────────────────────────────────────────

def top_mean(y: pd.Series, key: np.ndarray, frac: float) -> float:
    s = pd.Series(key, index=y.index)
    sel = s.rank(pct=True) >= (1 - frac)
    return round(float(y[sel].mean()), 1) if sel.sum() >= 5 else float("nan")


def block_bootstrap_ic(df: pd.DataFrame, ycol: str, rng) -> tuple:
    """IC 95% (modelo, heurístico, uplift) remuestreando DÍAS completos."""
    codes, uniq = pd.factorize(df["day"])          # evita comparar tz-aware vs naive
    days = np.arange(len(uniq))
    idx_by_day = {d: np.flatnonzero(codes == d) for d in days}
    m_ic, h_ic, up = [], [], []
    for _ in range(N_BOOT):
        pick = rng.choice(days, size=len(days), replace=True)
        ix = np.concatenate([idx_by_day[d] for d in pick])
        sub = df.iloc[ix].reset_index(drop=True)   # spearman alinea por índice
        a = spearman(sub["pred"], sub[ycol])
        b = spearman(sub["heur"], sub[ycol])
        m_ic.append(a); h_ic.append(b); up.append(a - b)
    q = lambda v: (round(float(np.nanpercentile(v, 2.5)), 3),
                   round(float(np.nanpercentile(v, 97.5)), 3))
    return q(m_ic), q(h_ic), q(up)


def window_metrics(df: pd.DataFrame, ycol: str, rng) -> dict:
    if len(df) < 500:
        return {}
    m_ic = spearman(df["pred"], df[ycol])
    h_ic = spearman(df["heur"], df[ycol])
    ci_m, ci_h, ci_u = block_bootstrap_ic(df, ycol, rng)
    y = df[ycol]
    return dict(
        n=len(df), days=df["day"].nunique(),
        m_ic=round(m_ic, 3), h_ic=round(h_ic, 3), uplift=round(m_ic - h_ic, 3),
        ci_m=ci_m, ci_h=ci_h, ci_u=ci_u,
        m_t10=top_mean(y, df["pred"].values, 0.10), h_t10=top_mean(y, df["heur"].values, 0.10),
        m_t1=top_mean(y, df["pred"].values, 0.01), h_t1=top_mean(y, df["heur"].values, 0.01),
        base=round(float(y.mean()), 1),
    )


def psi(ref: pd.Series, cur: pd.Series, bins: int = 10) -> float:
    ref, cur = ref.dropna(), cur.dropna()
    edges = np.unique(np.nanpercentile(ref, np.linspace(0, 100, bins + 1)))
    if len(edges) < 3:
        return float("nan")
    edges[0], edges[-1] = -np.inf, np.inf
    r = np.histogram(ref, edges)[0] / len(ref)
    c = np.histogram(cur, edges)[0] / len(cur)
    r, c = np.clip(r, 1e-4, None), np.clip(c, 1e-4, None)
    return round(float(np.sum((c - r) * np.log(c / r))), 3)


# ── DB read-only: supervivencia de score_snapshots + PnL realizado ───────────

def db_queries() -> dict:
    out = {}
    try:
        from _scoring_data import get_engine
        from sqlalchemy import text
        engine = get_engine()
        with engine.connect() as conn:
            out["snap_age"] = pd.read_sql(text("""
                SELECT mode, model_version,
                       COUNT(*) AS n,
                       COUNT(DISTINCT symbol || '@' || exchange) AS pairs,
                       MIN(captured_at) AS oldest, MAX(captured_at) AS newest,
                       SUM(CASE WHEN captured_at <= NOW() - INTERVAL '14 days'
                                THEN 1 ELSE 0 END) AS n_ge14d
                FROM score_snapshots WHERE model_prediction IS NOT NULL
                GROUP BY mode, model_version ORDER BY mode, model_version
            """), conn)
            out["hist"] = pd.read_sql(text("""
                SELECT mode, capital_used, hours, payment_count, earned, fees,
                       net_earned, reason, closed_at
                FROM user_history
                WHERE closed_at >= '2026-06-17'
            """), conn)
            out["open"] = pd.read_sql(text("""
                SELECT mode, capital_used, earned_real, entry_time, auto_executed
                FROM user_positions WHERE status = 'active'
            """), conn)
    except SystemExit:
        out["error"] = "DATABASE_URL no configurada"
    except Exception as e:
        out["error"] = f"{type(e).__name__}: {e}"
    return out


def pnl_table(hist: pd.DataFrame) -> list:
    rows = []
    hist = hist.copy()
    hist["cap_days"] = hist["capital_used"].clip(lower=0) * hist["hours"].clip(lower=0) / 24
    hist["month"] = pd.to_datetime(hist["closed_at"], utc=True).dt.strftime("%Y-%m")
    for (mode, month), g in hist.groupby(["mode", "month"]):
        cap_days = g["cap_days"].sum()
        apr = g["net_earned"].sum() / cap_days * 365 * 100 if cap_days > 0 else float("nan")
        rows.append([mode, month, len(g), round(g["net_earned"].sum(), 2),
                     round(g["fees"].sum(), 2), round(g["hours"].median() / 24, 1),
                     f"{100 * (g['net_earned'] > 0).mean():.0f}%", round(apr, 1)])
    return rows


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    ap = argparse.ArgumentParser(description="Salud del modelo ML vivo (read-only).")
    ap.add_argument("--refresh-cache", action="store_true",
                    help="re-descarga funding_rate_snapshots (90d) antes de evaluar")
    ap.add_argument("--trials-csv", default="reports/optimizer_20260617_trials.csv")
    ap.add_argument("--model", default=str(MODEL_PATH))
    args = ap.parse_args()
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    opt._load_dotenv()
    rng = np.random.default_rng(42)

    bundle = joblib.load(args.model)
    model, calib = bundle["model"], bundle["calibration_pcts"]
    version = bundle.get("model_version")
    train_end = pd.Timestamp(bundle["train_window"]["to"], tz="UTC") + pd.Timedelta(days=1)
    print(f"Modelo {version} — train hasta {train_end.date()} (excl.)")

    if args.refresh_cache:
        from _scoring_data import load_fr_snapshots
        print("[0] Refrescando cache desde la DB...")
        load_fr_snapshots(force_reload=True)
    if not CACHE_FR.exists():
        sys.exit(f"Falta {CACHE_FR}. Corre con --refresh-cache.")

    print("[1] Cargando cache (solo la ventana necesaria)...")
    fr = pd.read_csv(CACHE_FR)
    fr["captured_at"] = pd.to_datetime(fr["captured_at"], utc=True)
    data_end = fr["captured_at"].max()
    ref_start = train_end - pd.Timedelta(days=PSI_REF_DAYS)
    fr = fr[fr["captured_at"] >= ref_start - pd.Timedelta(days=WARMUP_DAYS)]
    print(f"  {len(fr):,} snapshots, datos hasta {data_end}")
    # Huecos de recolección (scanner inactivo: event-driven, sin posiciones abiertas no escribe): días OOS con <50% de la mediana.
    per_day = fr[fr["captured_at"] >= train_end].groupby(fr["captured_at"].dt.floor("D")).size()
    per_day = per_day.reindex(pd.date_range(per_day.index.min(), data_end.floor("D"),
                                            freq="D"), fill_value=0)
    gaps = [str(d.date()) for d, c in per_day.items() if c < 0.5 * per_day.median()]
    # Series cortadas en huecos (exchange queda con sufijo de tramo hasta el
    # final; realistic_labels usa el mismo `fr`, así que las claves coinciden).
    fr = split_on_gaps(fr)

    print("[2] Extrayendo features + label (builder compartido con prod)...")
    feat = opt.extract_features(fr).dropna(subset=FEATURE_BASE_COLS + [TARGET])
    feat["captured_at"] = pd.to_datetime(feat["captured_at"], utc=True)
    feat = feat[feat["captured_at"] >= ref_start].sort_values("captured_at").reset_index(drop=True)
    fmat = build_feature_matrix(feat)

    ref_mask = feat["captured_at"] < train_end
    oos = feat[~ref_mask].copy()
    if oos.empty:
        sys.exit("Sin filas posteriores al train: refresca el cache (--refresh-cache).")
    print(f"  ref {int(ref_mask.sum()):,} filas | OOS {len(oos):,} filas")

    print("[3] Prediciendo modelo + heurístico v11.0 + label realista sobre OOS...")
    oos["pred"] = model.predict(fmat.loc[oos.index].values)
    v11p = reconstruct_v11_params(ROOT / args.trials_csv)
    oos["heur"] = oos.apply(lambda r: opt.parametric_score_candidate(r, v11p), axis=1)
    oos["net_apr_real"] = realistic_labels(fr, oos)
    oos["day"] = oos["captured_at"].dt.floor("D")
    oos["score"] = [max(0, min(bisect.bisect_right(calib, p), 100)) for p in oos["pred"]]

    full_cut = data_end - pd.Timedelta(days=LABEL_HORIZON_DAYS)
    purged_start = train_end + pd.Timedelta(days=LABEL_HORIZON_DAYS)
    windows = {
        f"A · OOS label completo ({train_end.date()}→{full_cut.date()})":
            oos[oos["captured_at"] <= full_cut],
        f"B · OOS purgado ≥{purged_start.date()} (label ≥7d)":
            oos[oos["captured_at"] >= purged_start],
        "C · OOS completo (label ≥7d)": oos,
    }

    print("[4] Métricas por ventana (bootstrap por días)...")
    wrows = []
    res = {}
    for wname, wdf in windows.items():
        for ycol, lname in ((TARGET, "oráculo"), ("net_apr_real", "realista")):
            m = window_metrics(wdf.dropna(subset=[ycol]), ycol, rng)
            res[(wname, lname)] = m
            if not m:
                wrows.append([wname, lname, len(wdf)] + ["—"] * 8)
                continue
            wrows.append([wname, lname, m["n"], m["m_ic"], m["h_ic"],
                          f"{m['uplift']:+.3f}", f"[{m['ci_u'][0]:+.3f}, {m['ci_u'][1]:+.3f}]",
                          m["base"], f"{m['m_t10']} / {m['h_t10']}",
                          f"{m['m_t1']} / {m['h_t1']}",
                          f"{m['m_t10'] - m['h_t10']:+.1f}"])

    # Semanas con label ≥7d completo y volumen suficiente (las colas cortas o
    # truncadas por el final de los datos no sirven para medir degradación).
    weekly = []
    wk_src = oos[oos["captured_at"] <= data_end - pd.Timedelta(days=7)]
    for wk, g in wk_src.groupby(wk_src["captured_at"].dt.to_period("W-SUN")):
        if len(g) < MIN_WEEK_ROWS:
            continue
        weekly.append([str(wk), len(g),
                       round(spearman(g["pred"], g[TARGET]), 3),
                       round(spearman(g["heur"], g[TARGET]), 3),
                       round(spearman(g["pred"], g["net_apr_real"]), 3),
                       round(spearman(g["heur"], g["net_apr_real"]), 3),
                       top_mean(g[TARGET], g["pred"].values, 0.10),
                       top_mean(g["net_apr_real"], g["pred"].values, 0.10)])

    print("[5] Drift (PSI) + calibración...")
    ref_f = fmat.loc[ref_mask]
    oos_f = fmat.loc[oos.index]
    drift = sorted(([f, psi(ref_f[f], oos_f[f])] for f in FEATURE_NAMES),
                   key=lambda x: -(x[1] if x[1] == x[1] else -1))
    sc = oos["score"]
    calib_row = [len(sc), f"{100 * (sc >= 70).mean():.1f}%", f"{100 * (sc >= 85).mean():.1f}%",
                 int(sc.median())]

    print("[6] DB read-only: score_snapshots + user_history...")
    live = validate_live_predictions(strip_gap_segments(feat.copy()))
    db = db_queries()

    # ── Veredicto ─────────────────────────────────────────────────────────
    a_or = res.get((list(windows)[0], "oráculo"), {})
    a_re = res.get((list(windows)[0], "realista"), {})
    b_re = res.get((list(windows)[1], "realista"), {})
    # El veredicto usa la ventana PURGADA (B): sin solape con los labels del train.
    flags = []
    works = bool(b_re) and b_re["ci_u"][0] > 0 and b_re["m_t10"] > b_re["h_t10"]
    flags.append(("Modelo bate al heurístico OOS purgado (label realista, CI uplift>0 y Δtop10>0)", works))
    if a_re:
        flags.append(("También en la ventana A (label completo)", a_re["uplift"] > 0))
    flags.append((f"Cobertura de datos sin huecos en OOS ({len(gaps)} días con <50% de snapshots)",
                  not gaps))
    if len(weekly) >= 2:
        flags.append(("Sin degradación semanal fuerte (IC última ≥ primera − 0.10)",
                      weekly[-1][2] >= weekly[0][2] - 0.10))
    max_psi = max((d[1] for d in drift if d[1] == d[1]), default=0)
    flags.append((f"Drift de features moderado (PSI máx {max_psi} < 0.25)", max_psi < 0.25))
    flags.append((f"Calibración OOS sana (% score≥70 = {calib_row[1]}, esperado ≈30%)",
                  (sc >= 70).mean() < 0.45))
    if a_or and a_re:
        flags.append(("Ranking robusto al label (IC realista ≥ 0.8 × IC oráculo)",
                      a_re["m_ic"] >= 0.8 * a_or["m_ic"]))

    if not works:
        verdict = "CAMBIAR — el modelo no bate al heurístico fuera de muestra con label realista"
    elif all(ok for _, ok in flags):
        verdict = "MANTENER + RE-ENTRENAR en el ciclo normal (funciona; el re-entreno está vencido)"
    else:
        verdict = "MANTENER con correcciones — funciona pero hay alertas (ver checklist)"

    # ── Reporte ───────────────────────────────────────────────────────────
    REPORT_DIR.mkdir(exist_ok=True)
    today = datetime.now().strftime("%Y%m%d")
    path = REPORT_DIR / f"ml_health_{today}.md"
    L = [f"# ML health — modelo {version} — {datetime.now():%Y-%m-%d %H:%M}", "",
         f"**Veredicto: {verdict}**", "",
         f"Datos hasta {data_end:%Y-%m-%d %H:%M} UTC. Train del modelo hasta "
         f"{bundle['train_window']['to']}. OOS = filas con `captured_at` ≥ {train_end.date()} "
         f"({len(oos):,} filas, {oos['day'].nunique()} días). Heurístico = v11.0 paramétrico "
         f"(`{args.trials_csv}`).", "",
         f"Días OOS con <50% de snapshots (scanner inactivo: event-driven, sin posiciones abiertas no escribe): {', '.join(gaps) or 'ninguno'}. "
         "Las series se cortan en los huecos (`split_on_gaps`); aun así "
         "el veredicto usa la ventana B.", "",
         "## Checklist", ""]
    L += [f"- [{'x' if ok else ' '}] {txt}" for txt, ok in flags]
    L += ["", "## 1. Modelo vs heurístico fuera de muestra", "",
          "Label *oráculo* = el de entrenamiento (sale antes del primer pago ≤0). "
          "Label *realista* = sale después de observarlo y lo paga. IC = Spearman; "
          "CI 95% por bootstrap de días completos. Top10/Top1 = net_apr medio "
          "(modelo / heurístico); base = media del universo.", "",
          md_table(["ventana", "label", "n", "IC modelo", "IC heur", "uplift", "CI uplift",
                    "base", "top10 m/h", "top1% m/h", "Δtop10"], wrows), "",
          "## 2. IC por semana (edad del modelo)", "",
          md_table(["semana", "n", "IC m (or)", "IC h (or)", "IC m (real)", "IC h (real)",
                    "top10 m (or)", "top10 m (real)"], weekly), "",
          "## 3. Drift de features (PSI ref = últimos "
          f"{PSI_REF_DAYS}d de train vs OOS)", "",
          "PSI <0.10 estable · 0.10–0.25 moderado · >0.25 alto.", "",
          md_table(["feature", "PSI"], drift), "",
          "## 4. Calibración del score 0–100 en OOS", "",
          md_table(["n", "% ≥70", "% ≥85", "mediana"], [calib_row]), "",
          "## 5. Validación vía score_snapshots (método de ml_train)", "",
          f"status={live['status']} · n spot_perp={live['n']} · IC={live['ic']} · "
          f"by_mode={live.get('by_mode')}", ""]
    if "snap_age" in db:
        sa = db["snap_age"]
        L += ["Supervivencia: `score_snapshots` se recorta a 30 filas por symbol+exchange, "
              "así que solo sobreviven ≥14d las predicciones de pares que dejaron de "
              "aparecer en el scan. Este IC NO representa al universo rankeado.", "",
              md_table(list(sa.columns), sa.astype(str).values.tolist()), ""]
    L += ["## 6. PnL realizado (user_history desde 2026-06-17, todos los usuarios)", ""]
    if "hist" in db and not db["hist"].empty:
        L += [md_table(["mode", "mes", "n", "net USD", "fees USD", "hold med (d)",
                        "% ganadoras", "APR realizado %"], pnl_table(db["hist"])), ""]
        op = db.get("open")
        if op is not None and not op.empty:
            L += [f"Posiciones activas: {len(op)} (capital {op['capital_used'].sum():,.0f}, "
                  f"earned {op['earned_real'].sum():,.2f}, auto {int(op['auto_executed'].fillna(False).sum())}).", ""]
        L += ["Nota: `user_history` no guarda el score/predicción de entrada, así que el "
              "PnL no se puede atribuir al modelo vs heurístico.", ""]
    else:
        L += [f"Sin datos ({db.get('error', 'vacío')}).", ""]
    path.write_text("\n".join(L), encoding="utf-8")
    print(f"\nVeredicto: {verdict}\nReporte: {path}")


if __name__ == "__main__":
    main()
