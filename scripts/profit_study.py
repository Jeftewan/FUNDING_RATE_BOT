#!/usr/bin/env python3
"""Rentabilidad por operación y por usuario — LOCAL, read-only.

Pregunta: ¿qué cambio de entrada, salida, selección o riesgo baja la pérdida de precio por operación
y acerca la cartera a 2.5–5 % mensual sobre el capital total?

Mismo motor que exec_study (scripts/_exec_engine.py: velas 1m perp/MARK/on-chain sin velas planas,
costo a + b·USD por token, empuje que decae ρ/min, mmr 5 %, seguros), pero:
  * entradas = candidatos del MODELO (filas de ml_v2 con p_prod > 0 en los tokens con velas 1m),
    no intentos cada 6 h; la salida la decide una regla sobre el funding liquidado (B = dos pagos
    ≤ 0, tope 30 d), no un hold fijo;
  * gas por swap; tramos de tamaño fijo (100–300 USD/min, «POV» con impacto lineal);
  * toma de ganancia por convergencia del basis (He, Manela, Ross, von Wachter: la ganancia del
    arbitraje viene sobre todo de la convergencia del precio);
  * rebalanceo spot → margen al subir el precio (Dynamic Collateral Control, arXiv 2605.05089);
  * cartera por usuario (cupos, capital, leverage) → rentabilidad mensual sobre capital total.

Criterio (fijado antes de correr), frente a P0 (vigente) a 1x, nocional 1,000 USD:
  Δ media por operación > 0 con IC 95 % por bloques símbolo × semana, Δp5 ≥ −0.25 pp y mejor
  media en ≥ 80 % de los trimestres con ≥ 30 operaciones. La cartera ordena las que pasan por
  rentabilidad mensual sobre capital (y % de meses ≥ 2.5 %), con DD mensual ≤ 5 %.

    python -u scripts/profit_study.py --calibrate     # modelo vs trades reales (entrada/salida)
    python -u scripts/profit_study.py [--workers 6]   # → reports/profit_study_YYYYMMDD.md
"""
import argparse
import math
import pickle
import sys
from concurrent.futures import ProcessPoolExecutor
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))
import _exec_engine as E  # noqa: E402
from _exec_engine import Pol  # noqa: E402
from analysis import signals as SIG  # noqa: E402

ROWS = ROOT / "cache" / "binance" / "ml_v2_rows.pkl"
OUT = ROOT / "cache" / "profit_study.pkl"
MONTHS = [str(pd.Period("2025-10", "M") + k) for k in range(13)]
GAS_USD = 0.05            # swap en BSC (≈ 200k gas); conservador
REB_LAT = 3               # min: swap + depósito BSC → Binance antes de que el margen cuente
PERP = E.PERP_COST
REF_USD = 1000.0          # nocional de referencia (10k USD / 5 cupos a 1x)
MIN_Q = 30
P5_TOL = 0.25


@dataclass(frozen=True)
class Var:
    name: str
    pol: Pol = field(default_factory=lambda: Pol("P0", gap=15, win_x=1440, a_gap=10))
    lev: float = 1.0
    k_neg: int = 2            # salida: k pagos seguidos ≤ 0
    min_days: float = 0.0     # hold mínimo antes de mirar la regla
    tp: float = None          # toma de ganancia: basis ejecutable de venta ≥ tp (%)
    rebal: float = None       # rebalanceo spot→margen al recorrer esta fracción del camino al SL
    slice_usd: float = None   # tramos de tamaño fijo (USD) en vez de por impacto
    cap_days: float = 30      # hold máximo
    tp_floor: float = None    # salida: basis de venta ≥ tp_floor con el funding en el piso (analysis/signals)
    floor_n: int = 1          # pagos liquidados desde la entrada que deben estar en el piso


BASE_POL = Pol("P0", gap=15, win_x=1440, a_gap=10)


def variants():
    P = BASE_POL
    return [
        Var("P0 vigente (θ+0.25/−0.15, 15 min, B, 1x)"),
        # Ejecución: tramos chicos / POV con impacto lineal y gas
        Var("E3 tramos 100 USD cada 1 min", replace(P, gap=1, smax=999), slice_usd=100),
        Var("E3 tramos 300 USD cada 1 min", replace(P, gap=1, smax=999), slice_usd=300),
        Var("E3 tramos 100 USD cada 5 min", replace(P, gap=5, smax=999), slice_usd=100),
        Var("E3 impacto ≤0.05 % cada 15 min", replace(P, imp=0.05, smax=20)),
        Var("E3 un solo swap", replace(P, smax=1)),
        # Entrada más exigente (precio límite en basis) con ventana más larga
        Var("E4 θe 0.50 / 12 h", replace(P, th_e=0.50, win_e=720)),
        Var("E4 θe 0.50 / 24 h", replace(P, th_e=0.50, win_e=1440)),
        Var("E4 θe 0.75 / 24 h", replace(P, th_e=0.75, win_e=1440)),
        # Salida
        Var("X1 toma de ganancia basis ≥ +0.30", tp=0.30),
        Var("X1 toma de ganancia basis ≥ +0.60", tp=0.60),
        Var("X2 salir con 3 pagos ≤ 0", k_neg=3),
        Var("X2 hold mínimo 7 d", min_days=7),
        Var("X3 salida θx −0.30 / 48 h", replace(P, th_x=-0.30, win_x=2880)),
        Var("X3 salida θx 0.00 / 48 h", replace(P, th_x=0.0, win_x=2880)),
        # Riesgo
        Var("R1 2x", lev=2),
        Var("R1 3x", lev=3),
        Var("R2 1x rebalanceo al 40 % del camino", rebal=0.4),
        Var("R2 2x rebalanceo al 40 %", lev=2, rebal=0.4),
        Var("R2 3x rebalanceo al 40 %", lev=3, rebal=0.4),
        Var("R2 2x rebalanceo al 60 %", lev=2, rebal=0.6),
    ]


# ── Salida por regla de funding ────────────────────────────────────────
def exit_minutes(S, t, k_neg, min_days, cap_days=30.0):
    """Minutos desde t hasta la salida: k pagos seguidos ≤ 0 (después de min_days), tope 30 d."""
    t_ms = int(S.ts[t])
    i0 = int(np.searchsorted(S.f_ts, t_ms, "right"))
    cap_ms = t_ms + int(cap_days * 86_400_000)
    if i0 >= len(S.f_ts) or S.f_ts[-1] < cap_ms and S.f_ts[-1] - t_ms < 2 * 86_400_000:
        return None
    run, exit_ms = 0, None
    for i in range(i0, len(S.f_ts)):
        ts = int(S.f_ts[i])
        if ts >= cap_ms:
            exit_ms = cap_ms
            break
        run = run + 1 if S.daily[i] <= 0 else 0
        if run >= k_neg and ts - t_ms >= min_days * 86_400_000:
            exit_ms = ts
            break
    if exit_ms is None:
        return None
    return int((exit_ms - t_ms) // 60_000)


def n_override(usd, v):
    return max(1, math.ceil(usd / v.slice_usd)) if v.slice_usd else None


def leg(S, start, side, usd, v, force=False):
    """E.leg con tramos de tamaño fijo opcionales (sin tope de impacto por tramo)."""
    if not v.slice_usd:
        return E.leg(S, start, side, usd, v.pol, force=force)
    n = n_override(usd, v)
    pol = replace(v.pol, smax=n, imp=1e-9)              # n_slices → n
    return E.leg(S, start, side, usd, pol, force=force)


def floor_mask(S, n):
    """Por minuto: los últimos n pagos liquidados (≤ ts) están en el piso (SIG.funding_at_floor)."""
    key = ("floor", n)
    if key not in S._cache:
        weak = (S.daily <= SIG.FUNDING_FLOOR_DAILY + SIG.FLOOR_EPS).astype(int)
        run = np.convolve(weak, np.ones(n, int), "full")[:len(weak)] >= n
        i = np.searchsorted(S.f_ts, S.ts, "right") - 1
        m = np.zeros(S.n, bool)
        ok = i >= 0
        m[ok] = run[i[ok]]
        S._cache[key] = m
    return S._cache[key]


def fill_shares(S, t, usd, pol):
    """Fracción de las 24 h previas con basis ejecutable de compra ≥ θ: (velas 1m, muestreo horario).
    El horario imita market_feature_snapshots (VWAP de las 5 velas que cierran en cada hora UTC)."""
    c = S.a + S.b * usd / E.n_slices(S, usd, pol)
    ex = S.ex("buy", c)
    w = ex[t - 1440:t]
    ok = ~np.isnan(w)
    f1 = float((w[ok] >= pol.th_e).mean()) if ok.any() else None
    hours = [k for k in range(t - 1440, t) if int(S.ts[k]) % 3_600_000 == 0]
    vals = []
    for h in hours:
        seg = ex[max(0, h - 5):h]
        vals.append(float(np.nanmean(seg)) if np.isfinite(seg).any() else None)
    return f1, SIG.fill_share(vals, pol.th_e)


# ── Ciclo de vida con TP, rebalanceo y gas ─────────────────────────────
def lifecycle(S, t, usd, v, hold):
    """dict o None (sin precio). Valores en % del nocional objetivo; sin entrada → todo 0."""
    pol, lev = v.pol, v.lev
    res = leg(S, t, "buy", usd, v)
    if res is None or not res[0]:
        return {"net": 0.0, "entry": 0.0, "exit": 0.0, "fund": 0.0, "frac": 0.0, "case": 0, "reb": 0,
                "gas": 0.0, "t_in": t, "t_out": t}
    fills, n = res
    frac = len(fills) / n
    js = np.array([j for j, _ in fills])
    cs = np.array([c for _, c in fills])
    SE = float(np.mean(S.sc[js] * (1 + cs / 100)))
    PE = float(np.mean(S.pc[js] * (1 - PERP / 100)))
    G0 = float(np.mean(S.G[js]))
    entry = (PE / SE - 1) * 100
    k_in = int(js[-1]) + 1
    start = t + hold
    if v.tp is not None:                                  # convergencia: salir antes si el basis paga
        c_x = S.a + S.b * usd * frac / max(1, n)
        ex = S.ex("sell", c_x)[k_in:min(t + hold, S.n - 1)]
        hit = E._first(ex >= v.tp) if len(ex) else -1
        if hit >= 0:
            start = k_in + hit
    if v.tp_floor is not None:                            # piloto LLM: basis paga y funding en el piso
        i_in = int(np.searchsorted(S.f_ts, S.ts[k_in], "right"))
        if i_in + v.floor_n - 1 < len(S.f_ts):
            lo = max(k_in, int(np.searchsorted(S.ts, S.f_ts[i_in + v.floor_n - 1])))
            hi = min(t + hold, S.n - 1)
            if lo < hi:
                c_x = S.a + S.b * usd * frac / max(1, n)
                hit = E._first((S.ex("sell", c_x)[lo:hi] >= v.tp_floor) & floor_mask(S, v.floor_n)[lo:hi])
                if hit >= 0:
                    start = min(start, lo + hit)
    outs, left, st = [], 1.0, start
    for attempt in range(4):
        out = leg(S, st, "sell", left * frac * usd, v, force=attempt == 3 or not pol.wait)
        if out is None:
            st += pol.win_x
            continue
        xf, xn = out
        outs += [(j, c, left / xn) for j, c in xf]
        left -= len(xf) * left / xn
        if left <= 1e-9:
            break
        st = max(st + pol.win_x, (xf[-1][0] + 1) if xf else 0)
    if not outs:
        return None
    if left > 1e-9:
        outs.append((outs[-1][0], outs[-1][1], left))
    k1 = outs[0][0]

    # Seguros por segmentos: cada rebalanceo vende la ganancia del spot, la deposita como margen y
    # recompra la misma fracción del short → nocional 1 a los precios nuevos, liquidación re-anclada.
    seg_val, fund, cost, nreb = 0.0, 0.0, 0.0, 0
    SEc, PEc, Gs, k = SE, PE, G0, k_in
    ev = None
    trig = S.mh
    while True:
        if v.rebal and k < k1:
            liq = PEc * (1 + 1 / lev) / (1 + E.MMR)
            sl = PEc + pol.F * (liq - PEc)
            lvl = PEc + v.rebal * (sl - PEc)
            tr = E._first(trig[k:k1] >= lvl)
            ts_ = E._first(trig[k:k1] >= sl)
            if tr >= 0 and (ts_ < 0 or tr < ts_):
                r = k + tr
                r2 = min(r + REB_LAT, k1 - 1)
                if E._first(trig[r:r2 + 1] >= sl) < 0 and r2 > r:
                    s2, p2 = float(S.scf[r2]), float(S.pc[r2])
                    seg_val += s2 / SEc - p2 / PEc               # marcado a mercado del segmento
                    fund += (S.G[r2] - Gs) / PEc
                    sold = s2 / SEc - 1                           # USD de spot sobre el nocional 1
                    if sold > 0:
                        cost += sold * (S.a + S.b * usd * frac * sold + PERP) / 100
                    SEc, PEc, Gs, k = s2, p2, float(S.G[r2]), r2 + 1
                    nreb += 1
                    continue
        ev = E.protect(S, k, k1, PEc, SEc, frac * usd, pol, lev)
        break
    if ev:
        val, j, case = ev
        fund += (S.G[j] - Gs) / PEc
        code = {"A": 2, "B": 3, "AB": 4}[case]
        n_out = pol.a_slices if case in ("A",) else 1
        t_out = j
    else:
        val = sum(w * (S.sc[j] * (1 - c / 100) / SEc - S.pc[j] * (1 + PERP / 100) / PEc) for j, c, w in outs)
        fund += sum(w * (S.G[j] - Gs) for j, _, w in outs) / PEc
        code, n_out, t_out = 1, len(outs), outs[-1][0]
    total_val = (seg_val + val) * 100 * frac - cost * 100 * frac
    gas = GAS_USD * (len(fills) + n_out + 2 * nreb) / usd * 100
    fund *= 100 * frac
    return {"net": total_val + fund - gas, "entry": entry * frac, "exit": total_val - entry * frac,
            "fund": fund, "frac": frac, "case": code, "reb": nreb, "gas": gas, "t_in": int(js[0]),
            "t_out": int(t_out)}


# ── Candidatos del modelo ──────────────────────────────────────────────
def candidates():
    import joblib
    from analysis.ml_features import FEATURE_NAMES
    syms = E.cached_symbols()
    df = pd.read_pickle(ROWS)
    df = df[df["symbol"].isin(syms.keys()) & (df["alpha"] == 1)].copy()
    bundle = joblib.load(ROOT / "models" / "scoring_model.joblib")
    df["p_prod"] = bundle["model"].predict(df[FEATURE_NAMES].to_numpy())
    df = df[df["p_prod"] > 0]
    return df[["symbol", "funding_ts", "t", "p_prod", "daily_rate", "fund_H30", "hold_H30"]], syms


def work(job):
    sym, months, fund, cost, cand, vs, sizes = job
    d = E.load(sym, months)
    if d is None or len(d["ts"]) < 40000:
        return sym, None
    S = E.Sym(sym, d, fund, cost)
    with np.errstate(invalid="ignore"):
        if np.nanmean(np.abs(S.pc / S.sc - 1) * 100) > 3:
            return sym, None
    tail = 4 * 2880 + 240
    # volatilidad 7 d (log-retornos diarios del perp) y recorrido 24 h al decidir
    rows = []
    for c in cand.itertuples():
        t = int(np.searchsorted(S.ts, int(c.funding_ts)))
        if t < 10080 or t >= S.n - tail:
            continue
        daily = S.pc[t - 10080:t + 1:1440]
        vol7 = float(np.std(np.diff(np.log(daily)))) * 100 if len(daily) > 3 else np.nan
        rng24 = float(np.nanmax(S.ph[t - 1440:t]) / np.nanmin(S.pl[t - 1440:t]) - 1) * 100
        rows.append((t, c.p_prod, c.daily_rate, vol7, rng24))
    out = []
    holds, fills = {}, {}
    for t, pp, dr, vol7, rng24 in rows:
        for vi, v in enumerate(vs):
            key = (v.k_neg, v.min_days, v.cap_days)
            if (t, key) not in holds:
                holds[(t, key)] = exit_minutes(S, t, *key)
            hold = holds[(t, key)]
            if hold is None or t + hold >= S.n - tail:
                continue
            for usd in (sizes(v) if callable(sizes) else sizes):
                r = lifecycle(S, t, usd, v, hold)
                if r is None:
                    continue
                if (t, usd) not in fills:
                    fills[(t, usd)] = fill_shares(S, t, usd, v.pol)
                f1m, fh = fills[(t, usd)]
                out.append({"sym": sym, "vi": vi, "usd": usd, "t_ms": int(S.ts[t]), "p_prod": pp,
                            "fill1m": f1m, "fillh": fh,
                            "daily": dr, "vol7": vol7, "rng24": rng24,
                            "in_ms": int(S.ts[r["t_in"]]), "out_ms": int(S.ts[min(r["t_out"], S.n - 1)]),
                            "a": S.a, "b": S.b, **{k: r[k] for k in ("net", "entry", "exit", "fund", "frac",
                                                                        "case", "reb", "gas")}})
    return sym, out


def run(vs, sizes, workers):
    cand, syms = candidates()
    funding, cost = E.funding_table(syms), E.cost_table()
    jobs = [(s, [m for m in syms[s] if m in MONTHS], funding[s], cost(s), cand[cand.symbol == s], vs, sizes)
            for s in sorted(syms) if s in funding and (cand.symbol == s).any()]
    rows = []
    with ProcessPoolExecutor(workers) as ex:
        for sym, out in ex.map(work, jobs):
            if out:
                rows += out
                print(f"  {sym}: {len(out)} filas", flush=True)
    return pd.DataFrame(rows)


# ── Evaluación por operación ───────────────────────────────────────────
def paired(df, vi, usd, base_vi=0):
    a = df[(df.vi == base_vi) & (df.usd == usd)].set_index(["sym", "t_ms"])
    b = df[(df.vi == vi) & (df.usd == usd)].set_index(["sym", "t_ms"])
    j = a[["net"]].join(b[["net"]], lsuffix="_b", rsuffix="_c", how="inner").reset_index()
    return j


def gate(j, seed=7):
    diff = j.net_c - j.net_b
    week = j.t_ms // E.WEEK_MS
    quarter = j.t_ms // E.QUARTER_MS
    wins = total = 0
    for q, g in j.groupby(quarter):
        if len(g) >= MIN_Q:
            total += 1
            wins += g.net_c.mean() > g.net_b.mean()
    blocks = pd.DataFrame({"b": j.sym + "_" + week.astype(str), "d": diff}).groupby("b").d.agg(["sum", "size"])
    s, n = blocks["sum"].to_numpy(), blocks["size"].to_numpy(float)
    rng = np.random.default_rng(seed)
    pick = rng.integers(0, len(s), (1000, len(s)))
    boots = s[pick].sum(1) / n[pick].sum(1)
    lo, hi = float(np.percentile(boots, 2.5)), float(np.percentile(boots, 97.5))
    dp5 = float(np.percentile(j.net_c, 5) - np.percentile(j.net_b, 5))
    return {"d": float(diff.mean()), "lo": lo, "hi": hi, "dp5": dp5, "wins": wins, "total": total,
            "ok": bool(lo > 0 and dp5 >= -P5_TOL and total and wins >= 0.8 * total)}


def trade_stats(g):
    fired = g.frac > 0
    f = g[fired]
    return {"n": len(g), "fire": fired.mean() * 100, "mean": g.net.mean(), "med": f.net.median() if len(f) else np.nan,
            "p5": np.percentile(g.net, 5), "entry": f.entry.mean(), "exit": f.exit.mean(), "fund": f.fund.mean(),
            "gas": f.gas.mean(), "ev": (g.case > 1).mean() * 100, "reb": f.reb.mean() if len(f) else 0,
            "days": ((f.out_ms - f.in_ms) / 86_400_000).median() if len(f) else np.nan}


# ── Cartera por usuario ────────────────────────────────────────────────
def portfolio(trades, capital, slots, lev, gate_fn=None):
    """Cupos iguales; un token a la vez; el cupo se reserva al decidir (si no entra, se libera
    al vencer la espera). PnL al cerrar. → serie mensual sobre el capital total."""
    notional = capital / slots * lev / (lev + 1)
    tr = trades.sort_values(["t_ms", "p_prod"], ascending=[True, False])
    if gate_fn is not None:
        tr = tr[gate_fn(tr)]
    busy, held, pnl, occ = [], {}, [], []
    for r in tr.itertuples():
        busy = [b for b in busy if b[0] > r.t_ms]
        held = {s: e for s, e in held.items() if e > r.t_ms}
        if len(busy) >= slots or r.sym in held:
            continue
        end = r.out_ms if r.frac > 0 else r.t_ms + 6 * 3_600_000
        busy.append((end, r.sym))
        held[r.sym] = end
        if r.frac > 0:
            pnl.append((r.out_ms, r.net / 100 * notional))
            occ.append((r.in_ms, r.out_ms))
    if not pnl:
        return None
    p = pd.DataFrame(pnl, columns=["ms", "usd"])
    p["m"] = pd.to_datetime(p.ms, unit="ms", utc=True).dt.strftime("%Y-%m")
    t0, t1 = trades.t_ms.min(), trades.t_ms.max()
    months = pd.period_range(pd.to_datetime(t0, unit="ms"), pd.to_datetime(t1, unit="ms"), freq="M").strftime("%Y-%m")
    monthly = p.groupby("m").usd.sum().reindex(months, fill_value=0.0) / capital * 100
    days = (t1 - t0) / 86_400_000
    used = sum(min(b, t1) - max(a, t0) for a, b in occ if b > a) / 86_400_000
    eq = capital + p.sort_values("ms").usd.cumsum()
    dd = float(((eq.cummax() - eq) / eq.cummax()).max() * 100) if len(eq) else 0.0
    return {"monthly": monthly, "mean_m": float(monthly.mean()), "med_m": float(monthly.median()),
            "p10_m": float(monthly.quantile(0.1)), "ge25": float((monthly >= 2.5).mean() * 100),
            "util": used / (days * slots) * 100 if days else 0.0, "dd": dd, "trades": len(pnl)}


# ── Etapa 2: combinaciones, capital por usuario y robustez ─────────────
CAPS = (2_000, 5_000, 10_000, 25_000)
SLOTS = 5


def slot_sizes(v):
    return tuple(round(c / SLOTS * v.lev / (v.lev + 1), 2) for c in CAPS)


def variants2():
    P = BASE_POL
    combo = dict(k_neg=3)
    pol_c = replace(P, th_x=0.0, win_x=2880, gap=5, smax=999)
    return [
        Var("P0 vigente 1x"),
        Var("Como se operó: salida a los 3 d", cap_days=3),
        Var("Salida a los 7 d", cap_days=7),
        Var("R2 1x rebalanceo 60 %", rebal=0.6),
        Var("R2 2x rebalanceo 60 %", lev=2, rebal=0.6),
        Var("R2 3x rebalanceo 60 %", lev=3, rebal=0.6),
        Var("Combo 2x: R2 60 % + 3 pagos + θx 0/48 h + 100 USD/5 min", pol_c, lev=2, rebal=0.6, slice_usd=100, **combo),
        Var("Combo 3x: R2 60 % + 3 pagos + θx 0/48 h + 100 USD/5 min", pol_c, lev=3, rebal=0.6, slice_usd=100, **combo),
    ]


def stage2(workers):
    vs = variants2()
    cand, syms = candidates()
    funding, cost = E.funding_table(syms), E.cost_table()
    jobs = [(s, [m for m in syms[s] if m in MONTHS], funding[s], cost(s), cand[cand.symbol == s], vs, slot_sizes)
            for s in sorted(syms) if s in funding and (cand.symbol == s).any()]
    rows = []
    with ProcessPoolExecutor(workers) as ex:
        for sym, out in ex.map(work, jobs):
            if out:
                rows += out
                print(f"  {sym}: {len(out)} filas", flush=True)
    df = pd.DataFrame(rows)
    df.to_pickle(ROOT / "cache" / "profit_study_stage2.pkl")
    return vs, df


def stage2_report(vs, df):
    today = datetime.now(timezone.utc).strftime("%Y%m%d")
    t0 = df.t_ms.min()
    L = [f"# Rentabilidad por usuario — combinaciones, leverage y capital — {today}", "",
         f"{SLOTS} cupos iguales, un token a la vez, nocional por cupo = capital / {SLOTS} · L/(L+1) (costo de impacto "
         "a ese tamaño). Rentabilidad mensual sobre el CAPITAL TOTAL. Robustez: misma cartera arrancando 0–5 meses más "
         "tarde (6 trayectorias). Universo: 25 tokens solo-Alpha con velas 1m.", "",
         "## Por operación (nocional del usuario de 10,000 USD)", "",
         "| política | n | media % nocional | p5 | eventos SL % | rebalanceos | días (mediana) |", "|---|---|---|---|---|---|---|"]
    for vi, v in enumerate(vs):
        g = df[(df.vi == vi) & (df.usd == slot_sizes(v)[2])]
        if g.empty:
            continue
        s = trade_stats(g)
        L.append(f"| {v.name} | {s['n']} | {s['mean']:+.2f} | {s['p5']:+.2f} | {s['ev']:.1f} | {s['reb']:.2f} | {fmt(s['days'], 1)} |")
    L += ["", "## Mensual sobre capital total por tamaño de usuario", "",
          "| política | capital USD | media mensual | rango de las 6 trayectorias | mediana mes | p10 mes | meses ≥ 2.5 % | DD máx % | utilización |",
          "|---|---|---|---|---|---|---|---|---|"]
    for vi, v in enumerate(vs):
        for cap, usd in zip(CAPS, slot_sizes(v)):
            g = df[(df.vi == vi) & (df.usd == usd)]
            if g.empty:
                continue
            res = []
            for off in range(6):
                start = (pd.Timestamp(t0, unit="ms") + pd.DateOffset(months=off)).value // 1_000_000
                p = portfolio(g[g.t_ms >= start], cap, SLOTS, v.lev)
                if p:
                    res.append(p)
            if not res:
                continue
            means = [r["mean_m"] for r in res]
            allm = pd.concat([r["monthly"] for r in res])
            L.append(f"| {v.name} | {cap:,} | {np.mean(means):+.2f} % | {min(means):+.2f} a {max(means):+.2f} % | "
                     f"{allm.median():+.2f} % | {allm.quantile(0.1):+.2f} % | {(allm >= 2.5).mean() * 100:.0f} % | "
                     f"{max(r['dd'] for r in res):.1f} | {np.mean([r['util'] for r in res]):.0f} % |")
    path = ROOT / "reports" / f"profit_study_{today}_cartera.md"
    path.write_text("\n".join(L) + "\n", encoding="utf-8")
    print("\n".join(L))
    print("Reporte:", path)


# ── Reglas del piloto LLM (reports/llm_policy_pilot_20261118.md) ───────
# Criterio fijado antes de correr. Base = política de producción (3 pagos ≤ 0, θx 0.00 / 48 h).
#  Salida X4 pasa si (a) gate() por operación a 1,000 y 2,100 USD, o (b) cartera a 5k y 10k (6 trayectorias):
#  %/mes medio > base, p10 mensual ≥ base − 0.05 pp y utilización ≥ base − 1 pp; además (b) se sostiene sin
#  operaciones > +20 % (mechas). Entrada E5 (filtro de candidatos, sin re-simular): mismo (b).
SIG_SIZES = (500.0, 1000.0, 2100.0)
SIG_CAPS = ((5_000, 500.0), (10_000, 1000.0))
OUTLIER = 20.0


def variants_signals():
    P = replace(BASE_POL, th_x=0.0, win_x=2880)
    vs = [Var("P0 prod (3 pagos ≤ 0, θx 0.00/48 h)", P, k_neg=3)]
    for tp in (0.2, 0.3, 0.5):
        for n in (1, 3):
            vs.append(Var(f"X4 basis venta ≥ +{tp:.1f} y funding en el piso ({n} pago{'s' if n > 1 else ''})",
                          P, k_neg=3, tp_floor=tp, floor_n=n))
    return vs


def port_traj(g, cap, gate_fn=None):
    t0 = g.t_ms.min()
    res = []
    for off in range(6):
        start = (pd.Timestamp(t0, unit="ms") + pd.DateOffset(months=off)).value // 1_000_000
        p = portfolio(g[g.t_ms >= start], cap, SLOTS, 1.0, gate_fn)
        if p:
            res.append(p)
    if not res:
        return None
    allm = pd.concat([r["monthly"] for r in res])
    return {"mean": float(np.mean([r["mean_m"] for r in res])), "p10": float(allm.quantile(0.1)),
            "util": float(np.mean([r["util"] for r in res])), "dd": max(r["dd"] for r in res),
            "trades": float(np.mean([r["trades"] for r in res]))}


def port_ok(p, b):
    return bool(p and b and p["mean"] > b["mean"] and p["p10"] >= b["p10"] - 0.05 and p["util"] >= b["util"] - 1)


def signals_study(workers, reuse):
    vs = variants_signals()
    out = ROOT / "cache" / "profit_study_signals.pkl"
    if reuse and out.exists():
        df = pd.read_pickle(out)
    else:
        df = run(vs, SIG_SIZES, workers)
        df.to_pickle(out)
    print(f"{len(df):,} filas", flush=True)
    today = datetime.now(timezone.utc).strftime("%Y%m%d")
    L = [f"# Reglas deterministas del piloto LLM — {today}", "",
         f"Candidatos del modelo (`p_prod` > 0) en {df.sym.nunique()} tokens solo-Alpha con velas 1m, "
         f"{pd.to_datetime(df.t_ms.min(), unit='ms'):%Y-%m-%d} → {pd.to_datetime(df.t_ms.max(), unit='ms'):%Y-%m-%d}. "
         "Base = política de producción (salida al 3er pago ≤ 0, salida paciente θx 0.00 / 48 h, θe 0.25 / 6 h, "
         "tramos cada 15 min, 1x). Reglas en `analysis/signals.py` (piso del funding 0.03 %/día + 0.005).", "",
         "**Criterio (fijado antes de correr):** X4 (salida) pasa si (a) `gate()` por operación a 1,000 y 2,100 USD "
         "(IC 95 % símbolo×semana > 0, Δp5 ≥ −0.25 pp, ≥ 80 % trimestres) o (b) cartera de 5 cupos a 5k y 10k USD "
         "(6 trayectorias): %/mes medio > base, p10 mensual ≥ base − 0.05 pp, utilización ≥ base − 1 pp, y (b) se "
         f"sostiene sin operaciones > +{OUTLIER:.0f} %. E5 (filtro de entrada) se evalúa con (b).", ""]

    # 1. Por operación
    L += ["## 1. Salida: por operación", "",
          "| política | USD | n | media | mediana | p5 | salida | días | Δ media [IC 95 %] | Δp5 | trim. | pasa (a) |",
          "|---|---|---|---|---|---|---|---|---|---|---|---|"]
    ok_a = {}
    for vi, v in enumerate(vs):
        for usd in (1000.0, 2100.0):
            g = df[(df.vi == vi) & (df.usd == usd)]
            if g.empty:
                continue
            s = trade_stats(g)
            gt = gate(paired(df, vi, usd)) if vi else None
            if gt:
                ok_a.setdefault(vi, []).append(gt["ok"])
            gtxt = (f"{gt['d']:+.2f} [{gt['lo']:+.2f}, {gt['hi']:+.2f}] | {gt['dp5']:+.2f} | {gt['wins']}/{gt['total']} | "
                    f"{'sí' if gt['ok'] else 'no'}") if gt else "— | — | — | base"
            L.append(f"| {v.name} | {usd:,.0f} | {s['n']} | {s['mean']:+.2f} | {fmt(s['med'])} | {s['p5']:+.2f} | "
                     f"{fmt(s['exit'])} | {fmt(s['days'], 1)} | {gtxt} |")

    # 2. Cartera
    def port_rows(label, frames, tag):
        rows, res = [], {}
        for cap, usd in SIG_CAPS:
            for clean in (False, True):
                g = frames(usd)
                if clean:
                    g = g[g.net <= OUTLIER]
                p = port_traj(g, cap)
                res[(cap, clean)] = p
                if p:
                    rows.append(f"| {label} | {cap:,} | {'sí' if clean else 'no'} | {p['trades']:.0f} | {p['mean']:+.2f} % | "
                                f"{p['p10']:+.2f} % | {p['util']:.0f} % | {p['dd']:.1f} | {tag(cap, clean, p)} |")
        return rows, res

    L += ["", "## 2. Salida: cartera (5 cupos, 1x, media de 6 trayectorias)", "",
          "| política | capital | sin > +20 % | ops | %/mes | p10 mes | utilización | DD % | vs base |",
          "|---|---|---|---|---|---|---|---|---|"]
    base_res = {}
    verdict = {}
    for vi, v in enumerate(vs):
        def frames(usd, vi=vi):
            return df[(df.vi == vi) & (df.usd == usd)]
        if vi == 0:
            rows, base_res = port_rows(v.name, frames, lambda c, cl, p: "base")
        else:
            rows, res = port_rows(v.name, frames,
                                  lambda c, cl, p: "ok" if port_ok(p, base_res.get((c, cl))) else "no")
            ok_b = all(port_ok(res[k], base_res.get(k)) for k in res)
            verdict[vi] = bool(all(ok_a.get(vi, [False])) or ok_b)
        L += rows

    # 3. Filtro de entrada
    base = lambda usd: df[(df.vi == 0) & (df.usd == usd)]  # noqa: E731
    b0 = base(1000.0)
    rho = b0[["fill1m", "fillh"]].dropna().corr(method="spearman").iloc[0, 1] if len(b0) else np.nan
    L += ["", "## 3. Entrada: filtro por fracción de las 24 h con basis ≥ θ (sobre P0 prod)", "",
          f"Paridad muestreo horario (prod) vs velas 1m: ρ Spearman {rho:.2f} (n {b0[['fill1m', 'fillh']].dropna().shape[0]}).", "",
          "| filtro | medición | % candidatos | entra % (kept) | entra % (excl.) | media kept | media excl. |",
          "|---|---|---|---|---|---|---|"]
    filt_res = {}
    for col in ("fillh", "fill1m"):
        for fm in (0.3, 0.5, 0.7):
            m = b0[col].fillna(0) >= fm
            k, x = b0[m], b0[~m]
            L.append(f"| ≥ {fm:.0%} | {'horaria' if col == 'fillh' else '1m'} | {m.mean() * 100:.0f} | "
                     f"{(k.frac > 0).mean() * 100:.0f} | {(x.frac > 0).mean() * 100 if len(x) else float('nan'):.0f} | "
                     f"{k.net.mean():+.2f} | {fmt(x.net.mean() if len(x) else None)} |")
    L += ["", "| filtro (horaria) | capital | sin > +20 % | ops | %/mes | p10 mes | utilización | DD % | vs base |",
          "|---|---|---|---|---|---|---|---|---|"]
    for fm in (0.3, 0.5, 0.7):
        def frames(usd, fm=fm):
            g = base(usd)
            return g[g.fillh.fillna(0) >= fm]
        rows, res = port_rows(f"E5 fill ≥ {fm:.0%}", frames,
                              lambda c, cl, p: "ok" if port_ok(p, base_res.get((c, cl))) else "no")
        filt_res[fm] = all(port_ok(res[k], base_res.get(k)) for k in res)
        L += rows

    # 4. Combinación: mejor X4 que pasa + mejor E5 que pasa
    best_x = max((vi for vi in verdict if verdict[vi]), default=None,
                 key=lambda vi: port_traj(df[(df.vi == vi) & (df.usd == 1000.0)], 10_000)["mean"])
    best_e = max((fm for fm in filt_res if filt_res[fm]), default=None,
                 key=lambda fm: port_traj(b0[b0.fillh.fillna(0) >= fm], 10_000)["mean"])
    comb_ok = None
    if best_x is not None and best_e is not None:
        def frames(usd):
            g = df[(df.vi == best_x) & (df.usd == usd)]
            return g[g.fillh.fillna(0) >= best_e]
        rows, res = port_rows(f"{vs[best_x].name} + E5 fill ≥ {best_e:.0%}", frames,
                              lambda c, cl, p: "ok" if port_ok(p, base_res.get((c, cl))) else "no")
        comb_ok = all(port_ok(res[k], base_res.get(k)) for k in res)
        L += ["", "## 4. Combinación", "", "| política | capital | sin > +20 % | ops | %/mes | p10 mes | utilización | DD % | vs base |",
              "|---|---|---|---|---|---|---|---|---|"] + rows

    L += ["", "## 5. Veredicto", ""]
    for vi, ok in verdict.items():
        L.append(f"- {vs[vi].name}: **{'pasa' if ok else 'no pasa'}**")
    for fm, ok in filt_res.items():
        L.append(f"- E5 fill ≥ {fm:.0%} (horaria): **{'pasa' if ok else 'no pasa'}**")
    if comb_ok is not None:
        L.append(f"- Combinación: **{'pasa' if comb_ok else 'no pasa'}**")
    path = ROOT / "reports" / f"signal_rules_study_{today}.md"
    path.write_text("\n".join(L) + "\n", encoding="utf-8")
    print("\n".join(L))
    print("Reporte:", path)


# ── Calibración contra trades reales ───────────────────────────────────
def calibrate():
    """Basis de entrada y de salida del modelo (ejecución inmediata, 1 swap) vs exec_json real."""
    from dotenv import load_dotenv
    from sqlalchemy import text
    load_dotenv(ROOT / ".env")
    from _scoring_data import get_engine
    with get_engine().connect() as c:
        h = pd.read_sql(text("SELECT id, symbol, capital_used, exposure, leverage, hours, closed_at, exec_json "
                             "FROM user_history WHERE closed_at >= '2026-10-01' AND exec_json IS NOT NULL"), c)
    syms = E.cached_symbols()
    cost = E.cost_table()
    now_pol = Pol("now", wait=False, smax=1)
    rows = []
    for r in h.itertuples():
        e = r.exec_json if isinstance(r.exec_json, dict) else {}
        if r.symbol not in syms:
            continue
        d = E.load(r.symbol, syms[r.symbol])
        if d is None:
            continue
        fund = E.funding_table([r.symbol]).get(r.symbol)
        if fund is None:
            continue
        S = E.Sym(r.symbol, d, fund, cost(r.symbol))
        close_ms = int(pd.Timestamp(r.closed_at).timestamp() * 1000)
        open_ms = close_ms - int(r.hours * 3_600_000)
        usd = float(r.exposure or r.capital_used / 2)
        out = {"id": r.id, "sym": r.symbol, "usd": round(usd), "real_in": e.get("entry_basis_pct"),
               "real_out": e.get("exit_basis_pct"), "sim_in": None, "sim_out": None}
        t = int(np.searchsorted(S.ts, open_ms))
        if t < S.n - 2:
            res = E.leg(S, t, "buy", usd, now_pol, force=True)
            if res and res[0]:
                j, c_ = res[0][0]
                out["sim_in"] = round(((S.pc[j] * (1 - PERP / 100)) / (S.sc[j] * (1 + c_ / 100)) - 1) * 100, 3)
        t2 = int(np.searchsorted(S.ts, close_ms))
        if t2 < S.n - 2:
            res = E.leg(S, max(t2 - 1, 0), "sell", usd, now_pol, force=True)
            if res and res[0]:
                j, c_ = res[0][0]
                # basis de salida en la convención de exec_json: (perp − spot)/spot del cierre
                out["sim_out"] = round(((S.pc[j] * (1 + PERP / 100)) / (S.scf[j] * (1 - c_ / 100)) - 1) * 100, 3)
        rows.append(out)
    return pd.DataFrame(rows)


# ── Reporte ────────────────────────────────────────────────────────────
def fmt(x, d=2):
    return "—" if x is None or (isinstance(x, float) and np.isnan(x)) else f"{x:+.{d}f}" if isinstance(x, float) else str(x)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--workers", type=int, default=6)
    ap.add_argument("--calibrate", action="store_true")
    ap.add_argument("--reuse", action="store_true", help="usar cache/profit_study.pkl")
    ap.add_argument("--stage2", action="store_true", help="combinaciones × leverage × capital por usuario")
    ap.add_argument("--signals", action="store_true", help="reglas del piloto LLM → signal_rules_study")
    a = ap.parse_args()
    sys.stdout.reconfigure(encoding="utf-8")
    if a.signals:
        signals_study(a.workers, a.reuse)
        return
    if a.stage2:
        stage2_report(*stage2(a.workers))
        return
    if a.calibrate:
        c = calibrate()
        print(c.to_string(index=False))
        for col in ("in", "out"):
            ok = c[f"real_{col}"].notna() & c[f"sim_{col}"].notna()
            if ok.any():
                err = (c.loc[ok, f"sim_{col}"] - c.loc[ok, f"real_{col}"]).abs()
                print(f"{col}: n={int(ok.sum())} |error| mediano {err.median():.3f} pp, medio {err.mean():.3f} pp")
        return
    vs = variants()
    sizes = (REF_USD,)
    if a.reuse and OUT.exists():
        df = pd.read_pickle(OUT)
    else:
        df = run(vs, sizes, a.workers)
        df.to_pickle(OUT)
    print(f"{len(df):,} filas", flush=True)
    today = datetime.now(timezone.utc).strftime("%Y%m%d")
    L = [f"# Rentabilidad por operación y por usuario — {today}", "",
         f"Candidatos del modelo vigente (`p_prod` > 0) en {df.sym.nunique()} tokens solo-Alpha con velas 1m, "
         f"{pd.to_datetime(df.t_ms.min(), unit='ms'):%Y-%m-%d} → {pd.to_datetime(df.t_ms.max(), unit='ms'):%Y-%m-%d}. "
         f"Nocional {REF_USD:,.0f} USD por operación, gas {GAS_USD} USD por swap, ρ {E.RHO}, mmr {E.MMR * 100:.0f} %. "
         "Valores en % del nocional; sin entrada = 0. Salida B = dos pagos seguidos ≤ 0 (tope 30 d).", "",
         "## 1. Por operación (frente a P0)", "",
         "| política | n | entra % | media | mediana | p5 | entrada | salida | funding | gas | eventos SL % | rebal. | días | Δ media [IC 95 %] | Δp5 | trim. | pasa |",
         "|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|"]
    gates = {}
    for vi, v in enumerate(vs):
        g = df[(df.vi == vi) & (df.usd == REF_USD)]
        if g.empty:
            continue
        s = trade_stats(g)
        gt = gate(paired(df, vi, REF_USD)) if vi else None
        gates[vi] = gt
        gtxt = (f"{gt['d']:+.2f} [{gt['lo']:+.2f}, {gt['hi']:+.2f}] | {gt['dp5']:+.2f} | {gt['wins']}/{gt['total']} | "
                f"{'sí' if gt['ok'] else 'no'}") if gt else "— | — | — | base"
        L.append(f"| {v.name} | {s['n']} | {s['fire']:.0f} | {s['mean']:+.2f} | {fmt(s['med'])} | {s['p5']:+.2f} | "
                 f"{fmt(s['entry'])} | {fmt(s['exit'])} | {fmt(s['fund'])} | {s['gas']:.2f} | {s['ev']:.1f} | "
                 f"{s['reb']:.2f} | {fmt(s['days'], 1)} | {gtxt} |")

    # Selección (filtros sobre las operaciones de P0, sin re-simular)
    base = df[(df.vi == 0) & (df.usd == REF_USD)].copy()
    base["c_rt"] = 2 * (base.a + base.b * REF_USD / 6) + 2 * PERP
    filters = {
        "S1 gate costo m=0 (pred·7d − costo ≥ 0)": lambda d: d.p_prod / 365 * 7 - d.c_rt >= 0,
        "S1 gate costo m=0.25": lambda d: d.p_prod / 365 * 7 - d.c_rt >= 0.25,
        "S1 gate costo m=0.50": lambda d: d.p_prod / 365 * 7 - d.c_rt >= 0.50,
        "S2 vol7 ≤ p75 de la muestra": lambda d: d.vol7 <= base.vol7.quantile(0.75),
        "S2 recorrido 24 h ≤ 15 %": lambda d: d.rng24 <= 15,
        "S2 recorrido 24 h ≤ 25 %": lambda d: d.rng24 <= 25,
    }
    L += ["", "## 2. Selección: filtros sobre las operaciones de P0", "",
          "| filtro | n | % de P0 | media | p5 | eventos SL % | media de las excluidas |", "|---|---|---|---|---|---|---|"]
    for name, fn in filters.items():
        m = fn(base).fillna(False)
        k, x = base[m], base[~m]
        L.append(f"| {name} | {len(k)} | {len(k) / len(base) * 100:.0f} | {k.net.mean():+.2f} | "
                 f"{np.percentile(k.net, 5):+.2f} | {(k.case > 1).mean() * 100:.1f} | {fmt(x.net.mean())} |")

    # Cartera por usuario
    L += ["", "## 3. Cartera por usuario (5 cupos, 10,000 USD; rentabilidad mensual sobre capital total)", "",
          "Universo = estos tokens solo-Alpha: la utilización es una cota inferior de la de prod (que ve ~100 tokens).",
          "", "| política | leverage | operaciones | media mensual | mediana | p10 | meses ≥ 2.5 % | utilización | DD % | mensual si utilización 90 % |",
          "|---|---|---|---|---|---|---|---|---|---|"]
    port = {}
    for vi, v in enumerate(vs):
        g = df[(df.vi == vi) & (df.usd == REF_USD)]
        if g.empty:
            continue
        p = portfolio(g, 10_000, 5, v.lev)
        if p is None:
            continue
        port[vi] = p
        scale = 90 / p["util"] if p["util"] else np.nan
        L.append(f"| {v.name} | {v.lev:.0f}x | {p['trades']} | {p['mean_m']:+.2f} % | {p['med_m']:+.2f} % | "
                 f"{p['p10_m']:+.2f} % | {p['ge25']:.0f} % | {p['util']:.0f} % | {p['dd']:.1f} | "
                 f"{p['mean_m'] * scale:+.2f} % |")
    for name, fn in filters.items():
        p = portfolio(base, 10_000, 5, 1.0, gate_fn=lambda d, fn=fn: fn(d).fillna(False))
        if p:
            L.append(f"| P0 + {name} | 1x | {p['trades']} | {p['mean_m']:+.2f} % | {p['med_m']:+.2f} % | "
                     f"{p['p10_m']:+.2f} % | {p['ge25']:.0f} % | {p['util']:.0f} % | {p['dd']:.1f} | "
                     f"{p['mean_m'] * 90 / p['util'] if p['util'] else float('nan'):+.2f} % |")
    path = ROOT / "reports" / f"profit_study_{today}.md"
    path.write_text("\n".join(L) + "\n", encoding="utf-8")
    with open(ROOT / "cache" / "profit_study_port.pkl", "wb") as f:
        pickle.dump({"port": port, "gates": gates}, f)
    print("\n".join(L))
    print("Reporte:", path)


if __name__ == "__main__":
    main()
