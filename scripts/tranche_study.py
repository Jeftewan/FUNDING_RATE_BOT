#!/usr/bin/env python3
"""¿Revalidar el basis antes de cada tramo mejora el neto on-chain? — LOCAL, read-only.

Corrige tres sesgos de scripts/execution_study.py, que se reportan como cascada:
  1. No cobraba la comisión del pool (alpha_depth mide impacto puro).
  2. Tomaba como precio las velas 1m planas (sin trades): cierres viejos contra un perp que se
     mueve son basis fantasma.
  3. Ejecutaba todos los tramos en una vela, y descartaba las esperas que no disparaban.

Modelo: cada tramo en su propia vela con costo = comisión + impacto + empuje propio acumulado.
Umbral sobre basis ejecutable, como prod. Entrada sin disparo = 0. Salida vencida: sigue cobrando
y se re-arma (máx. 4 ventanas). Funding solo sobre lo llenado. Todo en % del nocional objetivo.

    python scripts/tranche_study.py --refresh-cost      # cotizaciones públicas actuales
    python -u scripts/tranche_study.py [--notional 2100]
Salida: reports/tranche_study_YYYYMMDD.md
"""
import argparse
import json
import math
import sys
import time
from collections import defaultdict
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd
import requests

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
from binance_dataset import OUT_CSV  # noqa: E402
from execution_study import (DEPTH_CSV, FEE, HOLD, QUARTER_MS, STEP, Sim, impact_fn, load,  # noqa: E402
                             signal, symbols_months)

COST_CSV = ROOT / "cache" / "binance" / "alpha_cost.csv"
TOKENS = ROOT / "cache" / "binance" / "alpha_tokens.json"
KYBER = "https://aggregator-api.kyberswap.com/bsc/api/v1/routes"
USDT = "0x55d398326f99059fF775485246999027B3197955"
SIZES = (400, 1000, 2100, 3000)
W = 360
SLICE_IMPACT = 0.15
SLICE_MAX = 6
RHOS = (0.0, 0.8, 1.0)     # 0.8 = calibrado con el job #35 (scripts/execution_decomp.py), n=5
MIN_QUARTER = 30
WEEK_MS = 7 * 86400 * 1000


# ── Tabla de costos ─────────────────────────────────────────────────────
def _quote(src, dst, wei):
    for wait in (0, 3, 10):
        time.sleep(wait + 0.6)
        r = requests.get(KYBER, params={"tokenIn": src, "tokenOut": dst, "amountIn": str(int(wei))},
                         headers={"x-client-id": "basyo-study"}, timeout=20)
        if r.status_code == 200 and r.json().get("data"):
            return int(r.json()["data"]["routeSummary"]["amountOut"])
    return None


def refresh_cost():
    """comisión a (%) = medio spread a 25 USD; pendiente b (% por USD) sobre ese piso."""
    tokens = {t["symbol"].upper(): t for t in json.load(open(TOKENS, encoding="utf-8")) if str(t.get("chainId")) == "56"}
    rows = []
    for sym in sorted(symbols_months()):
        t = tokens.get(sym.upper())
        if not t:
            print(sym, "sin contrato")
            continue
        addr, dec = t["contractAddress"], int(t["decimals"])
        got = _quote(USDT, addr, 25e18)
        if not got:
            print(sym, "sin cotización")
            continue
        buy = 25 / (got / 10 ** dec)
        back = _quote(addr, USDT, got)
        if not back:
            continue
        sell = (back / 1e18) / (got / 10 ** dec)
        mid = (buy + sell) / 2
        a = max(0.0, (buy - sell) / (2 * mid) * 100)
        xs, ys = [], []
        for s in SIZES:
            out = _quote(USDT, addr, s * 1e18)
            if out:
                xs.append(s)
                ys.append(max(0.0, (s / (out / 10 ** dec) / mid - 1) * 100 - a))
        x, y = np.array(xs, float), np.array(ys, float)
        b = float((x * y).sum() / (x * x).sum()) if len(x) else np.nan
        rows.append({"symbol": sym, "fee_pct": a, "slope_pct_per_usd": b, "imp_2100": b * 2100})
        print(f"{sym}: comisión {a:.3f} %  impacto 2100 USD {b * 2100:.3f} %", flush=True)
    pd.DataFrame(rows).to_csv(COST_CSV, index=False)
    print("Guardado", COST_CSV)


def cost_table():
    df = pd.read_csv(COST_CSV).dropna()
    med = (float(df.fee_pct.median()), float(df.slope_pct_per_usd.median()))
    table = {r.symbol: (r.fee_pct, r.slope_pct_per_usd) for r in df.itertuples()}
    return lambda sym: table.get(sym, med)


# ── Políticas ───────────────────────────────────────────────────────────
@dataclass(frozen=True)
class Policy:
    name: str
    wait: bool = True
    theta_e: float = 0.25
    theta_x: float = -0.15
    confirm: object = 1          # lecturas seguidas, o "med3" (mediana de 3 lecturas)
    reval_e: float = None        # None = sin revalidar
    pause_e: int = 0             # minutos de pausa; 0 = parar y quedarse parcial
    reval_x: float = None
    pause_x: int = W
    gap: int = 1                 # velas entre tramos
    net_slices: bool = False     # True: el troceo no cuenta la comisión


def policies():
    base = Policy("prod actual")
    out = [Policy("ejecutar ahora", wait=False), base]
    for th, label in ((0.25, "θ"), (0.10, "θ−0.15"), (0.0, "≥0")):
        for pause, plabel in ((0, "parar"), (30, "pausa 30m"), (W, "pausa ventana")):
            out.append(replace(base, name=f"reval entrada {label}, {plabel}", reval_e=th, pause_e=pause))
    for th, label in ((-0.15, "θ"), (-0.30, "θ−0.15")):
        for pause, plabel in ((30, "pausa 30m"), (W, "pausa ventana")):
            out.append(replace(base, name=f"reval salida {label}, {plabel}", reval_x=th, pause_x=pause))
    for c in (2, 3, "med3"):
        out.append(replace(base, name=f"confirmación {c}", confirm=c))
    for g in (2, 5):
        out.append(replace(base, name=f"tramos cada {g} min", gap=g))
    out.append(replace(base, name="troceo sin contar comisión", net_slices=True))
    return out


# ── Motor ───────────────────────────────────────────────────────────────
class Bars:
    def __init__(self, d, cost, f_ts, f_pct, strict=True):
        self.ts, self.pc = d["ts"], d["pc"]
        sc = d["sc"].copy()
        if strict:
            prev = np.r_[np.nan, sc[:-1]]
            sc[(d["sh"] == d["sl"]) & (sc == prev)] = np.nan
        self.sc = sc
        self.a, self.b = cost
        self.n = len(sc)
        ok = ~np.isnan(sc)
        self.idx = np.where(ok)[0]
        self.last = np.maximum.accumulate(np.where(ok, np.arange(self.n), -1))
        self.fc = np.r_[0, np.cumsum(f_pct)][np.searchsorted(f_ts, self.ts, "right")]
        self._fire = {}

    def price_bar(self, k):
        """Primer print en [k, k+15]; si no, el último en [k−60, k]. None si no hay referencia."""
        p = np.searchsorted(self.idx, k)
        if p < len(self.idx) and self.idx[p] <= k + 15:
            return int(self.idx[p])
        k = min(k, self.n - 1)
        last = int(self.last[k])
        return last if last >= 0 and k - last <= 60 else None

    def fire(self, side, c, theta, confirm):
        key = (side, round(c, 5), theta, confirm)
        if key in self._fire:
            return self._fire[key]
        with np.errstate(invalid="ignore"):
            if side == "buy":
                ex = (self.pc / (self.sc * (1 + c / 100)) - 1) * 100
            else:
                ex = (self.sc * (1 - c / 100) / self.pc - 1) * 100
            if confirm == "med3":
                ok = signal(ex) >= theta
            elif confirm == 1:
                ok = ex >= theta
            else:
                # N prints seguidos sobre el umbral, en un lapso corto
                vals = ex[self.idx] >= theta
                good = np.convolve(vals.astype(int), np.ones(confirm, int), "valid") == confirm
                span = self.idx[confirm - 1:] - self.idx[:len(self.idx) - confirm + 1] <= confirm + 4
                ok = np.zeros(self.n, bool)
                ok[self.idx[confirm - 1:][good & span]] = True
        self._fire[key] = ok
        return ok

    def exec_at(self, side, pc, sc, cost):
        if side == "buy":
            return (pc / (sc * (1 + cost / 100)) - 1) * 100
        return (sc * (1 - cost / 100) / pc - 1) * 100


def n_slices(B, usd, net):
    imp = B.b * usd + (0 if net else B.a)
    return max(1, min(SLICE_MAX, math.ceil(imp / SLICE_IMPACT))) if imp > 0 else 1


def leg(B, start, side, usd, pol, rho, force=False):
    """[(barra, exec %)], n o None si no disparó en la ventana."""
    n = n_slices(B, usd, pol.net_slices)
    u, c = usd / n, B.a + B.b * usd / n
    buying = side == "buy"
    theta = pol.theta_e if buying else pol.theta_x
    reval = None if force else (pol.reval_e if buying else pol.reval_x)
    pause = pol.pause_e if buying else pol.pause_x
    t_end = min(start + W, B.n - 1)
    if force or not pol.wait:
        i = start
    else:
        fired = B.fire(side, c, theta, pol.confirm)[start:t_end]
        if not fired.any():
            return None
        i = start + int(fired.argmax())
    fills, push, k, prev = [], 0.0, i, i
    for s in range(n):
        target = k + 1 if s == 0 else k + pol.gap
        if target >= B.n:
            break
        if s > 0 and reval is not None:
            found = False
            for m in range(target, min(target + pause, max(t_end, target)) + 1):
                if m >= B.n:
                    break
                last = int(B.last[m])
                if last < 0 or m - last > 5:
                    continue
                p = push * rho ** max(m - prev, 1)
                if B.exec_at(side, B.pc[m], B.sc[last], c + p) >= reval:
                    found, target = True, m + 1      # llena en la vela siguiente, como el disparo
                    break
            if not found or target >= B.n:
                break
        j = B.price_bar(target)
        if j is None:
            break
        push = push * rho ** max(j - prev, 1) if s else 0.0
        fills.append((j, B.exec_at(side, B.pc[j], B.sc[j], c + push) - FEE))
        push += 2 * B.b * u
        prev, k = j, max(target, j)
    return fills, n


def trade(B, t, usd, pol, rho):
    """(neto, ejecución, funding, fracción, disparó) en % del nocional objetivo."""
    res = leg(B, t, "buy", usd, pol, rho)
    if res is None or not res[0]:
        return 0.0, 0.0, 0.0, 0.0, False
    fills, n = res
    frac = len(fills) / n
    execu = sum(e for _, e in fills) / n
    fund = -sum(B.fc[j] for j, _ in fills) / n
    left, start, last = frac, t + HOLD, None
    for attempt in range(4):
        force = attempt == 3 or not pol.wait
        out = leg(B, start, "sell", left * usd, pol, rho, force=force)
        if out is None:
            start += W
            continue
        xf, xn = out
        w = left / xn
        execu += sum(e for _, e in xf) * w
        fund += sum(B.fc[j] for j, _ in xf) * w
        if xf:
            last = xf[-1]
        left -= len(xf) * w
        if left <= 1e-9:
            break
        start = max(start + W, (xf[-1][0] + 1) if xf else 0)
    if left > 1e-9:
        if last is None:
            return None
        execu += last[1] * left
        fund += B.fc[last[0]] * left
    return execu + fund, execu, fund, frac, True


# ── Corrida ─────────────────────────────────────────────────────────────
def universe(args):
    syms = symbols_months()
    fr = pd.read_csv(OUT_CSV, usecols=["symbol", "rate", "interval_hours", "funding_ts"])
    fr = fr[fr.symbol.isin(syms)].sort_values("funding_ts")
    groups = {s: g for s, g in fr.groupby("symbol")}
    for sym, months in sorted(syms.items()):
        d = load(sym, months)
        g = groups.get(sym)
        if d is None or g is None or len(d["ts"]) < 40000:
            continue
        with np.errstate(invalid="ignore"):
            if np.nanmean(np.abs(d["pc"] / d["sc"] - 1) * 100) > 3:
                continue
        f_ts, f_pct = g.funding_ts.to_numpy(np.int64), g.rate.to_numpy(float) * 100
        daily = f_pct * 24 / g.interval_hours.to_numpy(float)
        yield sym, d, f_ts, f_pct, daily


def attempts(d, f_ts, daily, min_daily):
    for t in range(1440, len(d["ts"]) - HOLD - 1500, STEP):
        k = np.searchsorted(f_ts, d["ts"][t], "right")
        if k >= 3 and daily[k - 3:k].mean() >= min_daily:
            yield t


def waterfall(args):
    """Efecto de cada corrección sobre el estudio del 2026-11-08 (mismas reglas, 1600 USD, 4 tramos)."""
    impact, cost = impact_fn(pd.read_csv(DEPTH_CSV)), cost_table()
    steps = ("original", "+ comisión del pool", "+ sin velas planas")
    out = {s: defaultdict(list) for s in steps}
    for sym, d, f_ts, f_pct, daily in universe(args):
        imp = impact(sym, 1600 / 4)
        fee = cost(sym)[0]
        strict = dict(d)
        sc = d["sc"].copy()
        sc[(d["sh"] == d["sl"]) & (sc == np.r_[np.nan, sc[:-1]])] = np.nan
        strict["sc"] = sc
        for step, data, cst in ((steps[0], d, imp), (steps[1], d, imp + fee), (steps[2], strict, imp + fee)):
            sim = Sim(data, f_ts, f_pct)
            for t in attempts(d, f_ts, daily, args.min_daily):
                T = t + HOLD
                base = sim.funding(t, T)
                for name, res in (("espera 6 h", sim.patient(t, T, W, 0.4, cst)),
                                  ("inmediato", sim.immediate(t, T, cst))):
                    if res is not None:
                        out[step][name].append(res[0] + sim.funding(res[1], res[2]) - base)
    return out


def run(args, pols, rhos, strict=True):
    cost = cost_table()
    data, meta = defaultdict(list), []
    for s_i, (sym, d, f_ts, f_pct, daily) in enumerate(universe(args)):
        B = Bars(d, cost(sym), f_ts, f_pct, strict)
        for t in attempts(d, f_ts, daily, args.min_daily):
            ms = int(d["ts"][t])
            meta.append((s_i, ms // QUARTER_MS, ms // WEEK_MS))
            for pol in pols:
                if not pol.wait and rhos[0] != rhos[-1]:
                    pass
                for rho in rhos:
                    r = trade(B, t, args.notional, pol, rho)
                    data[pol.name, rho].append((np.nan,) * 5 if r is None else r)
        print(f"{sym}: {len(meta)} intentos", flush=True)
    return {k: np.array(v, float) for k, v in data.items()}, np.array(meta, np.int64)


# ── Evaluación ──────────────────────────────────────────────────────────
def summary(a):
    ok = ~np.isnan(a[:, 0])
    a = a[ok]
    fired = a[:, 4] > 0
    fund, execu = a[fired, 2].mean(), a[fired, 1].mean()
    return {"n": len(a), "fire": fired.mean() * 100, "mean": a[:, 0].mean(), "median": np.median(a[:, 0]),
            "med_f": np.median(a[fired, 0]),
            "p5": np.percentile(a[:, 0], 5), "win": (a[:, 0] > 0).mean() * 100, "frac": a[fired, 3].mean() * 100,
            "exec": execu, "fund": fund, "leak": -execu / fund * 100 if fund else float("nan")}


def gate(cand, base, meta, seed=7):
    """Criterio fijado en el plan, salvo la fracción llenada, que se evalúa aparte."""
    quarters = sorted(set(meta[:, 1]))
    wins = total = 0
    for q in quarters:
        m = (meta[:, 1] == q) & ~np.isnan(cand[:, 0]) & ~np.isnan(base[:, 0])
        if m.sum() < MIN_QUARTER:
            continue
        total += 1
        wins += bool(np.median(cand[m, 0]) - np.median(base[m, 0]) >= 0.05
                     and np.percentile(cand[m, 0], 5) >= np.percentile(base[m, 0], 5))
    ok = ~np.isnan(cand[:, 0]) & ~np.isnan(base[:, 0])
    blocks = meta[ok, 0] * 100000 + meta[ok, 2]
    diff = cand[ok, 0] - base[ok, 0]
    frame = pd.DataFrame({"b": blocks, "d": diff}).groupby("b").d.sum()
    sizes = pd.Series(blocks).value_counts().reindex(frame.index).to_numpy(float)
    vals, rng = frame.to_numpy(float), np.random.default_rng(seed)
    boots = []
    for _ in range(1000):
        p = rng.integers(0, len(vals), len(vals))
        boots.append(vals[p].sum() / sizes[p].sum())
    return {"wins": wins, "total": total, "ci": (float(np.percentile(boots, 2.5)), float(np.percentile(boots, 97.5))),
            "diff_mean": float(diff.mean())}


def walk_forward(data, meta, names, rho):
    quarters = sorted(set(meta[:, 1]))
    picks, oos, ref = [], [], []
    for i, q in enumerate(quarters[1:], 1):
        past = np.isin(meta[:, 1], quarters[:i])
        best = max(names, key=lambda n: np.nanmedian(data[n, rho][past, 0]))
        now = meta[:, 1] == q
        picks.append((int(q), best))
        oos.append(data[best, rho][now, 0])
        ref.append(data["prod actual", rho][now, 0])
    return picks, np.concatenate(oos), np.concatenate(ref)


def fmt(s):
    return (f"{s['n']} | {s['fire']:.0f} % | {s['frac']:.0f} % | {s['mean']:+.2f} | {s['median']:+.2f} | {s['med_f']:+.2f} | {s['p5']:+.2f} | "
            f"{s['win']:.0f} % | {s['exec']:+.2f} | {s['fund']:+.2f} | {s['leak']:.0f} %")


def report(args, water, data, meta, pols, legacy):
    head = ("| Política | n | Dispara | Llenado | Media | Mediana | Mediana disparados | p5 | > 0 | Ejecución | Funding | Fuga |\n"
            "|---|---|---|---|---|---|---|---|---|---|---|---|")
    L = [f"# Estudio de tramos on-chain — {datetime.now(timezone.utc):%Y-%m-%d}", "",
         f"Nocional objetivo {args.notional:g} USD, funding ≥ {args.min_daily} %/día, hold 7 d, ventana 6 h. "
         "% del nocional objetivo. Ejecución y funding: media de los intentos que dispararon. "
         "Fuga = costo de ejecución ÷ funding cobrado.", "",
         "## 1. Cascada sobre el estudio del 2026-11-08", "",
         "Mismas reglas del estudio (basis a mid ≥ 0.4, 1600 USD, 4 tramos); valor frente a no esperar, sin contar filas sin disparo.", "",
         "| Paso | Espera 6 h: mediana | p5 | n | Inmediato: mediana | p5 |", "|---|---|---|---|---|---|"]
    for step, res in water.items():
        p, i = np.array(res["espera 6 h"]), np.array(res["inmediato"])
        L.append(f"| {step} | {np.median(p):+.2f} | {np.percentile(p, 5):+.2f} | {len(p)} | "
                 f"{np.median(i):+.2f} | {np.percentile(i, 5):+.2f} |")
    L += ["", "## 2. Políticas (solo velas con trades, ρ calibrado 0.8)", "", head]
    rows = sorted(((p.name, summary(data[p.name, 0.8])) for p in pols), key=lambda kv: -kv[1]["median"])
    L += [f"| {n} | {fmt(s)} |" for n, s in rows]
    L += ["", "## 3. Criterio frente a `prod actual`", "",
          "| Política | Trimestres ρ 0.8 | Trimestres ρ 1 | Δ medio | IC 95 % | Llenado ≥ 80 % | Pasa |", "|---|---|---|---|---|---|---|"]
    passed = []
    for p in pols:
        if p.name == "prod actual":
            continue
        g8, g1 = (gate(data[p.name, r], data["prod actual", r], meta) for r in (0.8, 1.0))
        frac = summary(data[p.name, 0.8])["frac"]
        ok = (g8["total"] and g8["wins"] > g8["total"] / 2 and g1["wins"] > g1["total"] / 2
              and g8["ci"][0] > 0 and frac >= 80)
        if ok:
            passed.append(p.name)
        L.append(f"| {p.name} | {g8['wins']}/{g8['total']} | {g1['wins']}/{g1['total']} | {g8['diff_mean']:+.3f} | "
                 f"{g8['ci'][0]:+.3f} a {g8['ci'][1]:+.3f} | {frac:.0f} % | {'**SÍ**' if ok else 'no'} |")
    names = [p.name for p in pols]
    picks, oos, ref = walk_forward(data, meta, names, 0.8)
    L += ["", "## 4. Walk-forward (elige por mediana de trimestres anteriores, ρ 0.8)", ""]
    L += [f"- Trimestre {q}: {n}" for q, n in picks]
    L += ["", f"Fuera de muestra: mediana {np.nanmedian(oos):+.2f}, p5 {np.nanpercentile(oos, 5):+.2f}; "
          f"`prod actual` en los mismos trimestres: {np.nanmedian(ref):+.2f}, p5 {np.nanpercentile(ref, 5):+.2f}.", ""]
    L += ["## 5. Sensibilidad", "", "| Escenario de `prod actual` | Mediana | p5 | Fuga |", "|---|---|---|---|"]
    for r in RHOS:
        s = summary(data["prod actual", r])
        L.append(f"| velas con trades, ρ {r:g} | {s['median']:+.2f} | {s['p5']:+.2f} | {s['leak']:.0f} % |")
    if legacy:
        s = summary(legacy["prod actual", 0.8])
        L.append(f"| todas las velas (planas incluidas), ρ 0.8 | {s['median']:+.2f} | {s['p5']:+.2f} | {s['leak']:.0f} % |")
    L += ["", "## Veredicto", "",
          (f"Pasan el criterio: {', '.join(passed)}." if passed else "Ninguna política pasa el criterio: no se cambia la ejecución."), ""]
    path = ROOT / "reports" / f"tranche_study_{datetime.now(timezone.utc):%Y%m%d}{args.tag}.md"
    path.write_text("\n".join(L) + "\n", encoding="utf-8")
    print("\n".join(L))
    print("Reporte:", path)


def main():
    sys.stdout.reconfigure(encoding="utf-8")
    ap = argparse.ArgumentParser()
    ap.add_argument("--notional", type=float, default=2100)
    ap.add_argument("--min-daily", type=float, default=0.05)
    ap.add_argument("--refresh-cost", action="store_true")
    ap.add_argument("--waterfall-only", action="store_true")
    ap.add_argument("--tag", default="")
    args = ap.parse_args()
    if args.refresh_cost:
        return refresh_cost()
    water = waterfall(args)
    if args.waterfall_only:
        for step, res in water.items():
            for name, v in res.items():
                print(f"{step:<24} {name:<12} n={len(v)} mediana {np.median(v):+.2f} p5 {np.percentile(v, 5):+.2f}")
        return None
    pols = policies()
    data, meta = run(args, pols, RHOS)
    legacy, _ = run(args, [Policy("prod actual")], (0.8,), strict=False)
    report(args, water, data, meta, pols, legacy)


if __name__ == "__main__":
    main()
