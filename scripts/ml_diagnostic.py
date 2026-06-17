#!/usr/bin/env python3
"""ML diagnostic — ¿cuánto techo de predictibilidad deja el heurístico v11.0?

OPERACIÓN LOCAL Y MANUAL (no corre en Railway). Soporte de decisión, NO un
modelo de producción: entrena un GradientBoosting sobre los MISMOS features que
ve el scoring para predecir el target del heurístico (net_apr) y compara su
poder predictivo (rank IC) contra el score v11.0. Si el ML no supera al
heurístico por un margen claro, no vale el costo de meter sklearn en prod.

Reusa la extracción de features del optimizer (scripts/scoring_optimizer.py) y
el score v11.0 (parametric_score_candidate con los params del último run).

Requiere: pip install -r requirements-dev.txt  (añade scikit-learn)

Uso:
    python scripts/ml_diagnostic.py
    python scripts/ml_diagnostic.py --trials-csv reports/optimizer_20260617_trials.csv

Output: reports/ml_diagnostic_YYYYMMDD.md
"""
import argparse
import sys
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

import pandas as pd
from sklearn.ensemble import GradientBoostingRegressor

import scoring_optimizer as opt

REPORT_DIR = ROOT / "reports"
CACHE_FR = ROOT / "cache" / "fr_snapshots.csv"

# Features CRUDOS/indicadores que ve el scoring — sin columnas derivadas del
# score (evita leakage). mom_signal (categórico) se resume en mom_points.
ML_FEATURES = [
    "cv", "min_ratio", "streak", "pct", "volume", "settlement_avg", "ppd",
    "fee_drag", "current_rate", "reality_ratio", "z_value", "mom_points",
    "pctl_percentile", "pctl_points",
]
TARGET = "net_apr"
TRAIN_SAMPLE = 200_000   # subsample del train para entrenar el GBR (perf)


def spearman(a, b):
    m = a.notna() & b.notna()
    return a[m].rank().corr(b[m].rank()) if m.sum() > 2 else float("nan")


def reconstruct_v11_params(trials_csv: Path) -> dict:
    """best_p del optimizer desde el trials CSV (params del score v11.0 adoptado)."""
    tr = pd.read_csv(trials_csv)
    tr = tr[tr["value"] > -900]
    best = tr.loc[tr["value"].idxmax()]
    bp = {c[len("params_"):]: best[c] for c in tr.columns if c.startswith("params_")}
    wk = ("w_stab", "w_cons", "w_liq", "w_yield", "w_fee", "w_trend")
    scale = 100.0 / sum(bp[k] for k in wk)
    p = {k: bp[k] * scale for k in wk}
    for k, v in bp.items():
        if k not in p:
            p[k] = (v == "True" or v is True) if k == "caps_enabled" else v
    return p


def decile_table(df, key_vals, label):
    """net_apr medio + % rentable por decil de `key_vals`."""
    d = pd.qcut(pd.Series(key_vals, index=df.index).rank(method="first"), 10,
                labels=range(1, 11))
    t = df.assign(_d=d).groupby("_d", observed=True).agg(
        n=("net_apr", "size"), net_apr=("net_apr", "mean"),
        profit=("is_profitable", "mean"))
    t["net_apr"] = t["net_apr"].round(1)
    t["profit"] = (t["profit"] * 100).round(0)
    return t


def md_table(headers, rows):
    sep = "|" + "|".join(" --- " for _ in headers) + "|"
    h = "| " + " | ".join(map(str, headers)) + " |"
    b = "\n".join("| " + " | ".join(map(str, r)) + " |" for r in rows)
    return "\n".join([h, sep, b])


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--trials-csv", default="reports/optimizer_20260617_trials.csv")
    args = ap.parse_args()
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass

    print("[1/5] Cargando cache + extrayendo features...")
    if not CACHE_FR.exists():
        sys.exit(f"Falta {CACHE_FR}. Corre el optimizer una vez para generar el cache.")
    fr = pd.read_csv(CACHE_FR, parse_dates=["captured_at"])
    feat = opt.extract_features(fr)
    feat = feat.dropna(subset=ML_FEATURES + [TARGET]).reset_index(drop=True)
    print(f"  {len(feat):,} filas")

    print("[2/5] Split temporal (last 1/3 = test)...")
    dmin, dmax = feat["captured_at"].min(), feat["captured_at"].max()
    cutoff = dmax - timedelta(days=round((dmax - dmin).days / 3))
    train = feat[feat["captured_at"] < cutoff].copy()
    test = feat[feat["captured_at"] >= cutoff].copy()
    print(f"  train {len(train):,} | test {len(test):,} (cutoff {cutoff.date()})")

    print("[3/5] Scoreando heurístico v11.0...")
    v11p = reconstruct_v11_params(Path(args.trials_csv))
    test["_score"] = test.apply(lambda r: opt.parametric_score_candidate(r, v11p), axis=1)
    heur_ic = spearman(test["_score"], test[TARGET])

    print(f"[4/5] Entrenando GradientBoosting (train sample {TRAIN_SAMPLE:,})...")
    tr_fit = train.sample(n=min(TRAIN_SAMPLE, len(train)), random_state=42)
    gbr = GradientBoostingRegressor(n_estimators=300, max_depth=3,
                                    learning_rate=0.05, subsample=0.7,
                                    random_state=42)
    gbr.fit(tr_fit[ML_FEATURES], tr_fit[TARGET])
    test["_ml"] = gbr.predict(test[ML_FEATURES])
    ml_ic = spearman(test["_ml"], test[TARGET])
    uplift = ml_ic - heur_ic

    print("[5/5] Reporte...")
    heur_dec = decile_table(test, test["_score"], "heur")
    ml_dec = decile_table(test, test["_ml"], "ml")
    imp = sorted(zip(ML_FEATURES, gbr.feature_importances_),
                 key=lambda x: -x[1])

    verdict = ("QUEDARSE HEURÍSTICO" if uplift < 0.05 else
               "CONSIDERAR ML (verificar estabilidad/decil top antes de comprometer prod)")

    out = f"""# ML diagnostic — techo de predictibilidad vs heurístico v11.0 — {datetime.now():%Y-%m-%d %H:%M}

**Target:** `net_apr` (APR-neto, lo que el scoring optimiza). **Split:** temporal
(test = último 1/3, desde {cutoff.date()}). **Features ML:** {len(ML_FEATURES)} crudos/indicadores
(sin columnas derivadas del score → sin leakage). **Modelo:** GradientBoostingRegressor.

> Diagnóstico local, NO producción. Mide si un modelo flexible predice net_apr
> mejor que el heurístico, y qué features pesan. Prod no tiene numpy/sklearn.

## 1. Rank IC (Spearman pred ↔ net_apr) en test

{md_table(["", "Rank IC (test)"],
          [["Heurístico v11.0", f"{heur_ic:.3f}"],
           ["GradientBoosting", f"{ml_ic:.3f}"],
           ["**Uplift (ML − heur)**", f"**{uplift:+.3f}**"]])}

**Umbral de decisión:** uplift < 0.05 → quedarse heurístico (barato, interpretable,
sin deps en prod) y usar las importances para afinar pesos. Uplift ≥ 0.05 y estable
→ considerar híbrido (ML offline re-deriva pesos), no inferencia sklearn online.

**Veredicto:** {verdict}

## 2. Backtest por decil en test (net_apr medio | % rentable)

### Ordenado por score heurístico v11.0
{md_table(["decil", "n", "net_apr", "% rent"],
          [[i, r.n, r.net_apr, f"{r.profit:.0f}%"] for i, r in heur_dec.iterrows()])}

### Ordenado por predicción ML
{md_table(["decil", "n", "net_apr", "% rent"],
          [[i, r.n, r.net_apr, f"{r.profit:.0f}%"] for i, r in ml_dec.iterrows()])}

Top decil — heurístico net_apr {heur_dec.iloc[-1].net_apr} ({heur_dec.iloc[-1].profit:.0f}% rent) vs
ML {ml_dec.iloc[-1].net_apr} ({ml_dec.iloc[-1].profit:.0f}% rent).

## 3. Feature importances (GradientBoosting)

{md_table(["feature", "importance"],
          [[f, f"{v:.3f}"] for f, v in imp])}

Accionable aun sin adoptar ML: las features de mayor importancia indican qué
dimensiones del heurístico merecen más peso (cruzar con los pesos v11.0).
"""
    REPORT_DIR.mkdir(exist_ok=True)
    path = REPORT_DIR / f"ml_diagnostic_{datetime.now():%Y%m%d}.md"
    path.write_text(out, encoding="utf-8")

    print(f"\n  Reporte: {path}")
    print(f"  Heurístico IC={heur_ic:.3f} | ML IC={ml_ic:.3f} | uplift={uplift:+.3f}")
    print(f"  -> {verdict}")
    print("  Top features:", ", ".join(f"{f}={v:.2f}" for f, v in imp[:5]))


if __name__ == "__main__":
    main()
