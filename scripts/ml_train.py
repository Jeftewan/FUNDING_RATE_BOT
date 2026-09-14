#!/usr/bin/env python3
"""ML train — entrena/valida/exporta el modelo de scoring para PRODUCCIÓN.

OPERACIÓN LOCAL Y MANUAL. El usuario lo corre cada ~15 días: valida el modelo
que está vivo en prod contra resultados reales, entrena uno nuevo sobre 90d de
funding_rate_snapshots, confirma walk-forward que sigue batiendo al heurístico
v11.0, calibra el score y exporta `models/scoring_model.joblib`. NO despliega:
imprime las instrucciones de git para promover el modelo.

Paridad: las features se construyen con analysis/ml_features.build_feature_vector
— EXACTAMENTE el mismo builder que usa prod (analysis/ml_scorer). Garantiza que
el modelo vea offline lo mismo que verá online.

Reusa: scripts/scoring_optimizer.extract_features (features+label net_apr),
scripts/ml_diagnostic (helpers), el cache de fr_snapshots.

Requiere: pip install -r requirements-dev.txt   (scikit-learn pinneado == prod)

Uso:
    python scripts/ml_train.py
    python scripts/ml_train.py --trials-csv reports/optimizer_20260617_trials.csv
    python scripts/ml_train.py --no-validate-live   # salta validación de prod

Output:
    models/scoring_model.joblib        — artefacto para commitear/desplegar
    reports/ml_train_YYYYMMDD.md       — validación live + walk-forward + veredicto
"""
import argparse
import statistics
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

import numpy as np
import pandas as pd
import joblib
from sklearn.ensemble import HistGradientBoostingRegressor

import scoring_optimizer as opt
from ml_diagnostic import TARGET, md_table, reconstruct_v11_params, spearman
from ml_stability import top_stats   # net_apr medio + %rent del top por ranking
from analysis.ml_features import build_feature_vector, FEATURE_NAMES

REPORT_DIR = ROOT / "reports"
MODELS_DIR = ROOT / "models"
CACHE_FR = ROOT / "cache" / "fr_snapshots.csv"

TRAIN_SAMPLE = None             # HistGB entrena con TODAS las filas (el GBR usaba 200k)
LIVE_VALIDATION_MIN_AGE_DAYS = 14   # antigüedad mínima de una predicción para validarla
# HistGB elegido en reports/ml_experiments_20260913.md: bate al GBR 300x depth3
# (uplift IC +0.126 vs +0.112, Δtop10 +4.9 vs +4.1, 6/6 folds) y entrena ~7x más rápido.
MODEL_PARAMS = dict(max_iter=300, learning_rate=0.05, max_leaf_nodes=31,
                    min_samples_leaf=200, l2_regularization=0.0,
                    early_stopping=False, random_state=42)
# Calibración sobre las predicciones de los últimos N días: con todo el train los
# scores fuera de muestra salían inflados (mediana ~74, 55% ≥70); con 14d ~34% ≥70.
CALIB_WINDOW_DAYS = 14


# ── Feature matrix vía el builder COMPARTIDO (paridad con prod) ──────────────

def _row_to_vec(r) -> list:
    """Mapea una fila de extract_features al vector de build_feature_vector.

    Reconstruye los dicts (params + indicators ricos) tal como los arma prod,
    para que el vector pase por el MISMO código que analysis/ml_scorer.
    """
    params = {
        "cv": r.cv, "min_ratio": r.min_ratio, "streak": r.streak,
        "pct": r.pct, "volume": r.volume, "settlement_avg": r.settlement_avg,
        "payments_per_day": r.ppd, "current_rate": r.current_rate,
    }
    indicators = {
        "z_score": {"z": r.z_value},
        "momentum": {"points": r.mom_points},
        "percentile": {"percentile": r.pctl_percentile, "points": r.pctl_points},
    }
    return build_feature_vector(params, indicators)


def build_feature_matrix(df: pd.DataFrame) -> pd.DataFrame:
    """DataFrame de features (columnas = FEATURE_NAMES) para todo `df`."""
    vecs = [_row_to_vec(r) for r in df.itertuples(index=False)]
    return pd.DataFrame(vecs, columns=FEATURE_NAMES, index=df.index)


# ── Paso 3: validar el modelo VIVO contra predicciones previas ───────────────

def validate_live_predictions(feat: pd.DataFrame) -> dict:
    """Lee score_snapshots con model_prediction ≥14d atrás y compara la
    predicción logueada contra el net_apr REAL realizado (recomputado desde los
    features que ya extrajimos para esos symbol/exchange/captured_at).

    Devuelve {status, n, ic, version, by_mode}. status='no_data' si no hay
    predicciones previas suficientes (caso normal en los primeros 14 días).

    El IC principal es SOLO spot_perp: el label offline es el net_apr de una
    tasa única, que para cross_exchange no aplica (score_snapshots guarda ahí la
    pierna short). Cross se reporta aparte en by_mode como referencia.
    """
    out = {"status": "no_data", "n": 0, "ic": float("nan"), "version": None,
           "by_mode": {}}
    try:
        from _scoring_data import get_engine
        from sqlalchemy import text
        engine = get_engine()
        cutoff = datetime.now(timezone.utc) - timedelta(days=LIVE_VALIDATION_MIN_AGE_DAYS)
        with engine.connect() as conn:
            preds = pd.read_sql(
                text("""
                    SELECT symbol, exchange, mode, model_prediction, model_version,
                           captured_at
                    FROM   score_snapshots
                    WHERE  model_prediction IS NOT NULL
                      AND  captured_at <= :cutoff
                    ORDER  BY captured_at
                """),
                conn, params={"cutoff": cutoff},
            )
    except Exception as e:
        out["status"] = f"unavailable ({type(e).__name__})"
        return out

    if preds.empty:
        return out

    # Empareja cada predicción con el net_apr real de la fila de features más
    # cercana (mismo symbol/exchange, captured_at dentro de 6h).
    preds["captured_at"] = pd.to_datetime(preds["captured_at"], utc=True)
    preds["mode"] = preds["mode"].fillna("spot_perp")
    right = feat[["symbol", "exchange", "captured_at", TARGET]].copy()
    right["captured_at"] = pd.to_datetime(right["captured_at"], utc=True)
    matched = pd.merge_asof(
        preds.sort_values("captured_at"), right.sort_values("captured_at"),
        on="captured_at", by=["symbol", "exchange"], direction="nearest",
        tolerance=pd.Timedelta(hours=6),
    ).dropna(subset=[TARGET, "model_prediction"])

    for mode, g in matched.groupby("mode"):
        ic = spearman(g["model_prediction"], g[TARGET]) if len(g) >= 30 else float("nan")
        out["by_mode"][mode] = {"n": int(len(g)), "ic": round(float(ic), 3)}

    spot = matched[matched["mode"] == "spot_perp"]
    out["n"] = int(len(spot))
    if len(spot) < 30:
        out["status"] = "insufficient"
        return out

    out["status"] = "ok"
    out["ic"] = round(float(spearman(spot["model_prediction"], spot[TARGET])), 3)
    out["version"] = preds["model_version"].dropna().iloc[-1] if preds["model_version"].notna().any() else None
    return out


# ── Paso 5: walk-forward (modelo nuevo vs heurístico v11.0) ──────────────────

def make_default_model():
    """Estimador de producción (lo reusa scripts/ml_experiments.py como control)."""
    return HistGradientBoostingRegressor(**MODEL_PARAMS)


def recency_weights(ts: pd.Series, ref, half_life_days: float) -> np.ndarray:
    """sample_weight exponencial: una fila de `half_life_days` antes de `ref` pesa 0.5."""
    age_days = ((ref - ts).dt.total_seconds() / 86400).clip(lower=0)
    return np.power(0.5, age_days / half_life_days).values


def walk_forward(feat: pd.DataFrame, fmat: pd.DataFrame, heur_scores: pd.Series,
                 min_train_days: int, test_days: int, make_model=make_default_model,
                 train_sample=TRAIN_SAMPLE, half_life_days=None) -> list:
    """train_sample=None entrena con todo el train; half_life_days activa el
    peso por recencia (relativo al inicio del fold de test)."""
    folds = []
    dmin, dmax = feat["captured_at"].min(), feat["captured_at"].max()
    ws = dmin + timedelta(days=min_train_days)
    while ws + timedelta(days=test_days) <= dmax + timedelta(days=1):
        we = ws + timedelta(days=test_days)
        tr_mask = feat["captured_at"] < ws
        te_mask = (feat["captured_at"] >= ws) & (feat["captured_at"] < we)
        n_tr, n_te = int(tr_mask.sum()), int(te_mask.sum())
        if n_tr < 5000 or n_te < 2000:
            ws = we
            continue
        n_fit = n_tr if train_sample is None else min(train_sample, n_tr)
        tr_idx = feat[tr_mask].sample(n=n_fit, random_state=42).index
        model = make_model()
        sw = (recency_weights(feat.loc[tr_idx, "captured_at"], ws, half_life_days)
              if half_life_days else None)
        # Fit/predict sobre .values (sin nombres de columna): el modelo rankea por
        # POSICIÓN, igual que el vector-lista que le pasa prod (analysis/ml_scorer).
        t0 = time.time()
        model.fit(fmat.loc[tr_idx].values, feat.loc[tr_idx, TARGET], sample_weight=sw)
        fit_secs = round(time.time() - t0, 1)
        pred = model.predict(fmat.loc[te_mask].values)
        test_df = feat.loc[te_mask]
        heur_te = heur_scores[te_mask].values
        h_ic = spearman(pd.Series(heur_te, index=test_df.index), test_df[TARGET])
        m_ic = spearman(pd.Series(pred, index=test_df.index), test_df[TARGET])
        # Significancia ECONÓMICA: net_apr medio del top por ranking (no solo el
        # IC estadístico). Cuánta plata gana ordenar por modelo vs por heurístico.
        h_d10, _ = top_stats(test_df, heur_te, 0.10)
        m_d10, m_d10p = top_stats(test_df, pred, 0.10)
        h_t1, _ = top_stats(test_df, heur_te, 0.01)
        m_t1, _ = top_stats(test_df, pred, 0.01)
        folds.append(dict(win=f"{ws.date()}→{we.date()}", n_te=n_te,
                          h_ic=round(h_ic, 3), m_ic=round(m_ic, 3),
                          uplift=round(m_ic - h_ic, 3),
                          h_d10=h_d10, m_d10=m_d10, d10_lift=round(m_d10 - h_d10, 1),
                          h_t1=h_t1, m_t1=m_t1, t1_lift=round(m_t1 - h_t1, 1),
                          m_d10p=m_d10p, fit_secs=fit_secs))
        print(f"  {folds[-1]['win']}: IC heur={h_ic:.3f} ml={m_ic:.3f} "
              f"uplift={m_ic - h_ic:+.3f} | net_apr top10 heur={h_d10} ml={m_d10} "
              f"(Δ{m_d10 - h_d10:+.1f})")
        ws = we
    return folds


def main():
    ap = argparse.ArgumentParser(description="Entrena/valida/exporta el modelo ML de scoring (local).")
    ap.add_argument("--trials-csv", default="reports/optimizer_20260617_trials.csv",
                    help="trials del optimizer para reconstruir el heurístico v11.0")
    ap.add_argument("--min-train-days", type=int, default=45)
    ap.add_argument("--test-days", type=int, default=7)
    ap.add_argument("--no-validate-live", action="store_true",
                    help="salta la validación del modelo en producción")
    ap.add_argument("--refresh-cache", action="store_true",
                    help="re-descarga funding_rate_snapshots (90d) antes de entrenar")
    args = ap.parse_args()
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass

    opt._load_dotenv()  # DATABASE_URL para la validación live

    print("\n" + "=" * 60)
    print("  ML TRAIN (local) — entrena/valida/exporta el modelo de scoring")
    print("=" * 60 + "\n")

    if args.refresh_cache:
        from _scoring_data import load_fr_snapshots
        print("[0/6] Refrescando cache desde la DB...")
        load_fr_snapshots(force_reload=True)

    print("[1/6] Cargando cache + extrayendo features...")
    if not CACHE_FR.exists():
        sys.exit(f"Falta {CACHE_FR}. Corre el optimizer una vez para generar el cache.")
    fr = pd.read_csv(CACHE_FR, parse_dates=["captured_at"])
    feat = opt.extract_features(fr).dropna(subset=FEATURE_BASE_COLS + [TARGET])
    feat = feat.sort_values("captured_at").reset_index(drop=True)
    print(f"  {len(feat):,} filas")

    print("[2/6] Construyendo matriz de features (builder compartido con prod)...")
    fmat = build_feature_matrix(feat)

    print("[3/6] Validando el modelo VIVO contra predicciones previas...")
    if args.no_validate_live:
        live = {"status": "skipped", "n": 0, "ic": float("nan"), "version": None}
    else:
        live = validate_live_predictions(feat)
    print(f"  live validation: {live['status']} (n={live['n']}, IC={live['ic']})")

    print("[4/6] Scoreando heurístico v11.0 (referencia walk-forward)...")
    v11p = reconstruct_v11_params(Path(args.trials_csv))
    heur_scores = feat.apply(lambda r: opt.parametric_score_candidate(r, v11p), axis=1)

    print("[5/6] Walk-forward (modelo nuevo vs heurístico)...")
    folds = walk_forward(feat, fmat, heur_scores, args.min_train_days, args.test_days)
    if not folds:
        sys.exit("Sin folds válidos para walk-forward. Ajustá --min-train-days/--test-days.")
    ups = [f["uplift"] for f in folds]
    mean_up = statistics.mean(ups)
    sd_up = statistics.pstdev(ups) if len(ups) > 1 else 0.0
    all_pos = all(u > 0 for u in ups)

    def _nanmean(xs):
        xs = [x for x in xs if x == x]  # descarta NaN (folds con top <5 muestras)
        return round(statistics.mean(xs), 1) if xs else float("nan")

    mean_d10_lift = _nanmean([f["d10_lift"] for f in folds])
    mean_t1_lift = _nanmean([f["t1_lift"] for f in folds])
    # PROMOVER exige robustez estadística (uplift IC) Y económica (el net_apr del
    # top sube, no solo la correlación). Un IC mejor que no eleva la ganancia del
    # top no justifica desplegar.
    econ_ok = (mean_d10_lift == mean_d10_lift and mean_d10_lift > 0)
    stable = all_pos and mean_up >= 0.05 and sd_up <= 0.05 and econ_ok

    print("[6/6] Entrenando modelo FINAL + calibrando + exportando...")
    fit_idx = (feat.index if TRAIN_SAMPLE is None
               else feat.sample(n=min(TRAIN_SAMPLE, len(feat)), random_state=42).index)
    model = make_default_model()
    model.fit(fmat.loc[fit_idx].values, feat.loc[fit_idx, TARGET])

    # Calibración: percentiles p0..p100 de las predicciones de los últimos
    # CALIB_WINDOW_DAYS → mapear cualquier predicción a un score 0–100 relativo
    # al régimen reciente (el que más se parece a lo que verá prod).
    train_preds = model.predict(fmat.loc[fit_idx].values)
    calib_from = feat["captured_at"].max() - timedelta(days=CALIB_WINDOW_DAYS)
    recent = (feat.loc[fit_idx, "captured_at"] >= calib_from).values
    calibration_pcts = [float(v) for v in np.percentile(train_preds[recent], np.arange(0, 101))]

    today = datetime.now().strftime("%Y%m%d")
    bundle = {
        "model": model,
        "calibration_pcts": calibration_pcts,
        "feature_names": FEATURE_NAMES,
        "model_version": today,
        "train_window": {
            "from": str(feat["captured_at"].min().date()),
            "to": str(feat["captured_at"].max().date()),
            "n_rows": int(len(feat)),
            "n_fit": int(len(fit_idx)),
            "calib_window_days": CALIB_WINDOW_DAYS,
            "n_calib": int(recent.sum()),
        },
        "val_metrics": {
            "wf_folds": len(folds),
            "wf_uplift_mean": round(mean_up, 3),
            "wf_uplift_sd": round(sd_up, 3),
            "wf_all_positive": all_pos,
            "wf_net_apr_top10_lift_mean": mean_d10_lift,
            "wf_net_apr_top1pct_lift_mean": mean_t1_lift,
        },
    }
    MODELS_DIR.mkdir(exist_ok=True)
    model_path = MODELS_DIR / "scoring_model.joblib"
    joblib.dump(bundle, model_path)
    size_mb = model_path.stat().st_size / 1e6
    print(f"  Modelo exportado: {model_path} ({size_mb:.2f} MB)")

    # ── Reporte ──
    import sklearn
    # HistGB no expone feature_importances_ → importancia por permutación (ΔR²).
    from sklearn.inspection import permutation_importance
    pi_idx = feat.sample(n=min(50_000, len(feat)), random_state=0).index
    pi = permutation_importance(model, fmat.loc[pi_idx].values, feat.loc[pi_idx, TARGET],
                                n_repeats=3, random_state=0)
    imp = sorted(zip(FEATURE_NAMES, pi.importances_mean), key=lambda x: -x[1])
    if stable:
        verdict = ("PROMOVER — el modelo bate al heurístico de forma estable (IC) Y "
                   "eleva el net_apr del top (económico); commit + push.")
    elif not all_pos:
        verdict = "NO PROMOVER — uplift IC inconsistente entre folds; investigar drift."
    elif not econ_ok:
        verdict = ("REVISAR — el IC mejora pero el net_apr del top NO sube "
                   f"(Δtop-decil {mean_d10_lift}); el ranking gana correlación sin "
                   "traducirse en ganancia. No desplegar sin entender por qué.")
    else:
        verdict = "REVISAR — uplift positivo pero marginal/ruidoso; decidir según el detalle."
    live_line = {
        "ok": f"IC en vivo spot_perp {live['ic']} sobre {live['n']} predicciones (modelo {live['version']}). "
              "Compará contra el uplift esperado; una caída fuerte = drift.",
        "no_data": "Sin predicciones previas de ≥14d (normal en el primer ciclo o tras un reset).",
        "insufficient": f"Solo {live['n']} predicciones emparejadas (<30) — aún no concluyente.",
        "skipped": "Saltada (--no-validate-live).",
    }.get(live["status"], f"No disponible: {live['status']}.")
    if live.get("by_mode"):
        live_line += "\n\n" + md_table(
            ["mode", "n emparejadas", "IC"],
            [[m, v["n"], v["ic"]] for m, v in sorted(live["by_mode"].items())])
        live_line += ("\n\n> cross_exchange es solo referencia: su label offline es el "
                      "net_apr de la pierna short, no del diferencial.")

    out = f"""# ML train — modelo de scoring para producción — {datetime.now():%Y-%m-%d %H:%M}

**Artefacto:** `models/scoring_model.joblib` (version `{today}`, {size_mb:.2f} MB).
**Datos:** {len(feat):,} filas, {bundle['train_window']['from']} → {bundle['train_window']['to']}.
**Modelo:** {type(model).__name__}{MODEL_PARAMS}, fit sobre {len(fit_idx):,} filas. **Target:** `net_apr`.
**Calibración:** percentiles de las predicciones de los últimos {CALIB_WINDOW_DAYS}d ({int(recent.sum()):,} filas).
**scikit-learn:** {sklearn.__version__} (debe coincidir EXACTO con requirements.txt de prod).

> Local-only. Entrena + valida + exporta; NO despliega. Para promover:
> `git add models/scoring_model.joblib && git commit && git push` → Railway redeploya.

## 1. Validación del modelo VIVO (predicciones previas ↔ net_apr real)

{live_line}

## 2. Walk-forward — modelo nuevo vs heurístico v11.0 ({len(folds)} folds)

Por fold: rank-IC (robustez estadística) + net_apr medio del top-10% por ranking
(robustez ECONÓMICA — cuánta plata gana ordenar por modelo vs por heurístico).

{md_table(["Ventana test", "n_test", "IC heur", "IC ML", "Uplift",
           "net_apr top10 heur", "net_apr top10 ML", "Δtop10", "Δtop1%"],
          [[f["win"], f["n_te"], f["h_ic"], f["m_ic"], f"{f['uplift']:+.3f}",
            f["h_d10"], f["m_d10"], f"{f['d10_lift']:+.1f}", f"{f['t1_lift']:+.1f}"]
           for f in folds])}

{md_table(["Métrica", "Valor"],
          [["Uplift IC medio", f"{mean_up:+.3f}"],
           ["Uplift IC σ", f"{sd_up:.3f}"],
           ["Positivo en todos los folds", "Sí" if all_pos else "No"],
           ["Δ net_apr top-decil medio (ML − heur)", f"{mean_d10_lift:+.1f}"],
           ["Δ net_apr top-1% medio (ML − heur)", f"{mean_t1_lift:+.1f}"]])}

**net_apr está en % anualizado.** El Δtop es lo que subiría tu rendimiento si
tradeás las mejores oportunidades rankeadas por el modelo en vez de por el score.

Umbral PROMOVER: uplift IC>0 en todos los folds, medio ≥0.05, σ≤0.05, **y**
Δ net_apr top-decil medio > 0 (la mejora estadística se traduce en ganancia).

## 3. Feature importances (modelo final, permutación ΔR² sobre 50k filas)

{md_table(["feature", "importance"], [[f, f"{v:.3f}"] for f, v in imp])}

## 4. Veredicto

**{verdict}**

Recordatorio: el `.joblib` debe cargarse con la MISMA versión de scikit-learn
({sklearn.__version__}) que lo creó — está pinneada en requirements.txt y
requirements-dev.txt. Un mismatch rompe `joblib.load` en Railway.
"""
    REPORT_DIR.mkdir(exist_ok=True)
    report_path = REPORT_DIR / f"ml_train_{today}.md"
    report_path.write_text(out, encoding="utf-8")

    print(f"\n  Reporte: {report_path}")
    print(f"  Walk-forward: uplift IC medio={mean_up:+.3f} σ={sd_up:.3f} all_pos={all_pos}")
    print(f"  Significancia económica: Δnet_apr top-decil medio={mean_d10_lift:+.1f} "
          f"top-1%={mean_t1_lift:+.1f}")
    print(f"  -> {verdict}")
    if stable:
        print("\n  Para desplegar:")
        print("    git add models/scoring_model.joblib")
        print(f'    git commit -m "model(scoring): re-entrenar modelo ML {today}"')
        print("    git push   # Railway redeploya con el modelo nuevo\n")


# Columnas crudas que extract_features produce y que el builder necesita; se usa
# para el dropna (no incluye fee_drag/current_rate derivados internamente).
FEATURE_BASE_COLS = [
    "cv", "min_ratio", "streak", "pct", "volume", "settlement_avg", "ppd",
    "current_rate", "z_value", "mom_points", "pctl_percentile", "pctl_points",
]


if __name__ == "__main__":
    main()
