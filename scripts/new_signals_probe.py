#!/usr/bin/env python3
"""¿Las señales nuevas (market_feature_snapshots) aportan información que el modelo
vigente no tiene? — LOCAL, read-only. Sonda preliminar, NO es un gate de modelo.

Con ~30 d de historia no se puede entrenar/validar un modelo nuevo con el rigor del
gate (walk-forward 12 m + hold-out, scripts/ml_v2.py): el label H30 mira hasta 30 d
adelante. Aquí se mide, por día y de forma transversal (Spearman):
  IC        señal vs y7 = funding liquidado (t, t+7 d] por nocional
  IC resid  señal vs residuo de y7 tras quitar el rango del modelo vigente (p_prod)
            → información INCREMENTAL sobre el ranking actual
y un HistGB chico con/sin señales en split temporal purgado (indicativo).

Datos: cache/binance/market_features.csv (scripts/market_features_backfill.py),
cache/binance/fr_binance.csv (scripts/binance_dataset.py), cache/binance/features_v1.pkl
(scripts/ml_binance.py). Universo CEX ∪ Alpha. Salida: reports/new_signals_probe_YYYYMMDD.md
"""
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
from analysis.ml_features import FEATURE_NAMES  # noqa: E402

MF_CSV = ROOT / "cache" / "binance" / "market_features.csv"
HORIZON_D = 7
SIGNALS = ["basis_pct", "oi_change_1h_pct", "oi_change_24h_pct", "log_oi_usd", "taker_buy_sell_ratio",
           "top_long_short_pos_ratio", "volatility_7d"]


def load_signals() -> pd.DataFrame:
    mf = pd.read_csv(MF_CSV)
    mf = mf.sort_values(["symbol", "ts_hour"])
    oi = mf.groupby("symbol")["open_interest"]
    mf["oi_change_24h_pct"] = (mf["open_interest"] / oi.shift(24) - 1) * 100
    mf["log_oi_usd"] = np.log10(mf["open_interest_usd"].where(mf["open_interest_usd"] > 0))
    return mf


def y_forward(fr: pd.DataFrame, keys: pd.DataFrame) -> pd.Series:
    """Σ tasas liquidadas en (t, t+7 d] por símbolo; NaN si la serie no llega a t+7 d."""
    out = pd.Series(np.nan, index=keys.index)
    for sym, g in keys.groupby("symbol"):
        f = fr[fr["symbol"] == sym]
        if f.empty:
            continue
        ts = f["funding_ts"].to_numpy(np.int64)
        cs = np.concatenate([[0.0], np.cumsum(f["rate"].to_numpy())])
        t0 = g["funding_ts"].to_numpy(np.int64)
        t1 = t0 + HORIZON_D * 86_400_000
        ok = t1 <= ts.max()
        a = np.searchsorted(ts, t0, side="right")
        b = np.searchsorted(ts, t1, side="right")
        out.loc[g.index] = np.where(ok, cs[b] - cs[a], np.nan)
    return out


def daily_ic(df, x, y):
    ics = []
    for _, g in df.groupby(df["t"].dt.floor("D")):
        g = g[[x, y]].dropna()
        if len(g) >= 20 and g[x].nunique() > 1:
            ics.append(g[x].rank().corr(g[y].rank()))
    a = np.array([v for v in ics if not math.isnan(v)])
    if len(a) < 3:
        return float("nan"), float("nan"), len(a)
    return float(a.mean()), float(a.mean() / (a.std(ddof=1) / math.sqrt(len(a)))), len(a)


def main():
    sys.stdout.reconfigure(encoding="utf-8")
    import joblib
    mf = load_signals()
    t_lo = pd.to_datetime(mf["ts_hour"].min(), unit="s", utc=True)
    # Filas de features SIN filtrar por label H30 (ml_v2_rows lo exige → sesgo a salidas tempranas).
    import attribution_study as att
    rows = mb.dedupe_columns(pd.read_pickle(mb.FEAT_CACHE))
    rows = rows[rows["t"] >= t_lo].copy()
    lt = rows["symbol"].map(att.alpha_listing())
    alpha = lt.notna() & (lt <= rows["t"]) & (rows["has_spot"] == 0)
    base_ok = (rows["volume"] >= mb.MIN_VOLUME) & (rows["daily_rate"] >= mb.BASE_DAILY * (1 - 1e-6))
    rows = rows[rows["eligible"] | (alpha & base_ok)].copy()
    rows["p_prod"] = joblib.load(ROOT / "models" / "scoring_model.joblib")["model"].predict(
        rows[FEATURE_NAMES].to_numpy())
    rows["ts_hour"] = (rows["funding_ts"] // 3_600_000) * 3600
    d = rows.merge(mf[["symbol", "ts_hour", "spot_source", *[c for c in SIGNALS if c in mf]]],
                   on=["symbol", "ts_hour"], how="inner")
    fr = pd.read_csv(mb.DATA, usecols=["symbol", "rate", "funding_ts"]).sort_values("funding_ts")
    d["y7"] = y_forward(fr, d[["symbol", "funding_ts"]])
    d = d.dropna(subset=["y7"])
    # residuo transversal de y7 tras el rango del modelo vigente (por día)
    d["day"] = d["t"].dt.floor("D")
    d["r_prod"] = d.groupby("day")["p_prod"].rank(pct=True)
    d["y7_r"] = d.groupby("day")["y7"].rank(pct=True)
    d["resid"] = d["y7_r"] - d.groupby("day", group_keys=False).apply(
        lambda g: pd.Series(np.polyval(np.polyfit(g["r_prod"], g["y7_r"], 1), g["r_prod"]), index=g.index)
        if len(g) >= 20 else pd.Series(np.nan, index=g.index))

    today = datetime.now(timezone.utc).strftime("%Y%m%d")
    n_alpha = int((d["spot_source"] == "alpha").sum())
    L = [f"# Sonda de señales nuevas — {today}", "",
         f"Filas (settlements con señales y label y7 completo): {len(d):,} ({n_alpha:,} Alpha), "
         f"{d['t'].min():%Y-%m-%d} → {d['t'].max():%Y-%m-%d}, {d['day'].nunique()} días. "
         "y7 = funding liquidado (t, t+7 d] por nocional. IC = Spearman diario promedio (t sobre días; "
         "los labels de 7 d se solapan → t inflado, leer como orientativo).", "",
         "| señal | cobertura % | IC vs y7 | t | IC vs residuo del modelo | t | días |",
         "| --- | --- | --- | --- | --- | --- | --- |"]
    base = daily_ic(d, "p_prod", "y7")
    L.append(f"| **modelo vigente (p_prod)** | 100 | {base[0]:.3f} | {base[1]:.1f} | — | — | {base[2]} |")
    for s in SIGNALS:
        if s not in d:
            continue
        cov = d[s].notna().mean() * 100
        ic, t, n = daily_ic(d, s, "y7")
        icr, tr, _ = daily_ic(d, s, "resid")
        L.append(f"| {s} | {cov:.0f} | {ic:.3f} | {t:.1f} | {icr:.3f} | {tr:.1f} | {n} |")

    # HistGB chico con/sin señales, split temporal purgado (indicativo)
    from sklearn.ensemble import HistGradientBoostingRegressor
    days = sorted(d["day"].unique())
    cut = days[int(len(days) * 0.6)]
    tr = d[d["day"] < cut - pd.Timedelta(days=HORIZON_D)]
    te = d[d["day"] >= cut]
    L += ["", f"## HistGB chico (train < {pd.Timestamp(cut) - pd.Timedelta(days=HORIZON_D):%Y-%m-%d}, "
              f"test ≥ {pd.Timestamp(cut):%Y-%m-%d}, purga {HORIZON_D} d)", ""]
    if len(tr) < 2000 or len(te) < 500:
        L.append(f"Muy pocas filas para entrenar (train {len(tr):,}, test {len(te):,}): se omite.")
    else:
        sig = [s for s in SIGNALS if s in d]
        L += ["| modelo | IC test vs y7 |", "| --- | --- |", f"| modelo vigente | {daily_ic(te, 'p_prod', 'y7')[0]:.3f} |"]
        for lbl, cols in (("14 features + p_prod", FEATURE_NAMES + ["p_prod"]),
                          ("14 features + p_prod + señales", FEATURE_NAMES + ["p_prod"] + sig)):
            m = HistGradientBoostingRegressor(max_iter=150, learning_rate=0.05, max_leaf_nodes=15,
                                              min_samples_leaf=200)
            m.fit(tr[cols].to_numpy(), tr["y7"].to_numpy())
            te = te.assign(p=m.predict(te[cols].to_numpy()))
            L.append(f"| {lbl} | {daily_ic(te, 'p', 'y7')[0]:.3f} |")
    # Blend sin re-entrenar: rango del modelo + w · rango del basis. w se elige en la 1ª mitad
    # por el funding del top-5 diario (5 slots = lo que se opera) y se evalúa en la 2ª.
    d["r_basis"] = d.groupby("day")["basis_pct"].rank(pct=True)
    cut2 = days[len(days) // 2]
    halves = (("A (elige w)", d[d["day"] < cut2]), ("B (evalúa)", d[d["day"] >= cut2]))

    def top5(x, col):
        v = [g.nlargest(5, col)["y7"].mean() for _, g in x.groupby("day") if len(g) >= 20]
        return float(np.mean(v)) * 100 if v else float("nan")

    L += ["", f"## Blend modelo + basis (corte {pd.Timestamp(cut2):%Y-%m-%d})", "",
          "| w | " + " | ".join(f"IC {h} | top-5 y7 % {h}" for h, _ in halves) + " |",
          "| --- |" + " --- | --- |" * len(halves)]
    for w in (0.0, 0.1, 0.2, 0.3, 0.5, 1.0):
        cells = []
        for _, x in halves:
            x = x.assign(blend=x["r_prod"] + w * x["r_basis"])
            cells += [f"{daily_ic(x, 'blend', 'y7')[0]:.3f}", f"{top5(x, 'blend'):.3f}"]
        L.append(f"| {w} | " + " | ".join(cells) + " |")
    L += ["", "Lectura: una señal vale la pena si su IC sobre el **residuo** es estable y del mismo signo; "
              "aun así no se exporta un modelo hasta tener ≥ 3 meses de señales y pasar el gate de ml_v2.py.", ""]
    path = ROOT / "reports" / f"new_signals_probe_{today}.md"
    path.write_text("\n".join(L), encoding="utf-8")
    print("\n".join(L))


if __name__ == "__main__":
    main()
