#!/usr/bin/env python3
"""Modelo v3 — ¿las señales de mercado (basis, OI, top long/short, vol) mejoran el ranking? — LOCAL.

Pregunta: con ~3 meses de señales reconstruidas (scripts/signals_dataset.py), ¿un modelo
que las usa opera mejor que el vigente? Mismo universo, label y cartera que scripts/ml_v2.py
(CEX ∪ Alpha, H30, fee por venue, 5 slots/5.200 USD/1x).

Rankers comparados (todos fuera de muestra):
  vigente      models/scoring_model.joblib (pred > 0)
  E_all        HistGB 14 features, todo el historial (= ml_v2)
  E_all_sig    E_all + señales (NaN antes de la ventana de señales: HistGB las trata como
               faltantes y aprende a usarlas donde existen)
  E_win        HistGB 14 features solo con filas de la ventana de señales
  E_win_sig    E_win + señales
Walk-forward semanal dentro de la ventana (purga 16 d) + hold-out de las últimas 2 semanas
con label. Gate de export: E_*_sig supera al vigente y a su par sin señales en cartera WF
**y** hold-out, y el top-5 diario no empeora. Salida: reports/ml_v3_signals_YYYYMMDD.md y
cache/binance/ml_v3_rows.pkl (filas con señales, para scripts/analyst_study.py).

    python -u scripts/ml_v3_signals.py
"""
import bisect
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
import attribution_study as att  # noqa: E402
from _scoring_data import split_on_gaps  # noqa: E402
from analysis.ml_features import FEATURE_NAMES  # noqa: E402
from analysis.signal_features import SIGNAL_FEATURES, signal_features  # noqa: E402

SIG_CSV = ROOT / "cache" / "binance" / "signals_1h.csv"
OUT_ROWS = ROOT / "cache" / "binance" / "ml_v3_rows.pkl"
CUT = pd.Timestamp("2026-06-01", tz="UTC")       # filas nuevas desde aquí (el resto: ml_v2_rows.pkl)
HOLDOUT_DAYS = 14
PURGE = timedelta(days=mb.PURGE_DAYS + 1)


def rows_recent(fr_full: pd.DataFrame) -> pd.DataFrame:
    """Filas features+label H30 desde CUT con el dataset actualizado (ml_v2.build sobre el tramo)."""
    fr = fr_full[fr_full["funding_ts"] >= int((CUT - timedelta(days=45)).timestamp() * 1000)].copy()
    fr["captured_at"] = pd.to_datetime(fr["captured_at"], utc=True)
    df = mb.dedupe_columns(mb.build_rows(split_on_gaps(fr)))
    df = df[df["t"] >= CUT].copy()
    lt = df["symbol"].map(att.alpha_listing())
    df["alpha"] = (lt.notna() & (lt <= df["t"]) & (df["has_spot"] == 0)).astype(int)
    base_ok = (df["volume"] >= mb.MIN_VOLUME) & (df["daily_rate"] >= mb.BASE_DAILY * (1 - 1e-6))
    df = df[df["eligible"] | ((df["alpha"] == 1) & base_ok)].copy()
    df = df.merge(ml_v2.h30_labels(fr_full[["symbol", "rate", "interval_hours", "funding_ts"]]),
                  on=["symbol", "funding_ts"], how="left").dropna(subset=["fund_H30"])
    df["net_frac"] = df["fund_H30"] - np.where(df["alpha"] == 1, ml_v2.FEE_ALPHA, ml_v2.FEE_CEX)
    df["hold_days"] = df["hold_H30"]
    df["y_rate"] = (df["net_frac"] / np.maximum(df["hold_days"], 1 / 3) * 365 * 100).clip(-200, 400)
    df["y_win"] = (df["net_frac"] > 0).astype(int)
    return df


def attach_signals(df: pd.DataFrame) -> pd.DataFrame:
    sig = pd.read_csv(SIG_CSV)
    by = {s: g.sort_values("ts_hour").to_dict("records") for s, g in sig.groupby("symbol")}
    keys = {s: [r["ts_hour"] for r in rs] for s, rs in by.items()}
    feats = []
    for sym, t in zip(df["symbol"], df["funding_ts"] // 1000):
        rs = by.get(sym)
        if not rs:
            feats.append({k: None for k in SIGNAL_FEATURES})
            continue
        hi = bisect.bisect_right(keys[sym], t)
        lo = bisect.bisect_left(keys[sym], t - 80 * 3600)
        feats.append(signal_features(rs[lo:hi], int(t)))
    f = pd.DataFrame(feats, index=df.index).astype(float)
    return pd.concat([df.drop(columns=[c for c in SIGNAL_FEATURES if c in df]), f], axis=1)


VARIANTS = {
    "E_all": ("all", FEATURE_NAMES),
    "E_all_sig": ("all", FEATURE_NAMES + SIGNAL_FEATURES),
    "E_win": ("win", FEATURE_NAMES),
    "E_win_sig": ("win", FEATURE_NAMES + SIGNAL_FEATURES),
}


def train_rows(df, end, scope, win_start):
    tr = df[df["t"] < end - PURGE]
    return tr[tr["t"] >= win_start] if scope == "win" else tr


def top5(df, col, gate=None):
    """Media diaria del y_rate de las 5 filas mejor rankeadas (lo que se opera)."""
    vals = []
    for _, g in df.groupby(df["t"].dt.floor("D")):
        if gate is not None:
            g = g[gate(g)]
        g = g.sort_values(col, ascending=False).drop_duplicates("symbol").head(5)
        if len(g):
            vals.append(g["y_rate"].mean())
    return float(np.mean(vals)) if vals else float("nan")


def main():
    sys.stdout.reconfigure(encoding="utf-8")
    fr = pd.read_csv(mb.DATA)
    print(f"dataset funding hasta {pd.to_datetime(fr['funding_ts'].max(), unit='ms', utc=True):%Y-%m-%d %H:%M}",
          flush=True)
    new = rows_recent(fr)
    old = ml_v2.build(False)
    old = old[old["t"] < CUT]
    cols = [c for c in new.columns if c in old.columns]
    df = pd.concat([old[cols], new[cols]], ignore_index=True)
    print(f"filas: {len(df):,} (nuevas desde {CUT:%Y-%m-%d}: {len(new):,}, Alpha {int(new['alpha'].sum()):,})",
          flush=True)

    sig_lo = pd.to_datetime(pd.read_csv(SIG_CSV, usecols=["ts_hour"])["ts_hour"].min(), unit="s", utc=True)
    win_start = (sig_lo + timedelta(days=3)).ceil("D")            # 72 h de historia para basis_chg
    df = pd.concat([df[df["t"] < win_start].assign(**{c: np.nan for c in SIGNAL_FEATURES}),
                    attach_signals(df[df["t"] >= win_start].copy())], ignore_index=True)
    df.to_pickle(OUT_ROWS)

    import joblib
    df["p_prod"] = joblib.load(ROOT / "models" / "scoring_model.joblib")["model"].predict(
        df[FEATURE_NAMES].to_numpy())

    t_max = df["t"].max()
    hold_start = (t_max - timedelta(days=HOLDOUT_DAYS)).floor("D")
    first = (win_start + PURGE + timedelta(days=21)).floor("D")    # ≥ 3 semanas de train en "win"
    edges = list(pd.date_range(first, hold_start, freq="7D"))
    if edges[-1] < hold_start:
        edges.append(hold_start)
    folds = list(zip(edges[:-1], edges[1:]))
    print(f"ventana señales desde {win_start:%Y-%m-%d}; WF {first:%Y-%m-%d} → {hold_start:%Y-%m-%d} "
          f"({len(folds)} folds semanales); hold-out → {t_max:%Y-%m-%d}", flush=True)

    for name, (scope, fc) in VARIANTS.items():
        df[f"p_{name}"] = np.nan
        for fs, fe in folds + [(hold_start, t_max + timedelta(seconds=1))]:
            tr = train_rows(df, fs, scope, win_start)
            te = df[(df["t"] >= fs) & (df["t"] < fe)]
            if len(tr) < 2000 or not len(te):
                continue
            m = ml_v2.fit("E", tr[fc].to_numpy(), tr["y_rate"].to_numpy())
            df.loc[te.index, f"p_{name}"] = m.predict(te[fc].to_numpy())
        print(f"  {name} listo", flush=True)

    wf = df[(df["t"] >= first) & (df["t"] < hold_start)]
    hold = df[df["t"] >= hold_start]
    cov = wf[SIGNAL_FEATURES].notna().mean()
    rankers = [("vigente", "p_prod")] + [(n, f"p_{n}") for n in VARIANTS]
    res = {}
    today = datetime.now(timezone.utc).strftime("%Y%m%d")
    L = [f"# Modelo v3 — señales de mercado (3 meses) — {today}", "",
         f"Filas con label H30: {len(df):,}; ventana de señales desde {win_start:%Y-%m-%d} "
         f"({int((df['t'] >= win_start).sum()):,} filas, Alpha {int(df.loc[df['t'] >= win_start, 'alpha'].sum()):,}). "
         f"WF semanal {first:%Y-%m-%d} → {hold_start:%Y-%m-%d} ({len(folds)} folds, purga {PURGE.days} d); "
         f"hold-out {hold_start:%Y-%m-%d} → {t_max:%Y-%m-%d}. Cartera = la de ml_v2 (5 slots, 5.200 USD, 1x, H30); "
         "todos los rankers con gate pred > 0.", "",
         "Cobertura de señales en WF: " + ", ".join(f"{k} {v:.0%}" for k, v in cov.items()), "",
         "Sesgo conocido: las filas más recientes solo tienen label si salieron pronto (H30 mira 30 d) → "
         "el hold-out favorece salidas tempranas para todos los rankers por igual.", "",
         "| ranking | IC diario WF | top-5 y_rate WF | WF PnL USD | WF anual % | trades | % gan. | "
         "IC hold-out | top-5 hold-out | hold-out PnL USD | hold-out anual % | trades |",
         "| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |"]
    for lbl, col in rankers:
        gate = (lambda c, col=col: c[col] > 0)
        ic, ic_h = mb.daily_ic(wf, col, "y_rate")[0], mb.daily_ic(hold, col, "y_rate")[0]
        r, h = mb.portfolio(wf, col, gate=gate), mb.portfolio(hold, col, gate=gate)
        t5, t5h = top5(wf, col, gate), top5(hold, col, gate)
        res[lbl] = dict(ic=ic, t5=t5, wf=r, ho=h, t5h=t5h)
        L.append(f"| {lbl} | {ic:.3f} | {t5:.1f} | {r['pnl_usd']} | {r['ret_ann_pct']} | {r['trades']} | "
                 f"{r['win_pct']} | {ic_h:.3f} | {t5h:.1f} | {h['pnl_usd']} | {h['ret_ann_pct']} | {h['trades']} |")

    # Importancia por permutación del mejor modelo con señales (último fold)
    gates = {}
    for sig, base in [("E_all_sig", "E_all"), ("E_win_sig", "E_win")]:
        s, b, v = res[sig], res[base], res["vigente"]
        gates[sig] = (s["wf"]["pnl_usd"] > max(b["wf"]["pnl_usd"], v["wf"]["pnl_usd"])
                      and s["ho"]["pnl_usd"] > max(b["ho"]["pnl_usd"], v["ho"]["pnl_usd"])
                      and s["t5"] >= max(b["t5"], v["t5"]))
    L += ["", "## Gate", ""]
    for k, ok in gates.items():
        L.append(f"- `{k}`: {'PASA' if ok else 'NO pasa'} (WF y hold-out > vigente y > sin señales, top-5 no peor)")

    n_wf = res["vigente"]["wf"]["trades"]
    n_ho = res["vigente"]["ho"]["trades"]
    L += ["", "## Veredicto", "",
          (f"- Se exporta `{next(k for k, ok in gates.items() if ok)}`." if any(gates.values()) else
           "- **No se exporta.** Ningún modelo con señales gana a la vez en WF y hold-out."),
          f"- Muestra chica: ~{n_wf} trades en WF y ~{n_ho} en hold-out por ranking; una diferencia de "
          "pocas decenas de USD es ruido. Re-probar cuando la ventana de señales tenga ≥ 6 meses "
          "(re-correr signals_dataset.py con más --days; data.binance.vision guarda metrics desde 2020)."]

    win = df[df["t"] >= win_start]
    tr = train_rows(df, hold_start, "all", win_start)
    m = ml_v2.fit("E", tr[FEATURE_NAMES + SIGNAL_FEATURES].to_numpy(), tr["y_rate"].to_numpy())
    from sklearn.inspection import permutation_importance
    te = hold.dropna(subset=["y_rate"])
    if len(te) > 200:
        pi = permutation_importance(m, te[FEATURE_NAMES + SIGNAL_FEATURES].to_numpy(), te["y_rate"].to_numpy(),
                                    n_repeats=5, random_state=0, scoring="r2")
        imp = sorted(zip(FEATURE_NAMES + SIGNAL_FEATURES, pi.importances_mean), key=lambda x: -x[1])
        L += ["", "## Importancia por permutación (E_all_sig, hold-out, ΔR²)", "",
              "| feature | ΔR² |", "| --- | --- |"] + [f"| {k} | {v:.4f} |" for k, v in imp]
    L += ["", f"Filas de la ventana usadas: {len(win):,}. Reproducir: `python -u scripts/signals_dataset.py "
              "--days 105` y `python -u scripts/ml_v3_signals.py`.", ""]
    path = ROOT / "reports" / f"ml_v3_signals_{today}.md"
    path.write_text("\n".join(L), encoding="utf-8")
    print("\n".join(L))


if __name__ == "__main__":
    main()
