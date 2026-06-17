#!/usr/bin/env python3
"""ML stability — ¿el uplift del ML sobre el heurístico v11.0 es estable en el tiempo?

Validación WALK-FORWARD: en vez de un solo split, entrena el GBR en una ventana
expandible y testea en ventanas sucesivas de ~1 semana. Para cada fold compara
rank IC (heurístico vs ML) y, lo que importa para el bolsillo, el net_apr del
TOP (decil y top-1%) — cuánto subiría la ganancia si tradeás las mejores.

Responde: ¿el ML es consistentemente mejor predictor, y cuánto eleva la ganancia
del top? Local-only, soporte de decisión. NO toca producción.

Requiere: pip install -r requirements-dev.txt

Uso:
    python scripts/ml_stability.py
    python scripts/ml_stability.py --min-train-days 45 --test-days 7
"""
import argparse
import statistics
import sys
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

import pandas as pd
from sklearn.ensemble import GradientBoostingRegressor

import scoring_optimizer as opt
from ml_diagnostic import (ML_FEATURES, TARGET, md_table, reconstruct_v11_params,
                           spearman)

REPORT_DIR = ROOT / "reports"
CACHE_FR = ROOT / "cache" / "fr_snapshots.csv"
TRAIN_SAMPLE = 200_000


def top_stats(df, key_vals, frac):
    """net_apr medio y % rentable del top `frac` ordenado por key_vals."""
    s = pd.Series(key_vals, index=df.index)
    thr = s.rank(pct=True) >= (1 - frac)
    sub = df[thr]
    if len(sub) < 5:
        return float("nan"), float("nan")
    return round(sub[TARGET].mean(), 1), round(100 * sub["is_profitable"].mean(), 0)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--trials-csv", default="reports/optimizer_20260617_trials.csv")
    ap.add_argument("--min-train-days", type=int, default=45)
    ap.add_argument("--test-days", type=int, default=7)
    args = ap.parse_args()
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass

    print("[1/4] Cargando cache + extrayendo features...")
    if not CACHE_FR.exists():
        sys.exit(f"Falta {CACHE_FR}. Corre el optimizer una vez para el cache.")
    fr = pd.read_csv(CACHE_FR, parse_dates=["captured_at"])
    feat = opt.extract_features(fr).dropna(subset=ML_FEATURES + [TARGET])
    feat = feat.sort_values("captured_at").reset_index(drop=True)
    print(f"  {len(feat):,} filas")

    print("[2/4] Scoreando heurístico v11.0 (una vez, función fija)...")
    v11p = reconstruct_v11_params(Path(args.trials_csv))
    feat["_score"] = feat.apply(lambda r: opt.parametric_score_candidate(r, v11p), axis=1)

    print("[3/4] Walk-forward folds...")
    dmin, dmax = feat["captured_at"].min(), feat["captured_at"].max()
    start = dmin + timedelta(days=args.min_train_days)
    folds, ws = [], start
    while ws + timedelta(days=args.test_days) <= dmax + timedelta(days=1):
        we = ws + timedelta(days=args.test_days)
        train = feat[feat["captured_at"] < ws]
        test = feat[(feat["captured_at"] >= ws) & (feat["captured_at"] < we)]
        if len(train) < 5000 or len(test) < 2000:
            ws = we
            continue
        tr_fit = train.sample(n=min(TRAIN_SAMPLE, len(train)), random_state=42)
        gbr = GradientBoostingRegressor(n_estimators=300, max_depth=3,
                                        learning_rate=0.05, subsample=0.7,
                                        random_state=42)
        gbr.fit(tr_fit[ML_FEATURES], tr_fit[TARGET])
        pred = gbr.predict(test[ML_FEATURES])

        h_ic = spearman(test["_score"], test[TARGET])
        m_ic = spearman(pd.Series(pred, index=test.index), test[TARGET])
        h_d10, h_d10p = top_stats(test, test["_score"], 0.10)
        m_d10, m_d10p = top_stats(test, pred, 0.10)
        h_t1, h_t1p = top_stats(test, test["_score"], 0.01)
        m_t1, m_t1p = top_stats(test, pred, 0.01)
        folds.append(dict(win=f"{ws.date()}→{we.date()}", n_tr=len(train), n_te=len(test),
                          h_ic=round(h_ic, 3), m_ic=round(m_ic, 3),
                          uplift=round(m_ic - h_ic, 3),
                          h_d10=h_d10, m_d10=m_d10, d10_lift=round(m_d10 - h_d10, 1),
                          h_t1=h_t1, m_t1=m_t1, t1_lift=round(m_t1 - h_t1, 1),
                          m_d10p=m_d10p, h_d10p=h_d10p))
        print(f"  {folds[-1]['win']}: IC heur={h_ic:.3f} ml={m_ic:.3f} "
              f"uplift={m_ic-h_ic:+.3f} | top10 net_apr heur={h_d10} ml={m_d10}")
        ws = we

    if not folds:
        sys.exit("Sin folds válidos. Ajustá --min-train-days / --test-days.")

    print("[4/4] Reporte...")
    ups = [f["uplift"] for f in folds]
    d10l = [f["d10_lift"] for f in folds]
    t1l = [f["t1_lift"] for f in folds]
    mean_up = statistics.mean(ups)
    sd_up = statistics.pstdev(ups) if len(ups) > 1 else 0.0
    all_pos = all(u > 0 for u in ups)
    stable = all_pos and mean_up >= 0.05 and sd_up <= 0.05

    verdict = ("ESTABLE y MATERIAL — el ML bate al heurístico de forma consistente. "
               "Vale explorar el camino híbrido (ML offline)."
               if stable else
               ("INCONSISTENTE — el uplift no es robusto entre ventanas; quedarse "
                "heurístico y usar importances." if not all_pos else
                "POSITIVO pero MARGINAL/RUIDOSO — uplift bajo o variable; "
                "priorizar refinar el heurístico antes que ML."))

    out = f"""# ML stability (walk-forward) — uplift ML vs heurístico v11.0 — {datetime.now():%Y-%m-%d %H:%M}

**Setup:** train expandible (≥{args.min_train_days}d), test en ventanas sucesivas de
{args.test_days}d. GBR re-entrenado por fold (solo con pasado → sin look-ahead).
Target `net_apr`. {len(folds)} folds. Local-only, soporte de decisión.

## 1. Por fold

{md_table(["Ventana test", "n_test", "IC heur", "IC ML", "Uplift",
           "top10 heur", "top10 ML", "Δtop10", "top1% ML", "%rent top10 ML"],
          [[f["win"], f["n_te"], f["h_ic"], f["m_ic"], f"{f['uplift']:+.3f}",
            f["h_d10"], f["m_d10"], f"{f['d10_lift']:+.1f}", f["m_t1"],
            f"{f['m_d10p']:.0f}%"] for f in folds])}

## 2. Resumen de estabilidad

{md_table(["Métrica", "Valor"],
          [["Uplift IC medio", f"{mean_up:+.3f}"],
           ["Uplift IC desvío (σ)", f"{sd_up:.3f}"],
           ["Uplift positivo en todos los folds", "Sí" if all_pos else "No"],
           ["Δ net_apr top-decil medio (ML − heur)", f"{statistics.mean(d10l):+.1f}"],
           ["Δ net_apr top-1% medio (ML − heur)", f"{statistics.mean(t1l):+.1f}"]])}

**Interpretación de ganancia:** el Δ net_apr del top es lo que subiría tu rendimiento
anualizado si tradeás las mejores oportunidades rankeadas por ML en vez de por el
score. (net_apr está en % anualizado; recordá que a fee 0.30% el heurístico deja el
top en ~break-even.)

## 3. Veredicto

**{verdict}**

Umbrales: ESTABLE = uplift>0 en todos los folds, medio ≥0.05, σ≤0.05.
Aun si es estable, el primer paso es el **híbrido offline** (GBR en local re-deriva
pesos o pre-computa scores cacheados), NO inferencia sklearn en Railway (no tiene
numpy/sklearn). ML online solo si el híbrido no alcanza y el uplift es grande/durable.
"""
    REPORT_DIR.mkdir(exist_ok=True)
    path = REPORT_DIR / f"ml_stability_{datetime.now():%Y%m%d}.md"
    path.write_text(out, encoding="utf-8")
    print(f"\n  Reporte: {path}")
    print(f"  Uplift IC medio={mean_up:+.3f} σ={sd_up:.3f} | todos positivos={all_pos}")
    print(f"  Δ top-decil net_apr medio={statistics.mean(d10l):+.1f} | "
          f"Δ top-1%={statistics.mean(t1l):+.1f}")
    print(f"  -> {'ESTABLE' if stable else ('INCONSISTENTE' if not all_pos else 'MARGINAL')}")


if __name__ == "__main__":
    main()
