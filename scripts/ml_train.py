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
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

import numpy as np
import pandas as pd
import joblib
from sklearn.ensemble import GradientBoostingRegressor

import scoring_optimizer as opt
from ml_diagnostic import TARGET, md_table, reconstruct_v11_params, spearman
from analysis.ml_features import build_feature_vector, FEATURE_NAMES

REPORT_DIR = ROOT / "reports"
MODELS_DIR = ROOT / "models"
CACHE_FR = ROOT / "cache" / "fr_snapshots.csv"

TRAIN_SAMPLE = 200_000          # subsample para entrenar el GBR (perf)
LIVE_VALIDATION_MIN_AGE_DAYS = 14   # antigüedad mínima de una predicción para validarla
GBR_PARAMS = dict(n_estimators=300, max_depth=3, learning_rate=0.05,
                  subsample=0.7, random_state=42)


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

    Devuelve {status, n, ic, version, ...}. status='no_data' si no hay
    predicciones previas suficientes (caso normal en los primeros 14 días).
    """
    out = {"status": "no_data", "n": 0, "ic": float("nan"), "version": None}
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
    # cercana (mismo symbol/exchange, captured_at dentro de ±1 intervalo de scan).
    preds["captured_at"] = pd.to_datetime(preds["captured_at"], utc=True)
    feat = feat.copy()
    feat["captured_at"] = pd.to_datetime(feat["captured_at"], utc=True)

    matched = []
    for ex, grp in preds.groupby("exchange"):
        fsub = feat[feat["exchange"] == ex]
        if fsub.empty:
            continue
        for sym, sgrp in grp.groupby("symbol"):
            fpair = fsub[fsub["symbol"] == sym].sort_values("captured_at")
            if fpair.empty:
                continue
            ft = fpair["captured_at"].values
            for row in sgrp.itertuples(index=False):
                idx = np.searchsorted(ft, np.datetime64(row.captured_at))
                # tolerancia: la fila de features más cercana en el tiempo
                best = None
                for cand in (idx, idx - 1):
                    if 0 <= cand < len(fpair):
                        dt = abs((fpair.iloc[cand]["captured_at"] - row.captured_at).total_seconds())
                        if best is None or dt < best[0]:
                            best = (dt, cand)
                if best and best[0] <= 6 * 3600:   # dentro de 6h
                    matched.append((row.model_prediction,
                                    fpair.iloc[best[1]][TARGET]))

    if len(matched) < 30:
        out["status"] = "insufficient"
        out["n"] = len(matched)
        return out

    mp = pd.Series([m[0] for m in matched])
    real = pd.Series([m[1] for m in matched])
    out["status"] = "ok"
    out["n"] = len(matched)
    out["ic"] = round(float(spearman(mp, real)), 3)
    out["version"] = preds["model_version"].dropna().iloc[-1] if preds["model_version"].notna().any() else None
    return out


# ── Paso 5: walk-forward (modelo nuevo vs heurístico v11.0) ──────────────────

def walk_forward(feat: pd.DataFrame, fmat: pd.DataFrame, heur_scores: pd.Series,
                 min_train_days: int, test_days: int) -> list:
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
        tr_idx = feat[tr_mask].sample(n=min(TRAIN_SAMPLE, n_tr), random_state=42).index
        gbr = GradientBoostingRegressor(**GBR_PARAMS)
        # Fit/predict sobre .values (sin nombres de columna): el modelo rankea por
        # POSICIÓN, igual que el vector-lista que le pasa prod (analysis/ml_scorer).
        gbr.fit(fmat.loc[tr_idx].values, feat.loc[tr_idx, TARGET])
        pred = gbr.predict(fmat.loc[te_mask].values)
        h_ic = spearman(heur_scores[te_mask], feat.loc[te_mask, TARGET])
        m_ic = spearman(pd.Series(pred, index=feat[te_mask].index), feat.loc[te_mask, TARGET])
        folds.append(dict(win=f"{ws.date()}→{we.date()}", n_te=n_te,
                          h_ic=round(h_ic, 3), m_ic=round(m_ic, 3),
                          uplift=round(m_ic - h_ic, 3)))
        print(f"  {folds[-1]['win']}: IC heur={h_ic:.3f} ml={m_ic:.3f} "
              f"uplift={m_ic - h_ic:+.3f}")
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
    args = ap.parse_args()
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass

    opt._load_dotenv()  # DATABASE_URL para la validación live

    print("\n" + "=" * 60)
    print("  ML TRAIN (local) — entrena/valida/exporta el modelo de scoring")
    print("=" * 60 + "\n")

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
    stable = all_pos and mean_up >= 0.05 and sd_up <= 0.05

    print("[6/6] Entrenando modelo FINAL + calibrando + exportando...")
    fit_idx = feat.sample(n=min(TRAIN_SAMPLE, len(feat)), random_state=42).index
    model = GradientBoostingRegressor(**GBR_PARAMS)
    model.fit(fmat.loc[fit_idx].values, feat.loc[fit_idx, TARGET])

    # Calibración: percentiles p0..p100 de las predicciones de train → mapear
    # cualquier predicción a un score 0–100 estable e interpretable en prod.
    train_preds = model.predict(fmat.loc[fit_idx].values)
    calibration_pcts = [float(v) for v in np.percentile(train_preds, np.arange(0, 101))]

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
        },
        "val_metrics": {
            "wf_folds": len(folds),
            "wf_uplift_mean": round(mean_up, 3),
            "wf_uplift_sd": round(sd_up, 3),
            "wf_all_positive": all_pos,
        },
    }
    MODELS_DIR.mkdir(exist_ok=True)
    model_path = MODELS_DIR / "scoring_model.joblib"
    joblib.dump(bundle, model_path)
    size_mb = model_path.stat().st_size / 1e6
    print(f"  Modelo exportado: {model_path} ({size_mb:.2f} MB)")

    # ── Reporte ──
    import sklearn
    imp = sorted(zip(FEATURE_NAMES, model.feature_importances_), key=lambda x: -x[1])
    verdict = ("PROMOVER — el modelo bate al heurístico de forma estable; commit + push."
               if stable else
               ("NO PROMOVER — uplift inconsistente entre folds; investigar drift."
                if not all_pos else
                "REVISAR — uplift positivo pero marginal/ruidoso; decidir según el detalle."))
    live_line = {
        "ok": f"IC en vivo {live['ic']} sobre {live['n']} predicciones (modelo {live['version']}). "
              "Compará contra el uplift esperado; una caída fuerte = drift.",
        "no_data": "Sin predicciones previas de ≥14d (normal en el primer ciclo o tras un reset).",
        "insufficient": f"Solo {live['n']} predicciones emparejadas (<30) — aún no concluyente.",
        "skipped": "Saltada (--no-validate-live).",
    }.get(live["status"], f"No disponible: {live['status']}.")

    out = f"""# ML train — modelo de scoring para producción — {datetime.now():%Y-%m-%d %H:%M}

**Artefacto:** `models/scoring_model.joblib` (version `{today}`, {size_mb:.2f} MB).
**Datos:** {len(feat):,} filas, {bundle['train_window']['from']} → {bundle['train_window']['to']}.
**Modelo:** GradientBoostingRegressor{GBR_PARAMS}. **Target:** `net_apr`.
**scikit-learn:** {sklearn.__version__} (debe coincidir EXACTO con requirements.txt de prod).

> Local-only. Entrena + valida + exporta; NO despliega. Para promover:
> `git add models/scoring_model.joblib && git commit && git push` → Railway redeploya.

## 1. Validación del modelo VIVO (predicciones previas ↔ net_apr real)

{live_line}

## 2. Walk-forward — modelo nuevo vs heurístico v11.0 ({len(folds)} folds)

{md_table(["Ventana test", "n_test", "IC heur", "IC ML", "Uplift"],
          [[f["win"], f["n_te"], f["h_ic"], f["m_ic"], f"{f['uplift']:+.3f}"] for f in folds])}

{md_table(["Métrica", "Valor"],
          [["Uplift IC medio", f"{mean_up:+.3f}"],
           ["Uplift IC σ", f"{sd_up:.3f}"],
           ["Positivo en todos los folds", "Sí" if all_pos else "No"]])}

Umbral PROMOVER: uplift>0 en todos los folds, medio ≥0.05, σ≤0.05.

## 3. Feature importances (modelo final)

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
    print(f"  Walk-forward: uplift medio={mean_up:+.3f} σ={sd_up:.3f} all_pos={all_pos}")
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
