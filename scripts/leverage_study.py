#!/usr/bin/env python3
"""¿Conviene operar spot_perp a 2x o 3x en vez de 1x? — LOCAL, read-only.

A más leverage el short usa menos margen (capital C → nocional C·L/(L+1)), pero el
seguro (SL al 80 % del camino a liquidación + venta límite del spot) se dispara
más seguido y corta la posición. Este estudio mete esos eventos en la cartera:

  * entradas = filas Alpha de cache/binance/ml_v2_rows.pkl (scripts/ml_v2.py) de
    los símbolos con velas 1m en cache/binance/klines1m (protection_study.py --venue bsc);
  * por entrada y leverage: primer minuto en que el mark toca el SL o el spot cruza
    la venta límite (δ = 0.5 %, configuración de prod) antes de la salida H30;
  * si hay evento: hold = hasta el evento, funding cobrado hasta ahí, fee de ida y
    vuelta y costo del evento (caso A: spot desnudo vendido a mercado 10 s después;
    B: short recomprado 60 s después; AB: ambas piernas en el SL);
  * cartera 5 slots / 5.200 USD como ml_binance.portfolio, con nocional ×2L/(L+1)
    respecto de 1x. El slot liberado por un evento se re-usa en la siguiente decisión.
Salida: reports/leverage_study_YYYYMMDD.md
"""
import sys
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))
import ml_binance as mb  # noqa: E402
import ml_v2  # noqa: E402
import protection_study as ps  # noqa: E402

LEVERAGES = (1, 2, 3)
DELTA = 0.005
MONTHS = [str(p) for p in pd.period_range("2025-10", "2026-09", freq="M")]


def symbols_with_klines(venue: str) -> list:
    d = ps.KL_DIR / ("alpha" if venue == "bsc" else "spot")
    return sorted({p.name.split("-")[0] for p in d.glob("*.npz")}) if d.exists() else []


def first_event(d: dict, i: int, end: int, lev: int, venue: str = "bsc"):
    """(minuto, costo % del precio de entrada) del primer seguro disparado, o None."""
    e = d["perp_c"][i]
    sl = e + ps.FRACTION * (e * (1 / lev - ps.MMR))
    sp = sl * (1 - DELTA)
    tp = ps._first(d["mark_h"][i + 1:end] >= sl)
    ts = ps._first(d["spot_h"][i + 1:end] > sp)
    tp = tp + i + 1 if tp >= 0 else -1
    ts = ts + i + 1 if ts >= 0 else -1
    if tp < 0 and ts < 0:
        return None
    perp_fill = lambda k: max(sl, d["perp_o"][k]) * (1 + ps.SLIP)   # noqa: E731
    spot_entry = d["spot_c"][i]
    if ts >= 0 and (tp < 0 or ts < tp):                  # B: spot vendido, short recomprado a 60 s
        k = min(ts + 1, end - 1)
        hit = ps._first(d["mark_h"][ts + 1:k + 1] >= sl) if k > ts else -1
        buy = perp_fill(ts + 1 + hit) if hit >= 0 else d["perp_c"][k] * (1 + ps.SLIP)
        price_pnl = (sp - spot_entry) + (e - buy)
        return ts, price_pnl / e
    if tp >= 0 and (ts < 0 or tp < ts):                  # A: short cerrado, spot vendido a 10 s
        buy = perp_fill(tp)
        sell = d["spot_c"][min(tp, end - 1)] * (1 - ps.SPOT_SLIP[venue])
        return tp, ((sell - spot_entry) + (e - buy)) / e
    buy = perp_fill(tp)                                   # AB
    return tp, ((sp - spot_entry) + (e - buy)) / e


def main():
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--venue", choices=("bsc", "binance"), default="bsc",
                    help="bsc = tokens solo-Alpha; binance = pares con spot CEX")
    venue = ap.parse_args().venue
    sys.stdout.reconfigure(encoding="utf-8")
    syms = symbols_with_klines(venue)
    fee = ml_v2.FEE_ALPHA if venue == "bsc" else ml_v2.FEE_CEX
    rows = pd.read_pickle(ml_v2.ROWS_CACHE)
    rows = rows[(rows["alpha"] == (1 if venue == "bsc" else 0)) & rows["symbol"].isin(syms)
                & (rows["t"] >= pd.Timestamp(MONTHS[0] + "-01", tz="UTC"))
                & (rows["t"] < pd.Timestamp("2026-10-01", tz="UTC"))].copy()
    import joblib
    from analysis.ml_features import FEATURE_NAMES
    rows["p_prod"] = joblib.load(ROOT / "models" / "scoring_model.joblib")["model"].predict(
        rows[FEATURE_NAMES].to_numpy())
    fr = pd.read_csv(mb.DATA, usecols=["symbol", "rate", "funding_ts"])
    fr = fr[fr["symbol"].isin(syms)]
    print(f"{len(syms)} símbolos con velas, {len(rows):,} entradas ({venue})", flush=True)

    out = {lev: [] for lev in LEVERAGES}
    if venue == "bsc":
        ps.ALPHA_IDS.update(ps._alpha_bsc())      # completa meses faltantes de la caché
    for sym, g in rows.groupby("symbol"):
        try:
            df = ps.load_symbol(sym, MONTHS, venue=venue)
        except Exception as e:                    # token fuera de la lista Alpha actual
            print(f"  {sym}: sin velas ({e})", flush=True)
            continue
        if df is None:
            continue
        d = {k: df[k].to_numpy() for k in df.columns}
        idx_ts = df.index.to_numpy(np.int64)              # ms por minuto
        f = fr[fr["symbol"] == sym].sort_values("funding_ts")
        f_ts, f_cs = f["funding_ts"].to_numpy(np.int64), np.concatenate([[0.0], np.cumsum(f["rate"].to_numpy())])
        for r in g.itertuples(index=False):
            i = int(np.searchsorted(idx_ts, r.funding_ts))
            if i >= len(idx_ts) - 2:
                continue
            end_ms = r.funding_ts + int(r.hold_days * 86400_000)
            end = min(len(idx_ts), int(np.searchsorted(idx_ts, end_ms)) + 1)
            for lev in LEVERAGES:
                ev = first_event(d, i, end, lev, venue) if end > i + 1 else None
                if ev is None:
                    net, hold, evt = r.net_frac, r.hold_days, 0
                else:
                    k, cost = ev
                    t_ev = idx_ts[k]
                    fund = f_cs[np.searchsorted(f_ts, t_ev, side="right")] - f_cs[np.searchsorted(f_ts, r.funding_ts, side="right")]
                    net = fund - fee + cost
                    hold, evt = max((t_ev - r.funding_ts) / 86400_000, 1 / 24), 1
                out[lev].append({"symbol": sym, "t": r.t, "funding_ts": r.funding_ts, "p_prod": r.p_prod,
                                 "daily_rate": r.daily_rate, "net_frac": net * 2 * lev / (lev + 1),
                                 "hold_days": hold, "event": evt})

    today = datetime.now(timezone.utc).strftime("%Y%m%d")
    L = [f"# Estudio de apalancamiento con seguros en la cartera — {today}", "",
         f"Universo: {len(syms)} tokens {'Alpha' if venue == 'bsc' else 'con spot CEX'} con velas 1m ({', '.join(syms)}), {MONTHS[0]} → {MONTHS[-1]}. "
         f"SL al {ps.FRACTION:.0%} del camino a liquidación (mmr {ps.MMR:.0%}), venta spot δ={DELTA:.1%}, "
         f"fee {fee:.2%}, salida H30. Cartera 5 slots, 5.200 USD; el nocional por slot es "
         "C/5·L/(L+1). PnL en USD sobre el capital total.", "",
         "| leverage | ranking | PnL USD | anual % | trades | % ganadoras | hold d | % trades con evento | DD % |",
         "| --- | --- | --- | --- | --- | --- | --- | --- | --- |"]
    for lev in LEVERAGES:
        df = pd.DataFrame(out[lev])
        for lbl, col, gate in (("modelo vigente (pred>0)", "p_prod", lambda c: c["p_prod"] > 0),
                               ("tasa vigente", "daily_rate", None)):
            r = mb.portfolio(df, col, gate=gate)
            dd = ml_v2.max_drawdown_pct(df, col, gate)
            sel = df[gate(df)] if gate else df
            L.append(f"| {lev}x | {lbl} | {r['pnl_usd']} | {r['ret_ann_pct']} | {r['trades']} | {r['win_pct']} | "
                     f"{r['avg_hold_d']} | {sel['event'].mean() * 100:.0f} (universo) | {dd:.1f} |")
    L += ["", "Lectura: comparar filas del mismo ranking. El universo es chico (sesgo de supervivencia de la "
              "lista Alpha y de los 20 tokens de mayor carry), así que la cifra absoluta es optimista; la "
              "diferencia relativa entre leverages es lo que decide.", ""]
    path = ROOT / "reports" / f"leverage_study_{today}{'_bsc' if venue == 'bsc' else '_cex'}.md"
    path.write_text("\n".join(L), encoding="utf-8")
    print("\n".join(L))


if __name__ == "__main__":
    main()
