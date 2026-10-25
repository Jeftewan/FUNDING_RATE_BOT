#!/usr/bin/env python3
"""¿Conviene ROTAR posiciones (cerrar A para abrir B) o mantener hasta la regla de salida?
— LOCAL, read-only.

Cartera 5 slots / 5.200 USD / 1x sobre el universo CEX ∪ Alpha (cache/binance/ml_v2_rows.pkl,
scripts/ml_v2.py), ranking = modelo vigente con gate pred > 0, salida H30 (dos pagos ≤ 0, tope 30 d).
Cada 8 h, además de llenar slots libres, se evalúa cambiar la posición de menor predicción por la
mejor candidata no tenida si

    (pred_B − pred_A_ajustada) / 365 × HORIZONTE  >  fee_B + margen

pred en % anual neto sobre el nocional (el label del modelo descuenta la fee de una entrada nueva;
a la posición ya abierta se le devuelve esa fee porque ya la pagó). Al rotar, A realiza el funding
cobrado hasta ese momento menos su fee; B empieza con su propia salida H30.
Salida: reports/rotation_policy_YYYYMMDD.md
"""
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

HORIZON_DAYS = 14                   # horizonte del label del modelo vigente
MARGINS = (None, 0.0, 0.002, 0.005, 0.01, 0.02, 0.03, 0.05)   # None = nunca rotar


def simulate(df: pd.DataFrame, fund: dict, margin):
    notional = mb.CAPITAL / mb.SLOTS / 2
    pos, pnl, trades, wins, rotations, fees = [], 0.0, 0, 0, 0, 0.0

    def fee_of(r):
        return ml_v2.FEE_ALPHA if r.alpha == 1 else ml_v2.FEE_CEX

    def realized(p, t_ms):
        ts, cs = fund[p["sym"]]
        a = np.searchsorted(ts, p["ts"], side="right")
        b = np.searchsorted(ts, t_ms, side="right")
        return cs[b] - cs[a] - p["fee"]

    for t, g in df.groupby(df["t"].dt.floor("8h")):
        g = g.drop_duplicates("symbol", keep="last").set_index("symbol")
        t_ms = int(t.timestamp() * 1000)
        still = []
        for p in pos:                               # salidas por la regla H30
            if p["exit"] <= t:
                pnl += p["net"] * notional
                trades += 1
                wins += p["net"] > 0
            else:
                still.append(p)
        pos = still
        cand = g[g["p_prod"] > 0].sort_values("p_prod", ascending=False)
        held = {p["sym"] for p in pos}
        free = mb.SLOTS - len(pos)
        for sym, r in cand.iterrows():              # llenar slots libres
            if free <= 0:
                break
            if sym in held:
                continue
            pos.append({"sym": sym, "ts": r.funding_ts, "exit": r.t + timedelta(days=float(r.hold_days)),
                        "net": r.net_frac, "fee": fee_of(r)})
            fees += fee_of(r) * notional
            held.add(sym)
            free -= 1
        if margin is None or free > 0 or not pos:
            continue
        # rotación: peor posición tenida vs mejor candidata no tenida
        best = next(((s, r) for s, r in cand.iterrows() if s not in held), None)
        if best is None:
            continue
        def cur_pred(p):
            return g.loc[p["sym"], "p_prod"] + p["fee"] * 365 / HORIZON_DAYS * 100 if p["sym"] in g.index else -1e9
        worst = min(pos, key=cur_pred)
        s_b, r_b = best
        gain = (r_b.p_prod - cur_pred(worst)) / 100 / 365 * HORIZON_DAYS
        if gain > fee_of(r_b) + margin:
            net_a = realized(worst, t_ms)
            pnl += net_a * notional
            trades += 1
            wins += net_a > 0
            rotations += 1
            pos.remove(worst)
            pos.append({"sym": s_b, "ts": r_b.funding_ts, "exit": r_b.t + timedelta(days=float(r_b.hold_days)),
                        "net": r_b.net_frac, "fee": fee_of(r_b)})
            fees += fee_of(r_b) * notional
    span = max(1.0, (df["t"].max() - df["t"].min()).total_seconds() / 86400)
    return {"pnl": round(pnl, 1), "ann": round(pnl / mb.CAPITAL / span * 365 * 100, 1), "trades": trades,
            "win": round(100 * wins / trades, 1) if trades else 0, "rot": rotations, "fees": round(fees, 1)}


def main():
    sys.stdout.reconfigure(encoding="utf-8")
    import joblib
    from analysis.ml_features import FEATURE_NAMES
    df = pd.read_pickle(ml_v2.ROWS_CACHE)
    t_max = df["t"].max()
    df = df[df["t"] >= t_max - timedelta(days=mb.HOLDOUT_DAYS) - pd.DateOffset(months=mb.WF_MONTHS)].copy()
    df["p_prod"] = joblib.load(ROOT / "models" / "scoring_model.joblib")["model"].predict(
        df[FEATURE_NAMES].to_numpy())
    fr = pd.read_csv(mb.DATA, usecols=["symbol", "rate", "funding_ts"])
    fr = fr[fr["symbol"].isin(set(df["symbol"]))].sort_values("funding_ts")
    fund = {s: (g["funding_ts"].to_numpy(np.int64), np.concatenate([[0.0], np.cumsum(g["rate"].to_numpy())]))
            for s, g in fr.groupby("symbol")}
    today = datetime.now(timezone.utc).strftime("%Y%m%d")
    L = [f"# Política de rotación entre posiciones — {today}", "",
         f"Universo CEX ∪ Alpha, {df['t'].min():%Y-%m-%d} → {t_max:%Y-%m-%d}; 5 slots, 5.200 USD, 1x, salida H30, "
         f"ranking modelo vigente (pred > 0). Rotar si Δpred·{HORIZON_DAYS}d > fee de entrada + margen.", "",
         "| política | PnL USD | anual % | trades | % ganadoras | rotaciones | fees pagados USD |",
         "| --- | --- | --- | --- | --- | --- | --- |"]
    for m in MARGINS:
        r = simulate(df, fund, m)
        lbl = "nunca rotar" if m is None else f"rotar, margen {m * 100:.1f} %"
        print(lbl, r, flush=True)
        L.append(f"| {lbl} | {r['pnl']} | {r['ann']} | {r['trades']} | {r['win']} | {r['rot']} | {r['fees']} |")
    path = ROOT / "reports" / f"rotation_policy_{today}.md"
    path.write_text("\n".join(L), encoding="utf-8")
    print("\n".join(L))


if __name__ == "__main__":
    main()
