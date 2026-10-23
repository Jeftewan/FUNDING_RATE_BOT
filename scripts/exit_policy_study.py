#!/usr/bin/env python3
"""Estudio de la REGLA DE SALIDA sobre el dataset de Binance — LOCAL, read-only.

Pregunta de negocio: ¿cuándo conviene cerrar una posición spot_perp para
maximizar la ganancia en el tiempo? Compara, para las mismas entradas
(filas elegibles en prod, rankeadas por el heurístico v11.0 de prod y por el
modelo vigente), tres políticas:
  A  salir tras PAGAR el primer funding ≤ 0              (label real14 actual)
  B  salir tras dos pagos seguidos ≤ 0                    (= alerta CLOSE_SUGGESTED)
  C  hold fijo 14 días pase lo que pase
y además H30 (hold hasta 30 d con regla B) para ver si holds largos rinden más.

Reporta neto medio por trade (top-10 % diario) y una cartera simulada
(5 slots, 5.200 USD, 1x) por política. Salida: reports/exit_policy_YYYYMMDD.md.
Requiere cache/binance/fr_binance.csv y cache/binance/features_v1.pkl (ml_binance.py).
"""
import sys
from datetime import timedelta, datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))
import ml_binance as mb  # noqa: E402
from analysis.ml_features import FEATURE_NAMES  # noqa: E402

FEE = mb.FEE


def policy_labels(fr: pd.DataFrame, keys: pd.DataFrame) -> pd.DataFrame:
    """net_frac y hold_days por política para las filas (symbol, funding_ts) de `keys`."""
    want = {}
    for s, t in zip(keys["symbol"], keys["funding_ts"]):
        want.setdefault(s, set()).add(int(t))
    out = []
    for sym, g in fr.groupby("symbol"):
        if sym not in want:
            continue
        g = g.sort_values("funding_ts")
        ts = g["funding_ts"].to_numpy(np.int64)
        rates = g["rate"].to_numpy(float)
        ihs = g["interval_hours"].to_numpy(float)
        idx = {t: k for k, t in enumerate(ts)}
        n = len(ts)
        for t in want[sym]:
            i = idx.get(t)
            if i is None:
                continue
            ih = max(1.0, ihs[i])
            res = {"symbol": sym, "funding_ts": t}
            for name, max_days, neg_needed, fixed in (("A", 14, 1, False), ("B", 14, 2, False),
                                                      ("C", 14, 0, True), ("H30", 30, 2, False)):
                max_int = int(round(max_days * 24 / ih))
                cum, held, neg_run, done = 0.0, 0, 0, False
                for j in range(1, max_int + 1):
                    if i + j >= n:
                        break
                    r = rates[i + j]
                    cum += r
                    held += 1
                    if not fixed:
                        neg_run = neg_run + 1 if r <= 0 else 0
                        if neg_run >= neg_needed:
                            done = True
                            break
                complete = done or held >= max_int
                res[f"net_{name}"] = (cum - FEE) if complete else np.nan
                res[f"hold_{name}"] = held * ih / 24 if complete else np.nan
            out.append(res)
    return pd.DataFrame(out)


def main():
    df = mb.dedupe_columns(pd.read_pickle(mb.FEAT_CACHE))
    t_max = df["t"].max()
    holdout_start = (t_max - timedelta(days=mb.HOLDOUT_DAYS)).floor("D")
    wf_start = (holdout_start - pd.DateOffset(months=mb.WF_MONTHS)).floor("D")
    sub = df[df["eligible"] & (df["t"] >= wf_start)].copy()

    # Modelo VIGENTE en prod (estimate_v0) aplicado con los features settled (aprox).
    import joblib
    bundle = joblib.load(ROOT / "models" / "scoring_model.joblib")
    sub["p_prod"] = bundle["model"].predict(sub[FEATURE_NAMES].to_numpy())
    # Modelo nuevo (E0a) entrenado con datos ANTERIORES al periodo evaluado (purga 15 d).
    tr = df[df["t"] < wf_start - timedelta(days=mb.PURGE_DAYS)]
    m = mb.fit(tr[FEATURE_NAMES].to_numpy(), tr["y_real14"].to_numpy())
    sub["p_new"] = m.predict(sub[FEATURE_NAMES].to_numpy())

    fr = pd.read_csv(mb.DATA, usecols=["symbol", "rate", "interval_hours", "funding_ts"])
    labs = policy_labels(fr, sub[["symbol", "funding_ts"]])
    sub = sub.merge(labs, on=["symbol", "funding_ts"], how="left")

    lines = [f"# Estudio de regla de salida — {datetime.now(timezone.utc):%Y-%m-%d}", "",
             f"Filas elegibles {len(sub):,} desde {wf_start:%Y-%m-%d} (incluye hold-out desde "
             f"{holdout_start:%Y-%m-%d}). Neto por nocional; cartera 5 slots, 5.200 USD, 1x.", "",
             "## Neto medio por trade del top-10 % diario (en % del nocional)", "",
             "| ranking | A: 1er ≤0 | B: 2 seguidos ≤0 | C: 14 d fijo | H30: hasta 30 d (B) |",
             "| --- | --- | --- | --- | --- |"]
    for rk in ("heur", "p_prod", "p_new"):
        cells = []
        for pol in ("A", "B", "C", "H30"):
            vals = []
            for _, g in sub.groupby(sub["t"].dt.floor("D")):
                g = g.dropna(subset=[f"net_{pol}"])
                if len(g) >= 10:
                    vals.append(g.nlargest(max(1, len(g) // 10), rk)[f"net_{pol}"].mean())
            cells.append(f"{np.mean(vals) * 100:.3f}")
        lines.append(f"| {rk} | " + " | ".join(cells) + " |")

    lines += ["", "## Cartera simulada por política (walk-forward 12 m + hold-out)", "",
              "| ranking | política | PnL USD | anual % | trades | % ganadoras | hold medio d |",
              "| --- | --- | --- | --- | --- | --- | --- |"]
    best = None
    for rk in ("heur", "p_prod", "p_new"):
        for pol in ("A", "B", "C", "H30"):
            d = sub.dropna(subset=[f"net_{pol}"]).copy()
            d["net_frac"], d["hold_days"] = d[f"net_{pol}"], d[f"hold_{pol}"]
            for gate_name, gate in (("", None), ("+pred>0", (lambda c, r=rk: c[r] > 0) if rk != "heur" else None)):
                if gate_name and gate is None:
                    continue
                r = mb.portfolio(d, rk, gate=gate)
                lines.append(f"| {rk}{gate_name} | {pol} | {r['pnl_usd']} | {r['ret_ann_pct']} | {r['trades']} | "
                             f"{r['win_pct']} | {r['avg_hold_d']} |")
                if best is None or r["pnl_usd"] > best[2]["pnl_usd"]:
                    best = (rk + gate_name, pol, r)
    lines += ["", f"**Mejor combinación:** ranking `{best[0]}` + política `{best[1]}` → "
                  f"{best[2]['pnl_usd']} USD ({best[2]['ret_ann_pct']} % anual)."]

    # Piso de rendimiento: el ranking premia consistencia y llena slots con pares
    # estables de tasa base (≈11 % APR bruto) que apenas cubren fees. ¿Rinde más
    # exigir tasa diaria mínima (vigente y/o media de la ventana)?
    lines += ["", "## Piso de rendimiento (política H30, ranking p_prod, gate pred>0)", "",
              "| piso tasa diaria (vigente y media ventana) | PnL USD | anual % | trades | % ganadoras | hold d |",
              "| --- | --- | --- | --- | --- | --- |"]
    d = sub.dropna(subset=["net_H30"]).copy()
    d["net_frac"], d["hold_days"] = d["net_H30"], d["hold_H30"]
    d["sa_daily"] = d["w_sa"] * 24.0 / d["ih"]
    for floor in (0.0, 0.0005, 0.001, 0.002, 0.003):
        r = mb.portfolio(d, "p_prod", gate=lambda c, f=floor: (c["p_prod"] > 0) & (c["daily_rate"] >= f)
                         & (c["sa_daily"] >= f))
        lines.append(f"| {floor * 100:.2f} %/día | {r['pnl_usd']} | {r['ret_ann_pct']} | {r['trades']} | "
                     f"{r['win_pct']} | {r['avg_hold_d']} |")
    for floor in (0.0005, 0.001, 0.002):
        r = mb.portfolio(d, "daily_rate", gate=lambda c, f=floor: (c["p_prod"] > 0) & (c["sa_daily"] >= f))
        lines.append(f"| rank por tasa vigente, pred>0, media ≥ {floor * 100:.2f} %/día | {r['pnl_usd']} | "
                     f"{r['ret_ann_pct']} | {r['trades']} | {r['win_pct']} | {r['avg_hold_d']} |")
    sub.to_pickle(ROOT / "cache" / "binance" / "exit_study_rows.pkl")
    path = ROOT / "reports" / f"exit_policy_{datetime.now(timezone.utc):%Y%m%d}.md"
    path.write_text("\n".join(lines), encoding="utf-8")
    print("\n".join(lines))


if __name__ == "__main__":
    main()
