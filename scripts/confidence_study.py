#!/usr/bin/env python3
"""Confianza por oportunidad — LOCAL, read-only (salvo --export).

Motivo: en los picks de la cartera (top-5, pred > 0) el APR esperado del modelo
vigente se cumple en ~40 % de las entradas; la tasa cae a ~la mitad desde el
primer día (reversión a la media). Rankear por probabilidad o por cota inferior
empeora la cartera (reports/ml_v2_20261025.md), así que la confianza es una
SEGUNDA dimensión: se muestra y se usa para dimensionar el capital, no para
ordenar.

Cabezas HistGB (sklearn pinneado) sobre las 14 FEATURE_NAMES (spec settled_v1),
entrenadas solo con filas donde el modelo vigente predice > 0 (donde se opera):
  q10 / q50 / q90  cuantiles de y_rate (APR neto realizado con la salida H30)
  p5               P(y_rate ≥ ENTER_NAPR) — "probabilidad de que rinda de verdad"

Calibración SOBRE LOS PICKS (los cuantiles crudos cubren ~55 % en los picks por
sesgo de selección), expansiva y sin look-ahead (solo picks ya cerrados):
  * isotónica para p5 (solo diagnóstico: en WF no discrimina);
  * CQR: q̂ = cuantil 0.8 de max(q10 − y, y − q90) → banda [q10 − q̂, q90 + q̂].
Sizing: tier Alta (≥ mediana expansiva de la métrica) 1.25× / Baja 0.75×.
Métricas de tier comparadas: p5 y ancho relativo (q90 − q10)/max(|q50|, 5).
La confianza exportada es la frecuencia real de APR ≥ 5 % de cada tier en los picks.

Gate de export: sizing ≥ base en ≥ 3 de 4 trimestres WF y en el hold-out, DD
no peor y cobertura CQR de los picks en [0.72, 0.88].

Uso:
    python -u scripts/confidence_study.py [--refresh] [--export]
Salida: reports/confidence_YYYYMMDD.md; --export → models/confidence_model.joblib
"""
import argparse
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))
import ml_binance as mb  # noqa: E402
import ml_v2  # noqa: E402
from analysis.ai_analyzer import ENTER_NAPR  # noqa: E402
from analysis.ml_features import FEATURE_NAMES  # noqa: E402

QUANTILES = {"q10": 0.10, "q50": 0.50, "q90": 0.90}
COVERAGE = 0.80
SIZE_HI, SIZE_LO = 1.25, 0.75
MIN_CALIB = 30              # picks cerrados antes de calibrar
METRICS = {"p5": True, "relw": False}   # métrica: True = más alto es mejor


# ── Cabezas ─────────────────────────────────────────────────────────────────
def fit_heads(tr: pd.DataFrame) -> dict:
    from sklearn.ensemble import HistGradientBoostingClassifier, HistGradientBoostingRegressor
    X, y = tr[FEATURE_NAMES].to_numpy(), tr["y_rate"].to_numpy()
    heads = {k: HistGradientBoostingRegressor(loss="quantile", quantile=q, **mb.MODEL_PARAMS).fit(X, y)
             for k, q in QUANTILES.items()}
    heads["p5"] = HistGradientBoostingClassifier(**mb.MODEL_PARAMS).fit(X, (y >= ENTER_NAPR).astype(int))
    return heads


def predict_heads(heads: dict, d: pd.DataFrame) -> pd.DataFrame:
    X = d[FEATURE_NAMES].to_numpy()
    out = pd.DataFrame({k: heads[k].predict(X) for k in QUANTILES}, index=d.index)
    out["p5"] = heads["p5"].predict_proba(X)[:, 1]
    # Cuantiles cruzados (raro en GBM) → se ordenan para que la banda sea válida.
    out[list(QUANTILES)] = np.sort(out[list(QUANTILES)].to_numpy(), axis=1)
    out["relw"] = (out["q90"] - out["q10"]) / np.maximum(out["q50"].abs(), 5)
    return out


# ── Picks de la cartera (misma política que ml_binance.portfolio) ───────────
def picks(d: pd.DataFrame) -> pd.DataFrame:
    d = d.sort_values("t")
    open_pos, rows = [], []
    for t, g in d.groupby(d["t"].dt.floor("8h")):
        open_pos = [p for p in open_pos if p[1] > t]
        free = mb.SLOTS - len(open_pos)
        if free <= 0:
            continue
        held = {p[0] for p in open_pos}
        for r in g.sort_values("p_prod", ascending=False).drop_duplicates("symbol").itertuples():
            if free <= 0:
                break
            if r.symbol in held or r.p_prod <= 0:
                continue
            open_pos.append((r.symbol, r.t + timedelta(days=float(r.hold_days))))
            rows.append(r.Index)
            held.add(r.symbol)
            free -= 1
    e = d.loc[rows].copy()
    e["exit_t"] = e["t"] + pd.to_timedelta(e["hold_days"], unit="D")
    return e


# ── Calibración expansiva sobre picks (sin look-ahead) ──────────────────────
def isotonic_arrays(x, y):
    from sklearn.isotonic import IsotonicRegression
    ir = IsotonicRegression(y_min=0.0, y_max=1.0, out_of_bounds="clip").fit(x, y)
    return [float(v) for v in ir.X_thresholds_], [float(v) for v in ir.y_thresholds_]


def cqr_q(e: pd.DataFrame) -> float:
    s = np.maximum(e["q10"] - e["y_rate"], e["y_rate"] - e["q90"])
    return float(np.quantile(s, COVERAGE))


def expanding_calibration(e: pd.DataFrame) -> pd.DataFrame:
    """Para cada pick, calibra con los picks cerrados antes de su entrada."""
    e = e.sort_values("t").copy()
    for c in ("p5_cal", "lo", "hi", *[f"cut_{m}" for m in METRICS]):
        e[c] = np.nan
    for i, r in e.iterrows():
        past = e[e["exit_t"] <= r["t"]]
        if len(past) < MIN_CALIB:
            continue
        xs, ys = isotonic_arrays(past["p5"], (past["y_rate"] >= ENTER_NAPR).astype(float))
        e.at[i, "p5_cal"] = float(np.interp(r["p5"], xs, ys))
        q = cqr_q(past)
        e.at[i, "lo"], e.at[i, "hi"] = r["q10"] - q, r["q90"] + q
        for m in METRICS:
            e.at[i, f"cut_{m}"] = float(past[m].median())
    return e


def weights(e: pd.DataFrame, m: str) -> pd.Series:
    good = e[m] >= e[f"cut_{m}"] if METRICS[m] else e[m] <= e[f"cut_{m}"]
    w = pd.Series(np.where(good, SIZE_HI, SIZE_LO), index=e.index)
    return w.where(e[f"cut_{m}"].notna(), 1.0)


def pnl_stats(e: pd.DataFrame, w: pd.Series, days: float) -> dict:
    notional = mb.CAPITAL / mb.SLOTS / 2
    pnl = e["net_frac"] * notional * w
    eq = mb.CAPITAL + pd.Series(pnl.to_numpy(), index=e["exit_t"]).sort_index().cumsum()
    dd = float(((eq.cummax() - eq) / eq.cummax()).max() * 100) if len(eq) else 0.0
    return {"pnl": float(pnl.sum()), "ann": float(pnl.sum() / mb.CAPITAL / max(days, 1) * 365 * 100), "dd": dd}


# ── Main ────────────────────────────────────────────────────────────────────
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--refresh", action="store_true", help="recalcular filas/labels (ml_v2)")
    ap.add_argument("--export", action="store_true", help="exportar models/confidence_model.joblib si pasa el gate")
    a = ap.parse_args()
    sys.stdout.reconfigure(encoding="utf-8")

    import joblib
    df = ml_v2.build(a.refresh)
    df["p_prod"] = joblib.load(ROOT / "models" / "scoring_model.joblib")["model"].predict(
        df[FEATURE_NAMES].to_numpy())
    cand = df[df["p_prod"] > 0]
    print(f"filas {len(df):,}; con pred vigente > 0: {len(cand):,}", flush=True)

    t_max = df["t"].max()
    holdout_start = (t_max - timedelta(days=mb.HOLDOUT_DAYS)).floor("D")
    ms = pd.date_range(end=holdout_start, periods=mb.WF_MONTHS + 1, freq="MS", tz="UTC")
    folds = [(ms[k], ms[k + 1]) for k in range(mb.WF_MONTHS)]
    folds[-1] = (folds[-1][0], holdout_start)
    folds.append((holdout_start, t_max + timedelta(seconds=1)))

    preds = []
    for fs, fe in folds:
        tr = ml_v2.train_slice(cand, fs, "all")
        te = cand[(cand["t"] >= fs) & (cand["t"] < fe)]
        if len(te) == 0:
            continue
        print(f"fold {fs:%Y-%m-%d}: train {len(tr):,} test {len(te):,}", flush=True)
        preds.append(predict_heads(fit_heads(tr), te))
    P = pd.concat(preds)
    period = cand.loc[P.index].join(P)
    e = expanding_calibration(picks(period))
    ev = e.dropna(subset=["p5_cal"])
    print(f"picks {len(e)}, con calibración previa {len(ev)}", flush=True)

    # Fiabilidad y cobertura (picks con calibración previa)
    hit = (ev["y_rate"] >= ENTER_NAPR).astype(float)
    brier_raw = float(((ev["p5"] - hit) ** 2).mean())
    brier_cal = float(((ev["p5_cal"] - hit) ** 2).mean())
    cov_raw = float(((ev["y_rate"] >= ev["q10"]) & (ev["y_rate"] <= ev["q90"])).mean())
    cov_cqr = float(((ev["y_rate"] >= ev["lo"]) & (ev["y_rate"] <= ev["hi"])).mean())
    reach_pred = float((e["y_rate"] >= e["p_prod"]).mean())

    # Sizing por trimestre WF + hold-out
    e["seg"] = np.where(e["t"] >= holdout_start, "hold-out",
                        e["t"].dt.year.astype(str) + "-T" + ((e["t"].dt.month - 1) // 3 + 1).astype(str))
    seg_rows, wins = [], {m: 0 for m in METRICS}
    ones = pd.Series(1.0, index=e.index)
    W = {m: weights(e, m) for m in METRICS}
    wf_start = folds[0][0]
    n_q = 0                      # trimestres WF con sizing activo (ya hay calibración)
    for s, g in e.groupby("seg", sort=True):
        if s == "hold-out":
            days = (t_max - holdout_start).days
        else:                    # días calendario del trimestre dentro del WF
            q = pd.Period(s.replace("-T", "Q"), freq="Q")
            q0 = max(q.start_time.tz_localize("UTC"), wf_start)
            q1 = min(q.end_time.tz_localize("UTC"), holdout_start)
            days = (q1 - q0).days + 1
        base = pnl_stats(g, ones.loc[g.index], days)
        active = bool(e.loc[g.index, f"cut_{next(iter(METRICS))}"].notna().any())
        row = {"seg": s, "n": len(g), "base": base["ann"], "active": active}
        if s != "hold-out" and active:
            n_q += 1
        for m in METRICS:
            r = pnl_stats(g, W[m].loc[g.index], days)
            row[m] = r["ann"]
            if s != "hold-out" and active and r["pnl"] >= base["pnl"]:
                wins[m] += 1
        seg_rows.append(row)
    days_all = (t_max - wf_start).days
    tot = {"base": pnl_stats(e, ones, days_all), **{m: pnl_stats(e, W[m], days_all) for m in METRICS}}
    hold = e[e["seg"] == "hold-out"]

    best = max(METRICS, key=lambda m: tot[m]["pnl"])
    # p5 (interpretable) salvo que el ancho relativo gane por > 1 pp anual
    metric = "relw" if best == "relw" and tot["relw"]["ann"] - tot["p5"]["ann"] > 1.0 else "p5"
    hold_ok = len(hold) == 0 or pnl_stats(hold, W[metric].loc[hold.index], 30)["pnl"] >= \
        pnl_stats(hold, ones.loc[hold.index], 30)["pnl"]
    need = max(1, round(n_q * 0.75))
    # Probabilidad mostrada = frecuencia real de APR ≥ 5 % por tier (picks evaluados)
    good = (ev[metric] >= ev[f"cut_{metric}"]) if METRICS[metric] else (ev[metric] <= ev[f"cut_{metric}"])
    tier_ev = {"alta": ev[good], "baja": ev[~good]}
    gate = {"segments_ok": wins[metric] >= need, "holdout_ok": bool(hold_ok),
            "dd_ok": tot[metric]["dd"] <= tot["base"]["dd"] + 1e-9,
            "coverage_ok": 0.72 <= cov_cqr <= 0.88}
    passed = all(gate.values())

    today = datetime.now(timezone.utc).strftime("%Y%m%d")
    L = [f"# Confianza por oportunidad — {today}", "",
         f"Picks de la cartera del modelo vigente (top-5 cada 8 h, pred > 0, 5 slots, 5.200 USD, 1x, salida H30) "
         f"en walk-forward mensual {folds[0][0]:%Y-%m} → {holdout_start:%Y-%m-%d} + hold-out hasta {t_max:%Y-%m-%d}. "
         f"Cabezas entrenadas solo con filas pred vigente > 0 ({len(cand):,}). El modelo vigente es in-sample "
         f"en su ventana de train (2026-07-12 → 2026-10-08, dataset de snapshots distinto).", "",
         f"- Picks: **{len(e)}** ({int(e['alpha'].sum())} Alpha). APR predicho medio {e['p_prod'].mean():.1f} %, "
         f"realizado {e['y_rate'].mean():.1f} % (mediana {e['y_rate'].median():.1f} %); "
         f"llegan a lo predicho: **{reach_pred * 100:.0f} %**.",
         f"- Calibración expansiva (≥ {MIN_CALIB} picks cerrados antes), evaluada en {len(ev)} picks:",
         f"  - P(APR ≥ {ENTER_NAPR:.0f} %): media cruda {ev['p5'].mean():.2f} / calibrada {ev['p5_cal'].mean():.2f} "
         f"vs real {hit.mean():.2f}; Brier {brier_raw:.3f} → {brier_cal:.3f}.",
         f"  - Banda p10–p90: cobertura cruda {cov_raw:.2f} → CQR {cov_cqr:.2f} (objetivo {COVERAGE:.2f}); "
         f"ancho medio CQR {(ev['hi'] - ev['lo']).mean():.0f} pp de APR.", "",
         f"## Sizing por confianza (Alta {SIZE_HI}× / Baja {SIZE_LO}×, corte = mediana expansiva), anual %", "",
         "| segmento | picks | base | p5 | ancho relativo |", "| --- | --- | --- | --- | --- |"]
    for r in seg_rows:
        L.append(f"| {r['seg']}{'' if r['active'] else ' (sin calibración)'} | {r['n']} | {r['base']:.1f} | "
                 f"{r['p5']:.1f} | {r['relw']:.1f} |")
    L.append(f"| **total** | {len(e)} | {tot['base']['ann']:.1f} | {tot['p5']['ann']:.1f} | {tot['relw']['ann']:.1f} |")
    L += ["", f"DD %: base {tot['base']['dd']:.2f}, p5 {tot['p5']['dd']:.2f}, ancho {tot['relw']['dd']:.2f}. "
              f"Trimestres WF con sizing ≥ base: p5 {wins['p5']}/{n_q}, ancho {wins['relw']}/{n_q}.", "",
          "## Fiabilidad de P(APR ≥ 5 %) calibrada (picks evaluados, quintiles)", "",
          "| quintil | n | P predicha | frecuencia real | APR real medio |", "| --- | --- | --- | --- | --- |"]
    ev = ev.assign(qq=pd.qcut(ev["p5_cal"].rank(method="first"), 5, labels=False) + 1)
    for q, g in ev.groupby("qq"):
        L.append(f"| {q} | {len(g)} | {g['p5_cal'].mean():.2f} | {(g['y_rate'] >= ENTER_NAPR).mean():.2f} | "
                 f"{g['y_rate'].mean():.1f} |")
    L += ["", f"## Tiers con la métrica `{metric}` (picks evaluados, corte expansivo)", "",
          "| tier | n | P(APR ≥ 5 %) real | APR real medio | APR real mediana |", "| --- | --- | --- | --- | --- |"]
    for t, g in tier_ev.items():
        L.append(f"| {t} | {len(g)} | {(g['y_rate'] >= ENTER_NAPR).mean():.2f} | {g['y_rate'].mean():.1f} | "
                 f"{g['y_rate'].median():.1f} |")
    L += ["", f"## Gate (métrica de tier elegida: `{metric}`)", "",
          *[f"- {k}: {'OK' if v else 'NO'}" for k, v in gate.items()],
          f"- **{'PASA' if passed else 'NO PASA'}**" + ("" if passed else " → no se exporta"), ""]
    path = ROOT / "reports" / f"confidence_{today}.md"
    path.write_text("\n".join(L), encoding="utf-8")
    print("\n".join(L))

    if a.export and passed:
        export(cand, e, metric, gate, tot, cov_cqr, brier_cal, today)
    elif a.export:
        print("gate NO pasa: no se exporta", flush=True)


def export(cand, e, metric, gate, tot, cov_cqr, brier_cal, today):
    import joblib
    import sklearn
    tr = cand[cand["t"] < cand["t"].max() - timedelta(days=mb.PURGE_DAYS)]
    heads = fit_heads(tr)
    cut = float(e[metric].median())
    good = e[metric] >= cut if METRICS[metric] else e[metric] <= cut
    hit = e["y_rate"] >= ENTER_NAPR
    # Confianza mostrada = frecuencia real de APR ≥ 5 % del tier en los picks (la
    # P de la cabeza p5 no discrimina en WF: ver reporte).
    tier_reach = {"alta": round(float(hit[good].mean()), 3), "baja": round(float(hit[~good].mean()), 3)}
    bundle = {
        **heads, "conformal_q": cqr_q(e), "tier_reach": tier_reach,
        "tier_metric": metric, "tier_high_is_good": METRICS[metric], "tier_cut": cut,
        "size_hi": SIZE_HI, "size_lo": SIZE_LO, "reach_apr": ENTER_NAPR,
        "feature_names": FEATURE_NAMES, "feature_spec": "settled_v1",
        "model_version": today,
        "train_window": {"from": str(tr["t"].min().date()), "to": str(tr["t"].max().date()), "rows": int(len(tr))},
        "val_metrics": {"gate": gate, "picks": int(len(e)), "coverage_cqr": round(cov_cqr, 3),
                        "brier_cal": round(brier_cal, 4),
                        "ann": {k: round(v["ann"], 1) for k, v in tot.items()}},
        "sklearn_version": sklearn.__version__,
    }
    joblib.dump(bundle, ROOT / "models" / "confidence_model.joblib", compress=3)
    print(f"exportado models/confidence_model.joblib ({today}, tier={metric})", flush=True)


if __name__ == "__main__":
    main()
