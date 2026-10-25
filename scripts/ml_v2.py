#!/usr/bin/env python3
"""Modelo v2 — universo CEX ∪ Alpha, label de la política de prod (H30) — LOCAL, read-only.

Motivo (reports/attribution_20261025.md): el dataset de ml_binance.py solo marca
elegibles los pares con spot CEX, pero los trades rentables son tokens con spot
solo en Binance Alpha. Este script re-entrena y valida sobre el universo que se
opera de verdad, con el label de la política vigente (salir tras dos pagos ≤ 0,
tope 30 d) y la fee real de cada venue.

Cabezas (HistGB, sklearn pinneado):
  E    regresor de y_rate = neto por nocional / días de hold × 365 (velocidad del capital)
  Q25  regresor cuantil 0.25 de y_rate (cota inferior → "máxima probabilidad")
  P    clasificador P(neto > 0)
Variantes de ventana de entrenamiento: todo el historial vs últimos 12 / 6 meses.
Gate = cartera simulada (5 slots, 5.200 USD, 1x) en walk-forward Y en hold-out
contra el modelo vigente y contra "tasa vigente". Salida: reports/ml_v2_YYYYMMDD.md.
"""
import argparse
import math
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))
import ml_binance as mb  # noqa: E402
import attribution_study as att  # noqa: E402
from analysis.ml_features import FEATURE_NAMES  # noqa: E402

ROWS_CACHE = ROOT / "cache" / "binance" / "ml_v2_rows.pkl"
FEE_CEX = mb.FEE
FEE_ALPHA = 0.0041          # mediana real fees/nocional de los trades Binance (todos Alpha)
MAX_HOLD_DAYS = 30
NEG_NEEDED = 2              # = CLOSE_SUGGESTED
EXTRA = ["alpha", "regime_median_daily", "regime_frac_premium"]
WINDOWS = {"all": None, "12m": 365, "6m": 182}


# ── Label H30 vectorizado ───────────────────────────────────────────────────
def h30_labels(fr: pd.DataFrame) -> pd.DataFrame:
    """Para cada liquidación i: funding acumulado de i+1 hasta la salida (segundo pago
    seguido ≤ 0, tope 30 d) y días de hold. NaN si la serie termina antes."""
    out = []
    for sym, g in fr.groupby("symbol"):
        g = g.sort_values("funding_ts")
        r = g["rate"].to_numpy(float)
        ih = np.maximum(1.0, g["interval_hours"].to_numpy(float))
        n = len(r)
        cs = np.concatenate([[0.0], np.cumsum(r)])               # cs[k] = Σ r[0..k-1]
        neg = r <= 0
        pair_end = np.zeros(n, bool)
        pair_end[1:] = neg[1:] & neg[:-1]
        # next_pair[k] = menor m ≥ k con pair_end[m]; n si no hay
        idx = np.where(pair_end, np.arange(n), n)
        next_pair = np.minimum.accumulate(idx[::-1])[::-1]
        i = np.arange(n)
        max_int = np.rint(MAX_HOLD_DAYS * 24 / ih).astype(int)
        first = np.minimum(i + 2, n - 1)
        exit_pair = np.where(i + 2 <= n - 1, next_pair[first], n)
        exit_ = np.minimum(exit_pair, i + max_int)
        ok = exit_ <= n - 1
        e = np.where(ok, exit_, 0)
        fund = np.where(ok, cs[e + 1] - cs[i + 1], np.nan)
        held = np.where(ok, (e - i) * ih / 24, np.nan)
        out.append(pd.DataFrame({"symbol": sym, "funding_ts": g["funding_ts"].to_numpy(np.int64),
                                 "fund_H30": fund, "hold_H30": held}))
    return pd.concat(out, ignore_index=True)


def build(refresh: bool) -> pd.DataFrame:
    if ROWS_CACHE.exists() and not refresh:
        return pd.read_pickle(ROWS_CACHE)
    lst = att.alpha_listing()
    df = mb.dedupe_columns(pd.read_pickle(mb.FEAT_CACHE))
    lt = df["symbol"].map(lst)
    df["alpha"] = (lt.notna() & (lt <= df["t"]) & (df["has_spot"] == 0)).astype(int)
    base_ok = (df["volume"] >= mb.MIN_VOLUME) & (df["daily_rate"] >= mb.BASE_DAILY * (1 - 1e-6))
    df = df[df["eligible"] | ((df["alpha"] == 1) & base_ok)].copy()
    fr = pd.read_csv(mb.DATA, usecols=["symbol", "rate", "interval_hours", "funding_ts"])
    df = df.merge(h30_labels(fr), on=["symbol", "funding_ts"], how="left")
    df = df.dropna(subset=["fund_H30"])
    df["net_frac"] = df["fund_H30"] - np.where(df["alpha"] == 1, FEE_ALPHA, FEE_CEX)
    df["hold_days"] = df["hold_H30"]
    df["y_rate"] = (df["net_frac"] / np.maximum(df["hold_days"], 1 / 3) * 365 * 100).clip(-200, 400)
    df["y_win"] = (df["net_frac"] > 0).astype(int)
    df.to_pickle(ROWS_CACHE)
    return df


# ── Modelos ─────────────────────────────────────────────────────────────────
def fit(head: str, X, y):
    from sklearn.ensemble import HistGradientBoostingClassifier, HistGradientBoostingRegressor
    if head == "P":
        m = HistGradientBoostingClassifier(**mb.MODEL_PARAMS)
    elif head == "Q25":
        m = HistGradientBoostingRegressor(loss="quantile", quantile=0.25, **mb.MODEL_PARAMS)
    else:
        m = HistGradientBoostingRegressor(**mb.MODEL_PARAMS)
    m.fit(X, y)
    return m


def predict(m, head, X):
    return m.predict_proba(X)[:, 1] if head == "P" else m.predict(X)


VARIANTS = {   # nombre: (cabeza, ventana, columnas)
    "E_all": ("E", "all", FEATURE_NAMES),
    "E_12m": ("E", "12m", FEATURE_NAMES),
    "E_6m": ("E", "6m", FEATURE_NAMES),
    "E_all_x": ("E", "all", FEATURE_NAMES + EXTRA),
    "Q25_all": ("Q25", "all", FEATURE_NAMES),
    "P_all": ("P", "all", FEATURE_NAMES),
}


def train_slice(df, end, window):
    tr = df[df["t"] < end - timedelta(days=mb.PURGE_DAYS + 1)]
    if WINDOWS[window]:
        tr = tr[tr["t"] >= end - timedelta(days=WINDOWS[window])]
    return tr


def run(df, name, folds):
    head, window, cols = VARIANTS[name]
    y = "y_win" if head == "P" else "y_rate"
    preds = pd.Series(np.nan, index=df.index)
    for fs, fe in folds:
        tr = train_slice(df, fs, window)
        te = df[(df["t"] >= fs) & (df["t"] < fe)]
        if len(tr) < 5000 or len(te) < 300:
            continue
        m = fit(head, tr[cols].to_numpy(), tr[y].to_numpy())
        preds.loc[te.index] = predict(m, head, te[cols].to_numpy())
    return preds


def max_drawdown_pct(df, col, gate=None):
    """DD de la curva de PnL realizado por fecha de salida (aprox. de la cartera)."""
    import ml_binance
    d = df.sort_values("t")
    notional = ml_binance.CAPITAL / ml_binance.SLOTS / 2
    open_pos, events = [], []
    for t, g in d.groupby(d["t"].dt.floor("8h")):
        open_pos = [p for p in open_pos if p > t]
        free = ml_binance.SLOTS - len(open_pos)
        if free <= 0:
            continue
        cand = g.sort_values(col, ascending=False).drop_duplicates("symbol")
        if gate is not None:
            cand = cand[gate(cand)]
        for r in cand.head(free).itertuples(index=False):
            ex = r.t + timedelta(days=float(r.hold_days))
            open_pos.append(ex)
            events.append((ex, r.net_frac * notional))
    if not events:
        return 0.0
    s = pd.Series([e[1] for e in events], index=[e[0] for e in events]).sort_index().cumsum()
    eq = ml_binance.CAPITAL + s
    return float(((eq.cummax() - eq) / eq.cummax()).max() * 100)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--refresh", action="store_true", help="recalcular filas/labels")
    a = ap.parse_args()
    sys.stdout.reconfigure(encoding="utf-8")
    df = build(a.refresh)
    print(f"filas CEX ∪ Alpha con label H30: {len(df):,} (Alpha {int(df['alpha'].sum()):,})", flush=True)

    t_max = df["t"].max()
    holdout_start = (t_max - timedelta(days=mb.HOLDOUT_DAYS)).floor("D")
    ms = pd.date_range(end=holdout_start, periods=mb.WF_MONTHS + 1, freq="MS", tz="UTC")
    folds = [(ms[k], ms[k + 1]) for k in range(mb.WF_MONTHS)]
    folds[-1] = (folds[-1][0], holdout_start)

    import joblib
    bundle = joblib.load(ROOT / "models" / "scoring_model.joblib")
    df["p_prod"] = bundle["model"].predict(df[FEATURE_NAMES].to_numpy())

    for name in VARIANTS:
        print(f"walk-forward {name}...", flush=True)
        df[f"p_{name}"] = run(df, name, folds)
    df["p_ExP"] = df["p_E_all"] * df["p_P_all"]           # ganancia esperada × probabilidad

    wf = df[(df["t"] >= folds[0][0]) & (df["t"] < holdout_start)].dropna(subset=[f"p_{n}" for n in VARIANTS])
    hold = df[df["t"] >= holdout_start].copy()
    for name in VARIANTS:                                   # hold-out: entrenar con todo lo previo
        head, window, cols = VARIANTS[name]
        tr = train_slice(df, holdout_start, window)
        m = fit(head, tr[cols].to_numpy(), tr["y_win" if head == "P" else "y_rate"].to_numpy())
        hold[f"p_{name}"] = predict(m, head, hold[cols].to_numpy())
    hold["p_ExP"] = hold["p_E_all"] * hold["p_P_all"]

    rankers = [("modelo vigente (pred>0)", "p_prod", lambda c: c["p_prod"] > 0),
               ("tasa vigente", "daily_rate", None),
               *[(f"{n} (>0)" if not n.startswith("P") else f"{n} (≥0.5)", f"p_{n}",
                  (lambda c, n=n: c[f"p_{n}"] >= 0.5) if n.startswith("P") else (lambda c, n=n: c[f"p_{n}"] > 0))
                 for n in VARIANTS],
               ("E×P (>0)", "p_ExP", lambda c: c["p_ExP"] > 0)]

    today = datetime.now(timezone.utc).strftime("%Y%m%d")
    L = [f"# Modelo v2 — universo CEX ∪ Alpha, label H30 — {today}", "",
         f"Filas con label: {len(df):,} ({int(df['alpha'].sum()):,} Alpha), {df['t'].min():%Y-%m-%d} → "
         f"{t_max:%Y-%m-%d}. Walk-forward mensual {folds[0][0]:%Y-%m} → {holdout_start:%Y-%m-%d} "
         f"(purga {mb.PURGE_DAYS + 1} d), hold-out intocado desde {holdout_start:%Y-%m-%d}. Fee CEX "
         f"{FEE_CEX * 100:.2f} %, Alpha {FEE_ALPHA * 100:.2f} %. Cartera 5 slots, 5.200 USD, 1x, salida H30.",
         "", "Variantes probadas: " + ", ".join(VARIANTS) + ", E×P (registro completo para el sesgo de selección).",
         "", "## Walk-forward (12 meses) y hold-out (30 d)", "",
         "| ranking | IC diario WF (y_rate) | WF PnL USD | WF anual % | trades | % gan. | DD % | "
         "hold-out PnL USD | hold-out anual % | hold-out trades |",
         "| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |"]
    res = {}
    for lbl, col, gate in rankers:
        ic = mb.daily_ic(wf, col, "y_rate")[0]
        r = mb.portfolio(wf, col, gate=gate)
        dd = max_drawdown_pct(wf, col, gate)
        h = mb.portfolio(hold, col, gate=gate)
        res[lbl] = (r, h)
        L.append(f"| {lbl} | {ic:.3f} | {r['pnl_usd']} | {r['ret_ann_pct']} | {r['trades']} | {r['win_pct']} | "
                 f"{dd:.1f} | {h['pnl_usd']} | {h['ret_ann_pct']} | {h['trades']} |")

    # PnL mensual del mejor ranking en WF (¿cuántos meses superan 5 %?)
    best = max((k for k in res if k != "tasa vigente"), key=lambda k: res[k][0]["pnl_usd"])
    col, gate = next((c, g) for l_, c, g in rankers if l_ == best)
    L += ["", f"## Retorno mensual del mejor ranking en WF: `{best}` (1x, sobre 5.200 USD)", "",
          "| mes | PnL USD (por fecha de entrada) | % del capital |", "| --- | --- | --- |"]
    d = wf.copy()
    d["m"] = d["t"].dt.strftime("%Y-%m")
    for m_, g in d.groupby("m"):
        r = mb.portfolio(g, col, gate=gate)
        L.append(f"| {m_} | {r['pnl_usd']} | {r['pnl_usd'] / mb.CAPITAL * 100:.2f} |")
    L += ["", "Nota: la cartera por mes reinicia slots cada mes (aprox.); a 3x el nocional por slot sube "
              "×1.5 antes del costo de eventos de SL (ver F4).", ""]

    path = ROOT / "reports" / f"ml_v2_{today}.md"
    path.write_text("\n".join(L), encoding="utf-8")
    print("\n".join(L))


if __name__ == "__main__":
    main()
