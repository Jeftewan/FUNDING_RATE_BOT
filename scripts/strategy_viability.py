#!/usr/bin/env python3
"""Viabilidad de la estrategia spot_perp CEX ∪ Alpha: sensibilidades de la cartera simulada.
— LOCAL, read-only.

Mismo motor que scripts/rotation_study.py (sin rotar): universo cache/binance/ml_v2_rows.pkl
(scripts/ml_v2.py), salida H30 (dos pagos ≤ 0, tope 30 d), decisión cada 8 h, 1x, nocional por
pierna = capital / slots / 2. Preguntas:
  1. ¿El edge se mantiene en el tiempo? (anual % por trimestre, ranking por tasa: sin fuga del modelo)
  2. ¿Cuánto depende de pocos tokens? (concentración y PnL sin los top-k)
  3. ¿Qué pesa la fee? (multiplicador sobre la fee de entrada+salida)
  4. Capacidad: capital 5.2 k → 260 k con el impacto on-chain MEDIDO (cotizaciones KyberSwap, read-only)
     sumado a la fee Alpha
  5. Capital ocioso: ocupación media de los slots
Salida: reports/strategy_viability_YYYYMMDD.md
"""
import sys
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))
import ml_binance as mb  # noqa: E402
import ml_v2  # noqa: E402

RISK_FREE = 4.0          # % anual de referencia (T-bills / Simple Earn USDT, orden de magnitud)
REAL_NOTIONAL = 800.0    # nocional medio por pierna de los trades Alpha reales (capital ~1.6 k a 1x)
DEPTH_SIZES = (50, 1000, 2600, 5000, 13000, 26000)    # USD; 50 = referencia de precio medio
KYBER_ROUTES = "https://aggregator-api.kyberswap.com/bsc/api/v1/routes"
USDT_BSC = "0x55d398326f99059fF775485246999027B3197955"


def depth_curve(symbols, today):
    """Impacto de compra (%) por tamaño con cotizaciones públicas de KyberSwap (no opera nada).
    Cachea en cache/binance/alpha_depth_YYYYMMDD.csv."""
    path = ROOT / "cache" / "binance" / f"alpha_depth_{today}.csv"
    if path.exists():
        return pd.read_csv(path)
    import json
    import time
    import requests
    alist = json.loads((ROOT / "cache" / "binance" / "alpha_tokens.json").read_text(encoding="utf-8"))
    addr = {str(a["symbol"]).upper(): (a["contractAddress"], float(a["liquidity"] or 0)) for a in alist
            if str(a.get("chainId")) == "56" and not a.get("offline")}

    def out(tok, usd):
        r = requests.get(KYBER_ROUTES, params={"tokenIn": USDT_BSC, "tokenOut": tok,
                                               "amountIn": str(int(usd * 1e18))},
                         headers={"x-client-id": "basyo"}, timeout=15)
        return int(r.json()["data"]["routeSummary"]["amountOut"])

    rows = []
    for s in symbols:
        if s not in addr:
            continue
        tok, liq = addr[s]
        try:
            ref = out(tok, DEPTH_SIZES[0]) / DEPTH_SIZES[0]
            row = {"symbol": s, "liquidity_usd": round(liq)}
            for z in DEPTH_SIZES[1:]:
                time.sleep(0.3)
                row[f"imp_{z}"] = round(100 * (1 - out(tok, z) / z / ref), 3)
            rows.append(row)
        except Exception as e:  # noqa: BLE001 — sin ruta / token sin pool: se omite
            print(f"depth {s}: {type(e).__name__}", flush=True)
    df = pd.DataFrame(rows)
    df.to_csv(path, index=False)
    return df


def simulate(df, fee_mult=1.0, capital=mb.CAPITAL, slots=mb.SLOTS, rank="daily_rate",
             alpha_extra=0.0, drop=()):
    """Cartera sin rotación. Devuelve métricas + PnL por símbolo y por mes de salida."""
    notional = capital / slots / 2
    pos, pnl, trades, wins, occ, steps = [], 0.0, 0, 0, 0, 0
    by_sym, by_month = defaultdict(float), defaultdict(float)
    for t, g in df.groupby(df["t"].dt.floor("8h")):
        still = []
        for p in pos:
            if p["exit"] <= t:
                v = p["net"] * notional
                pnl += v
                trades += 1
                wins += v > 0
                by_sym[p["sym"]] += v
                by_month[p["exit"].strftime("%Y-%m")] += v
            else:
                still.append(p)
        pos = still
        cand = g[g[rank] > 0] if rank == "p_prod" else g
        cand = cand.drop_duplicates("symbol", keep="last").sort_values(rank, ascending=False)
        if drop:
            cand = cand[~cand["symbol"].isin(drop)]
        held = {p["sym"] for p in pos}
        for r in cand.itertuples():
            if len(pos) >= slots:
                break
            if r.symbol in held:
                continue
            fee = ml_v2.FEE_ALPHA if r.alpha == 1 else ml_v2.FEE_CEX
            pos.append({"sym": r.symbol, "exit": r.t + timedelta(days=float(r.hold_days)),
                        "net": r.net_frac - (fee_mult - 1) * fee - (alpha_extra if r.alpha == 1 else 0.0)})
            held.add(r.symbol)
        occ += len(pos)
        steps += 1
    span = max(1.0, (df["t"].max() - df["t"].min()).total_seconds() / 86400)
    return {"pnl": pnl, "ann": pnl / capital / span * 365 * 100, "trades": trades,
            "win": 100 * wins / trades if trades else 0.0, "occ": 100 * occ / max(1, steps) / slots,
            "by_sym": dict(by_sym), "by_month": dict(by_month)}


def fmt(x, d=1):
    return f"{x:.{d}f}"


def main():
    sys.stdout.reconfigure(encoding="utf-8")
    import joblib
    from analysis.ml_features import FEATURE_NAMES
    df = pd.read_pickle(ml_v2.ROWS_CACHE)    # ya filtrado a elegibles CEX ∪ Alpha por ml_v2
    t_max = df["t"].max()
    a0 = df.loc[df["alpha"] == 1, "t"].min()
    today = datetime.now(timezone.utc).strftime("%Y%m%d")
    L = [f"# Viabilidad de la estrategia spot_perp CEX ∪ Alpha — {today}", "",
         f"Motor de `scripts/rotation_study.py` sin rotar: salida H30, 1x, decisión cada 8 h, fee CEX "
         f"{ml_v2.FEE_CEX * 100:.2f} %, Alpha {ml_v2.FEE_ALPHA * 100:.2f} % del nocional. Primer token Alpha "
         f"en el universo: {a0:%Y-%m-%d}. Referencia libre de riesgo: {RISK_FREE:.0f} % anual.", ""]

    # 1. Edge en el tiempo (ranking por tasa vigente: no usa el modelo → sin fuga in-sample)
    L += ["## 1. Edge por trimestre (ranking por tasa vigente, 5 slots, 5.200 USD)", "",
          "| trimestre | universo | anual % | trades | % ganadoras | ocupación slots % |",
          "| --- | --- | --- | --- | --- | --- |"]
    df["q"] = df["t"].dt.tz_localize(None).dt.to_period("Q")
    for q in sorted(df.loc[df["t"] >= a0, "q"].unique()):
        dq = df[df["q"] == q]
        for name, sub in (("CEX", dq[dq["alpha"] == 0]), ("CEX ∪ Alpha", dq)):
            r = simulate(sub)
            L.append(f"| {q} | {name} | {fmt(r['ann'])} | {r['trades']} | {fmt(r['win'])} | {fmt(r['occ'])} |")
            print(q, name, fmt(r["ann"]), flush=True)

    L += ["", "Cada trimestre se simula por separado: las posiciones abiertas al cierre del trimestre no "
          "cuentan (sesgo a la baja, mayor en el último trimestre, parcial). Sin gate de predicción: entra "
          "aunque la tasa sea baja (por eso la ocupación es 100 %)."]
    # Periodo walk-forward del modelo vigente (como attribution/rotation)
    wf = df[df["t"] >= t_max - timedelta(days=mb.HOLDOUT_DAYS) - pd.DateOffset(months=mb.WF_MONTHS)].copy()
    wf["p_prod"] = joblib.load(ROOT / "models" / "scoring_model.joblib")["model"].predict(
        wf[FEATURE_NAMES].to_numpy())
    base = simulate(wf, rank="p_prod")
    L += ["", f"Periodo walk-forward {wf['t'].min():%Y-%m-%d} → {t_max:%Y-%m-%d}, ranking modelo vigente "
          f"(pred > 0): **{fmt(base['ann'])} % anual**, {base['trades']} trades, ocupación de slots "
          f"{fmt(base['occ'])} % → exceso sobre la tasa libre de riesgo ≈ {fmt(base['ann'] - RISK_FREE)} pp.", ""]

    # 2. Concentración
    s = pd.Series(base["by_sym"]).sort_values(ascending=False)
    tot = s.sum()
    L += ["## 2. Concentración del PnL (walk-forward, modelo vigente)", "",
          f"{len(s)} símbolos operados. Top-5: " + ", ".join(f"{k} {v:+.0f}" for k, v in s.head(5).items()) + ".", "",
          "| se excluye del universo | anual % | trades |", "| --- | --- | --- |"]
    L.append(f"| nada | {fmt(base['ann'])} | {base['trades']} |")
    for k in (1, 3, 5, 10):
        r = simulate(wf, rank="p_prod", drop=tuple(s.index[:k]))
        L.append(f"| top-{k} por PnL | {fmt(r['ann'])} | {r['trades']} |")
        print("drop", k, fmt(r["ann"]), flush=True)
    share3 = 100 * s.head(3).sum() / tot if tot else 0
    L += ["", f"Top-3 = {fmt(share3)} % del PnL (excluirlos re-asigna el slot al siguiente candidato).", ""]

    # Mensual
    m = pd.Series(base["by_month"]).sort_index()
    L += ["Retorno mensual sobre el capital (por mes de cierre): " +
          ", ".join(f"{k} {100 * v / mb.CAPITAL:+.1f} %" for k, v in m.items()) + ".",
          f"Meses negativos: {int((m < 0).sum())} de {len(m)}.", ""]

    # 3. Fees
    L += ["## 3. Sensibilidad a la fee (entrada + salida)", "",
          "| fee × | fee Alpha % | anual % |", "| --- | --- | --- |"]
    for fm in (0.5, 0.75, 1.0, 1.5, 2.0):
        r = simulate(wf, rank="p_prod", fee_mult=fm)
        L.append(f"| {fm} | {fmt(ml_v2.FEE_ALPHA * fm * 100, 2)} | {fmt(r['ann'])} |")
        print("fee", fm, fmt(r["ann"]), flush=True)

    # 4. Capacidad: impacto on-chain MEDIDO hoy (cotizaciones Kyber) sobre los tokens Alpha operados
    alpha_syms = set(wf.loc[wf["alpha"] == 1, "symbol"])
    traded = sorted((s for s in base["by_sym"] if s in alpha_syms), key=lambda s: -base["by_sym"][s])
    dc = depth_curve(traded, today)
    med = dc[[c for c in dc.columns if c.startswith("imp_")]].median()
    sizes = np.array([int(c[4:]) for c in med.index], float)
    imp = lambda n: float(np.interp(np.log(n), np.log(sizes), med.to_numpy()))  # noqa: E731
    L += ["", f"## 4. Capacidad: impacto on-chain medido ({len(dc)} tokens Alpha operados, cotizaciones "
          f"KyberSwap del {today})", "",
          "Impacto de compra (una vía) vs una compra de 50 USD, mediana entre tokens: " +
          ", ".join(f"{int(s):,} USD → {v:.2f} %" for s, v in zip(sizes, med)) + ". Detalle por token en "
          f"`cache/binance/alpha_depth_{today}.csv`.", "",
          f"La fee Alpha de {ml_v2.FEE_ALPHA * 100:.2f} % corresponde a un nocional real de ~{REAL_NOTIONAL:,.0f} USD "
          "por pierna; para N mayor se suma 2·(impacto(N) − impacto(real)) (compra + venta). La liquidez es la de "
          "hoy (no hay histórico) y se aplica a todos los tokens Alpha del periodo.", "",
          "| capital USD | slots | N/pierna USD | impacto 1 vía % | fee Alpha efectiva % | anual % |",
          "| --- | --- | --- | --- | --- | --- |"]
    for cap, sl in ((5200, 5), (10400, 5), (26000, 5), (52000, 10), (104000, 10), (260000, 20)):
        n = cap / sl / 2
        extra = max(0.0, 2 * (imp(n) - imp(REAL_NOTIONAL)) / 100)
        r = simulate(wf, rank="p_prod", capital=cap, slots=sl, alpha_extra=extra)
        L.append(f"| {cap:,} | {sl} | {n:,.0f} | {fmt(imp(n), 2)} | {fmt((ml_v2.FEE_ALPHA + extra) * 100, 2)} | "
                 f"{fmt(r['ann'])} |")
        print("cap", cap, sl, fmt(r["ann"]), flush=True)
    L += ["", "Limitaciones: liquidez on-chain actual (no histórica) y un solo tramo por entrada (un TWAP de "
          "varios tramos espaciados reduce el impacto si el pool se re-arbitra entre tramos); lista Alpha actual "
          "(sesgo de supervivencia); sin basis de entrada/salida."]
    path = ROOT / "reports" / f"strategy_viability_{today}.md"
    path.write_text("\n".join(L), encoding="utf-8")
    print("\n".join(L))


if __name__ == "__main__":
    main()
