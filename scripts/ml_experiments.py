#!/usr/bin/env python3
"""ML experiments — compara variantes del modelo de scoring con el MISMO
walk-forward que usa ml_train.py (control = estimador de producción).

OPERACIÓN LOCAL. No exporta ni despliega nada: produce un reporte para decidir
qué adoptar en ml_train.py. Features FIJOS (FEATURE_NAMES) y solo estimadores de
scikit-learn → cualquier ganador es servible por analysis/ml_scorer sin cambios.

Features/heurístico se cachean en cache/ml_exp_features.pkl y los resultados se
acumulan en cache/ml_exp_results.json, así se pueden correr candidatos por partes.

Uso:
    python scripts/ml_experiments.py                    # todos los candidatos
    python scripts/ml_experiments.py --only A_gbr_control B1_hgb
    python scripts/ml_experiments.py --calib B1_hgb     # diagnóstico de calibración
    python scripts/ml_experiments.py --report-only

Output: reports/ml_experiments_YYYYMMDD.md
"""
import argparse
import io
import json
import statistics
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

import joblib
import numpy as np
import pandas as pd
from sklearn.ensemble import GradientBoostingRegressor, HistGradientBoostingRegressor

import scoring_optimizer as opt
from ml_diagnostic import TARGET, md_table, reconstruct_v11_params
from ml_train import (CACHE_FR, FEATURE_BASE_COLS, MODEL_PARAMS,
                      build_feature_matrix, recency_weights, walk_forward)
from analysis.ml_features import FEATURE_NAMES

FEAT_CACHE = ROOT / "cache" / "ml_exp_features.pkl"
RESULTS = ROOT / "cache" / "ml_exp_results.json"
REPORT_DIR = ROOT / "reports"

# GBR de prod hasta 2026-09 (control histórico del experimento del 20260913).
GBR_PARAMS = dict(n_estimators=300, max_depth=3, learning_rate=0.05,
                  subsample=0.7, random_state=42)
GBR_SAMPLE = 200_000


def _hgb(**kw):
    return lambda: HistGradientBoostingRegressor(**{**MODEL_PARAMS, **kw})


def _gbr(**kw):
    return lambda: GradientBoostingRegressor(**{**GBR_PARAMS, **kw})


# name → (factory, train_sample (None = todo el train), half_life_days, descripción)
CANDIDATES = {
    "A_gbr_control": (_gbr(), GBR_SAMPLE, None, "GBR prod (control)"),
    "A_gbr_hl30":    (_gbr(), GBR_SAMPLE, 30, "GBR prod + recencia 30d"),
    "C1_gbr_d4":     (_gbr(max_depth=4, n_estimators=600, learning_rate=0.03),
                      GBR_SAMPLE, None, "GBR depth4 600it lr0.03"),
    "C2_gbr_huber":  (_gbr(loss="huber"), GBR_SAMPLE, None, "GBR loss huber"),
    "B1_hgb":        (_hgb(), None, None, "HistGB 31 hojas, todo el train"),
    "B2_hgb_deep":   (_hgb(max_iter=600, max_leaf_nodes=63, min_samples_leaf=500,
                           l2_regularization=1.0), None, None, "HistGB 63 hojas 600it l2=1"),
    "B3_hgb_small":  (_hgb(learning_rate=0.1, max_leaf_nodes=15, min_samples_leaf=1000),
                      None, None, "HistGB 15 hojas lr0.1"),
    "B4_hgb_abs":    (_hgb(loss="absolute_error"), None, None, "HistGB loss absolute_error"),
    "B1_hgb_hl30":   (_hgb(), None, 30, "HistGB 31 hojas + recencia 30d"),
    "B2_hgb_deep_hl30": (_hgb(max_iter=600, max_leaf_nodes=63, min_samples_leaf=500,
                              l2_regularization=1.0), None, 30, "HistGB 63 hojas + recencia 30d"),
}


def load_data(trials_csv: Path, rebuild: bool):
    if FEAT_CACHE.exists() and not rebuild and FEAT_CACHE.stat().st_mtime >= CACHE_FR.stat().st_mtime:
        return pd.read_pickle(FEAT_CACHE)
    print("Extrayendo features (se cachea)...")
    fr = pd.read_csv(CACHE_FR, parse_dates=["captured_at"])
    feat = opt.extract_features(fr).dropna(subset=FEATURE_BASE_COLS + [TARGET])
    feat = feat.sort_values("captured_at").reset_index(drop=True)
    fmat = build_feature_matrix(feat)
    v11p = reconstruct_v11_params(trials_csv)
    heur = feat.apply(lambda r: opt.parametric_score_candidate(r, v11p), axis=1)
    data = {"feat": feat, "fmat": fmat, "heur": heur}
    pd.to_pickle(data, FEAT_CACHE)
    return data


def summarize(folds: list) -> dict:
    def mean(key):
        xs = [f[key] for f in folds if f[key] == f[key]]
        return round(statistics.mean(xs), 3) if xs else float("nan")
    ups = [f["uplift"] for f in folds]
    return {
        "folds": folds, "n_folds": len(folds),
        "ic_ml": mean("m_ic"), "uplift": round(statistics.mean(ups), 3),
        "uplift_sd": round(statistics.pstdev(ups), 3) if len(ups) > 1 else 0.0,
        "pos_folds": sum(u > 0 for u in ups),
        "top10_ml": mean("m_d10"), "d10_lift": mean("d10_lift"),
        "t1_lift": mean("t1_lift"), "fit_secs": round(sum(f["fit_secs"] for f in folds), 1),
    }


def calib_diagnostic(data, name, test_days):
    """Entrena con todo menos los últimos `test_days` y compara la calibración
    in-sample (la de prod) contra la distribución real de predicciones fuera de
    muestra: si el modelo sobre/sub-estima, el % de scores ≥70/≥85 se desvía del
    30%/15% esperado. También mide el tamaño del artefacto."""
    feat, fmat = data["feat"], data["fmat"]
    make, sample, hl, _ = CANDIDATES[name]
    cut = feat["captured_at"].max() - timedelta(days=test_days)
    tr, te = feat["captured_at"] < cut, feat["captured_at"] >= cut
    n_tr = int(tr.sum())
    idx = feat[tr].sample(n=n_tr if sample is None else min(sample, n_tr), random_state=42).index
    sw = recency_weights(feat.loc[idx, "captured_at"], cut, hl) if hl else None
    model = make()
    model.fit(fmat.loc[idx].values, feat.loc[idx, TARGET], sample_weight=sw)
    calib = np.percentile(model.predict(fmat.loc[idx].values), np.arange(0, 101))
    pred_te = model.predict(fmat.loc[te].values)
    scores = np.searchsorted(calib, pred_te, side="right")
    buf = io.BytesIO()
    joblib.dump({"model": model, "calibration_pcts": list(calib)}, buf)
    return {
        "name": name, "n_test": int(te.sum()),
        "pct_ge70": round(100 * float((scores >= 70).mean()), 1),
        "pct_ge85": round(100 * float((scores >= 85).mean()), 1),
        "median_score": float(np.median(scores)),
        "size_mb": round(buf.tell() / 1e6, 2),
    }


def write_report(results: dict):
    ctrl = results.get("runs", {}).get("A_gbr_control")
    rows = []
    for name, r in sorted(results.get("runs", {}).items(), key=lambda kv: -kv[1]["uplift"]):
        dic = f"{r['uplift'] - ctrl['uplift']:+.3f}" if ctrl else "—"
        dtop = f"{r['d10_lift'] - ctrl['d10_lift']:+.1f}" if ctrl else "—"
        rows.append([name, r["desc"], r["ic_ml"], f"{r['uplift']:+.3f}", r["uplift_sd"],
                     f"{r['pos_folds']}/{r['n_folds']}", r["top10_ml"],
                     f"{r['d10_lift']:+.1f}", f"{r['t1_lift']:+.1f}", dic, dtop, r["fit_secs"]])
    calib = results.get("calib", {})
    calib_tbl = md_table(["candidato", "n_test", "% score≥70 (esperado 30)",
                          "% score≥85 (esperado 15)", "score mediano (esperado 50)", "tamaño MB"],
                         [[c["name"], c["n_test"], c["pct_ge70"], c["pct_ge85"],
                           c["median_score"], c["size_mb"]] for c in calib.values()]) if calib else "_(no corrido)_"
    out = f"""# ML experiments — variantes del modelo de scoring — {datetime.now():%Y-%m-%d %H:%M}

**Datos:** {results.get('data', '')}. Walk-forward idéntico a `ml_train.py`
(`--min-train-days {results.get('min_train_days')}`, `--test-days {results.get('test_days')}`).
Features fijos ({len(FEATURE_NAMES)}), target `net_apr`. Control = `A_gbr_control` (GBR de prod).

## 1. Walk-forward por candidato (medias sobre folds)

{md_table(["candidato", "descripción", "IC ML", "uplift vs heur", "σ uplift", "folds +",
           "net_apr top10 ML", "Δtop10 vs heur", "Δtop1% vs heur", "ΔIC vs control",
           "Δtop10 vs control", "fit s (total)"], rows)}

Criterio de adopción: superar al control en uplift IC **y** en Δtop10, con los
mismos folds positivos y σ ≤ 0.05; artefacto razonable (<5 MB).

## 2. Calibración in-sample vs fuera de muestra (último bloque de test)

{calib_tbl}
"""
    REPORT_DIR.mkdir(exist_ok=True)
    path = REPORT_DIR / f"ml_experiments_{datetime.now():%Y%m%d}.md"
    path.write_text(out, encoding="utf-8")
    return path


def main():
    ap = argparse.ArgumentParser(description="Experimentos de modelo ML de scoring (local).")
    ap.add_argument("--only", nargs="*", help="candidatos a correr (default: todos)")
    ap.add_argument("--calib", nargs="*", help="candidatos para el diagnóstico de calibración")
    ap.add_argument("--trials-csv", default="reports/optimizer_20260617_trials.csv")
    ap.add_argument("--min-train-days", type=int, default=45)
    ap.add_argument("--test-days", type=int, default=7)
    ap.add_argument("--rebuild-features", action="store_true")
    ap.add_argument("--report-only", action="store_true")
    args = ap.parse_args()
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass

    results = json.loads(RESULTS.read_text()) if RESULTS.exists() else {"runs": {}, "calib": {}}
    if not args.report_only:
        data = load_data(Path(args.trials_csv), args.rebuild_features)
        feat = data["feat"]
        results.update(data=f"{len(feat):,} filas, {feat['captured_at'].min().date()} → "
                            f"{feat['captured_at'].max().date()}",
                       min_train_days=args.min_train_days, test_days=args.test_days)
        names = [] if args.calib is not None and args.only is None else (args.only or list(CANDIDATES))
        for name in names:
            make, sample, hl, desc = CANDIDATES[name]
            t0 = time.time()
            folds = walk_forward(feat, data["fmat"], data["heur"], args.min_train_days,
                                 args.test_days, make_model=make, train_sample=sample,
                                 half_life_days=hl)
            results["runs"][name] = {**summarize(folds), "desc": desc}
            r = results["runs"][name]
            print(f"== {name}: uplift={r['uplift']:+.3f} σ={r['uplift_sd']} "
                  f"pos={r['pos_folds']}/{r['n_folds']} Δtop10={r['d10_lift']:+.1f} "
                  f"({time.time() - t0:.0f}s)", flush=True)
            RESULTS.write_text(json.dumps(results, default=str))
        for name in args.calib or []:
            results.setdefault("calib", {})[name] = calib_diagnostic(data, name, args.test_days)
            print(f"== calib {name}: {results['calib'][name]}", flush=True)
            RESULTS.write_text(json.dumps(results, default=str))

    print(f"Reporte: {write_report(results)}")


if __name__ == "__main__":
    main()
