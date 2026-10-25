#!/usr/bin/env python3
"""Atribución: ¿por qué la ganancia REALIZADA (~29 % anual) no se parece a la
SIMULADA (~3.7 % anual, reports/exit_policy_20261022.md)? — LOCAL, read-only.

Reproduce cada cierre real de `user_history` sobre el dataset de Binance
(cache/binance/fr_binance.csv + cache/binance/exit_study_rows.pkl) y descompone
la brecha en: venue (trades fuera de Binance), periodo, tamaño de posición,
fees, selección (rango del par en el ranking del simulador al entrar) y salida.

Uso:
  python scripts/attribution_study.py --refresh   # re-lee user_history (DATABASE_URL en .env)
  python scripts/attribution_study.py             # usa cache/real_trades.pkl
Salida: reports/attribution_YYYYMMDD.md
"""
import argparse
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
import ml_binance as mb  # noqa: E402

TRADES = ROOT / "cache" / "real_trades.pkl"
ROWS = ROOT / "cache" / "binance" / "exit_study_rows.pkl"
UROWS = ROOT / "cache" / "binance" / "universe_rows.pkl"
ALPHA_LIST_URL = ("https://www.binance.com/bapi/defi/v1/public/wallet-direct/buw/"
                  "wallet/cex/alpha/all/token/list")
MIN_TEST_CAPITAL = 50.0       # posiciones de prueba de auto-ejecución (18–30 USD)
PRICE_ANOMALY_FRAC = 0.10      # |price_pnl| > 10 % del capital en un hedge 1x → revisar


def load_trades(refresh: bool) -> pd.DataFrame:
    if refresh or not TRADES.exists():
        from dotenv import load_dotenv
        from sqlalchemy import create_engine, text
        load_dotenv(ROOT / ".env")
        url = os.environ.get("DATABASE_URL", "")
        if not url:
            sys.exit("ERROR: DATABASE_URL no está configurada.")
        url = url.replace("postgres://", "postgresql+psycopg2://", 1)
        if url.startswith("postgresql://"):
            url = url.replace("postgresql://", "postgresql+psycopg2://", 1)
        with create_engine(url).connect() as c:
            h = pd.read_sql(text(
                "select id, user_id, symbol, exchange, mode, capital_used, exposure, leverage, hours, "
                "payment_count, earned, fees, net_earned, price_pnl, reason, closed_at from user_history"), c)
        TRADES.parent.mkdir(parents=True, exist_ok=True)
        h.to_pickle(TRADES)
    h = pd.read_pickle(TRADES)
    h["closed_at"] = pd.to_datetime(h["closed_at"], utc=True)
    h["open_at"] = h["closed_at"] - pd.to_timedelta(h["hours"], unit="h")
    h["days"] = h["hours"] / 24
    h["funding_net"] = h["earned"] - h["fees"]
    return h.sort_values("open_at").reset_index(drop=True)


def ann(x, capital, days):
    return x / capital / max(days, 1 / 24) * 365 * 100


def alpha_listing() -> dict:
    """{símbolo: listingTime (UTC)} de la lista pública de Binance Alpha (todas las cadenas,
    incluidos offline). Sesgo de supervivencia: los tokens borrados de la lista no aparecen."""
    import json
    import requests
    path = ROOT / "cache" / "binance" / "alpha_tokens.json"
    if not path.exists():
        r = requests.get(ALPHA_LIST_URL, timeout=30)
        r.raise_for_status()
        path.write_text(json.dumps(r.json().get("data") or []), encoding="utf-8")
    out = {}
    for t in json.loads(path.read_text(encoding="utf-8")):
        s = (t.get("symbol") or "").upper()
        lt = pd.to_datetime(int(t.get("listingTime") or 0), unit="ms", utc=True)
        if s and (s not in out or lt < out[s]):
            out[s] = lt
    return out


def universe_rows(t0, t1) -> pd.DataFrame:
    """Filas del dataset en [t0, t1] con columna `alpha` (spot solo en Binance Alpha al momento)
    y `elig_u` = elegible con spot CEX **o** Alpha; ranking del modelo vigente y labels por política."""
    import joblib
    import exit_policy_study as eps
    from analysis.ml_features import FEATURE_NAMES
    lst = alpha_listing()
    df = mb.dedupe_columns(pd.read_pickle(mb.FEAT_CACHE))
    df = df[(df["t"] >= t0) & (df["t"] <= t1)].copy()
    lt = df["symbol"].map(lst)
    df["alpha"] = lt.notna() & (lt <= df["t"]) & (df["has_spot"] == 0)
    base_ok = (df["volume"] >= mb.MIN_VOLUME) & (df["daily_rate"] >= mb.BASE_DAILY * (1 - 1e-6))
    df["elig_u"] = df["eligible"] | (df["alpha"] & base_ok)
    df = df[df["elig_u"]].copy()
    bundle = joblib.load(ROOT / "models" / "scoring_model.joblib")
    df["p_prod"] = bundle["model"].predict(df[FEATURE_NAMES].to_numpy())
    fr = pd.read_csv(mb.DATA, usecols=["symbol", "rate", "interval_hours", "funding_ts"])
    labs = eps.policy_labels(fr, df[["symbol", "funding_ts"]])
    return df.merge(labs, on=["symbol", "funding_ts"], how="left")


def entry_context(rows: pd.DataFrame, by_slot: dict, sym: str, t_open) -> dict:
    """Fila del simulador en el último settlement ≤ apertura y su rango en ese slot de 8 h."""
    slot = t_open.floor("8h")
    g = by_slot.get(slot)
    out = {"in_dataset": False}
    if g is None:
        return out
    g = g.drop_duplicates("symbol", keep="last")
    r = g[g["symbol"] == sym]
    if r.empty:
        return out
    r = r.iloc[0]
    n = len(g)
    out.update({
        "in_dataset": True, "eligible": "Alpha" if r["alpha"] else "CEX", "n_slot": n,
        "rank_p_prod": int((g["p_prod"] > r["p_prod"]).sum()) + 1,
        "rank_rate": int((g["daily_rate"] > r["daily_rate"]).sum()) + 1,
        "rank_heur": int((g["heur"] > r["heur"]).sum()) + 1 if pd.notna(r["heur"]) else None,
        "daily_rate_pct": r["daily_rate"] * 100, "p_prod": r["p_prod"],
        "sim_net_H30": r["net_H30"], "sim_hold_H30": r["hold_H30"],
        "sim_net_B": r["net_B"], "sim_hold_B": r["hold_B"],
    })
    return out


def realized_funding_frac(fr: pd.DataFrame, sym: str, t0, t1) -> float:
    """Funding liquidado (por nocional) en (t0, t1] según el dataset público."""
    g = fr[(fr["symbol"] == sym) & (fr["t"] > t0) & (fr["t"] <= t1)]
    return float(g["rate"].sum()) if len(g) else np.nan


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--refresh", action="store_true")
    args = ap.parse_args()

    h = load_trades(args.refresh)
    if args.refresh or not UROWS.exists():
        ref = pd.read_pickle(ROWS)["t"]                 # mismo periodo que exit_policy_study
        universe_rows(ref.min(), ref.max()).to_pickle(UROWS)
    rows = pd.read_pickle(UROWS)
    by_slot = {k: g for k, g in rows.groupby(rows["t"].dt.floor("8h"))}
    fr = pd.read_csv(mb.DATA, usecols=["symbol", "rate", "funding_ts"])
    fr["t"] = pd.to_datetime(fr["funding_ts"], unit="ms", utc=True)

    test = h["capital_used"] < MIN_TEST_CAPITAL
    anomaly = (h["price_pnl"].abs() > PRICE_ANOMALY_FRAC * h["capital_used"]) & ~test
    real = h[~test].copy()

    recs = []
    for r in real.itertuples(index=False):
        ctx = entry_context(rows, by_slot, r.symbol, r.open_at) if r.exchange == "Binance" else {"in_dataset": False}
        f_ds = realized_funding_frac(fr, r.symbol, r.open_at, r.closed_at) if r.exchange == "Binance" else np.nan
        recs.append({**r._asdict(), **ctx, "ds_funding_frac": f_ds,
                     "rec_funding_frac": r.earned / r.exposure if r.exposure else np.nan,
                     "roc_ann": ann(r.funding_net, r.capital_used, r.days),
                     "anomaly": bool(anomaly.loc[h["id"] == r.id].iloc[0])})
    t = pd.DataFrame(recs)

    L = [f"# Atribución realizado vs simulado — {datetime.now(timezone.utc):%Y-%m-%d}", "",
         f"Cierres en `user_history`: {len(h)} · pruebas excluidas (capital < {MIN_TEST_CAPITAL:.0f} USD): "
         f"{int(test.sum())} · analizados: {len(t)}.", ""]

    # 1. Anomalías de PnL de precio
    an = t[t["anomaly"]]
    L += ["## 1. PnL de precio anómalo (revisar antes de usarlo)", ""]
    if an.empty:
        L += ["Ninguno.", ""]
    else:
        L += ["| id | símbolo | capital | price_pnl | net_earned | cierre |", "| --- | --- | --- | --- | --- | --- |"]
        for r in an.itertuples():
            L.append(f"| {r.id} | {r.symbol} | {r.capital_used:.0f} | {r.price_pnl:+.2f} | {r.net_earned:+.2f} | "
                     f"{r.closed_at:%Y-%m-%d} |")
        L += ["", "En un hedge 1x el PnL de precio debería ser del orden del basis (±1 %). Estas cifras se "
                  "excluyen del análisis (se usa solo funding − fees).", ""]

    # 2. Resultado por venue
    L += ["## 2. Funding neto por venue (sin PnL de precio)", "",
          "| venue | trades | capital medio | funding neto USD | ROC anual % (capital·tiempo) | hold medio d |",
          "| --- | --- | --- | --- | --- | --- |"]
    for ex, g in [*t.groupby("exchange"), ("**total**", t)]:
        roc = g.funding_net.sum() / (g.capital_used * g.days).sum() * 365 * 100
        L.append(f"| {ex} | {len(g)} | {g.capital_used.mean():.0f} | {g.funding_net.sum():.1f} | "
                 f"{roc:.1f} | {g.days.mean():.1f} |")
    L.append("")

    # 3. Trades Binance: ranking al entrar y contraste con el dataset
    b = t[t["in_dataset"]].copy()
    L += ["## 3. Trades Binance: ¿qué veía el simulador al entrar?", "",
          f"{len(b)} de {int((t.exchange == 'Binance').sum())} trades Binance tienen fila en el dataset del simulador "
          "(slot de 8 h de la apertura).", "",
          "| id | símbolo | apertura | tasa diaria % | rango tasa | rango modelo | rango heur | n slot | spot | "
          "funding real (rec/dataset) % | ROC real anual % | sim H30 neto % / d |",
          "| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |"]
    for r in b.itertuples():
        L.append(f"| {r.id} | {r.symbol} | {r.open_at:%Y-%m-%d %H} | {r.daily_rate_pct:.3f} | {r.rank_rate} | "
                 f"{r.rank_p_prod} | {r.rank_heur if r.rank_heur else '—'} | {r.n_slot} | {r.eligible} | "
                 f"{r.rec_funding_frac * 100:.2f} / {r.ds_funding_frac * 100:.2f} | {r.roc_ann:.0f} | "
                 f"{(r.sim_net_H30 * 100 if pd.notna(r.sim_net_H30) else float('nan')):.2f} / "
                 f"{(r.sim_hold_H30 if pd.notna(r.sim_hold_H30) else float('nan')):.1f} |")
    L.append("")

    # 4. Simulador por universo (CEX / Alpha / ambos), en el periodo real y en todo el walk-forward
    alpha_fee = float((t.loc[t.exchange == "Binance", "fees"] / t.loc[t.exchange == "Binance", "exposure"]).median())
    rows = rows.dropna(subset=["net_H30"]).copy()
    rows["net_frac"] = rows["net_H30"] + np.where(rows["alpha"], mb.FEE - alpha_fee, 0.0)
    rows["hold_days"] = rows["hold_H30"]
    periods = (("periodo real", t["open_at"].min(), min(t["closed_at"].max(), rows["t"].max())),
               ("walk-forward completo", rows["t"].min(), rows["t"].max()))
    L += ["## 4. Simulador por universo de spot", "",
          f"Política H30, 5 slots, 5.200 USD, 1x, decisión cada 8 h. Fee CEX {mb.FEE * 100:.2f} % (supuesto); "
          f"fee Alpha = mediana real de los trades Binance = {alpha_fee * 100:.2f} % del nocional. "
          "Alpha = token en la lista pública de Binance Alpha con `listingTime` ≤ fecha y sin spot CEX.", "",
          "| periodo | universo | ranking | PnL USD | anual % | trades | % ganadoras | hold d |",
          "| --- | --- | --- | --- | --- | --- | --- | --- |"]
    for plbl, a, z in periods:
        per = rows[(rows["t"] >= a) & (rows["t"] <= z)]
        for ulbl, mask in (("CEX", ~per["alpha"]), ("Alpha", per["alpha"]), ("CEX ∪ Alpha", per["alpha"] | ~per["alpha"])):
            d = per[mask]
            for rk, gate in (("p_prod (pred>0)", lambda c: c["p_prod"] > 0), ("tasa vigente", None)):
                col = "p_prod" if rk.startswith("p_prod") else "daily_rate"
                r = mb.portfolio(d, col, gate=gate)
                L.append(f"| {plbl} {a:%Y-%m-%d}→{z:%Y-%m-%d} | {ulbl} | {rk} | {r['pnl_usd']} | {r['ret_ann_pct']} | "
                         f"{r['trades']} | {r['win_pct']} | {r['avg_hold_d']} |")
    L.append("")

    # 5. Contrafactual: las MISMAS entradas reales Binance con la salida del simulador
    bb = b.dropna(subset=["sim_net_H30"])
    if len(bb):
        sim_usd = float((bb["sim_net_H30"] * bb["exposure"]).sum())
        real_usd = float(bb["funding_net"].sum())
        L += ["## 5. Mismas entradas reales (Binance), salida del simulador (H30)", "",
              f"{len(bb)} trades: real {real_usd:.1f} USD (hold medio {bb.days.mean():.1f} d) vs "
              f"simulado {sim_usd:.1f} USD (hold medio {bb.sim_hold_H30.mean():.1f} d) con el mismo nocional.", ""]

    L += ["## Lectura", "",
          "- El simulador de `exit_policy_study.py`/`ml_binance.py` filtra `has_spot` (spot CEX) y deja fuera los "
          "tokens cuyo spot solo existe en Binance Alpha, que es donde se hicieron los trades rentables. "
          "Con ese universo la cartera simulada se acerca al ROC realizado.",
          "- Sesgos que quedan: la lista Alpha es la de hoy (tokens borrados no aparecen → supervivencia), "
          "sin liquidez histórica del pool, y la fee Alpha es la mediana de pocos trades.",
          "- Todo estudio o gate de modelo nuevo debe correr sobre CEX ∪ Alpha con la fee Alpha real.", ""]
    path = ROOT / "reports" / f"attribution_{datetime.now(timezone.utc):%Y%m%d}.md"
    path.write_text("\n".join(L), encoding="utf-8")
    sys.stdout.reconfigure(encoding="utf-8")
    print("\n".join(L))


if __name__ == "__main__":
    main()
