#!/usr/bin/env python3
"""Límites pareados on-chain + perp para entrar y salir — LOCAL, read-only.

Pregunta del usuario (2026-11-10): manualmente lo que mejor funcionó fue poner una orden límite
on-chain (Kyber) y otra en el perp al MISMO precio (o con margen a favor) y esperar que llenen.
¿Le gana a la ejecución actual ("prod actual" = swap paciente con basis ejecutable ≥ 0.25 % /
≥ −0.15 %, tranche_study) y con qué control si llena una sola pierna?

Mismo motor y unidades que scripts/tranche_study.py (velas 1m sin velas planas, comisión del
pool + impacto por token de cache/binance/alpha_cost.csv, funding liquidado, intentos cada 6 h
con funding ≥ 0.05 %/día, hold 7 d, resultado = ejecución ida y vuelta + funding en % del nocional;
entrada que no llena = 0).

Modelo de la orden pareada (por intento, nocional entero en una sola orden por pierna):
  * Ancla P (mid de perp y on-chain | perp | on-chain) y margen a favor θ:
      entrada: compra límite on-chain a P(1−θ/2), venta límite perp a P(1+θ/2);
      salida:  venta límite on-chain a P(1+θ/2), compra límite perp a P(1−θ/2).
  * Límite Kyber: la llena un arbitrajista cuando el pool pasa el precio por `edge` (calibrado con
    las órdenes reales llenadas: JCT +0.10 %, ARIA +0.68 % → edge = máx(0.10, comisión + 0.10) %,
    sensibilidad 0.3/0.7 %). Capacidad por vela = cuánto mueve el pool ese exceso (pendiente de
    impacto b): llena cuando lo acumulado cubre el nocional. Sin comisión para quien pone la orden.
  * Perp límite post-only: llena si la vela 1m lo cruza (maker 0.02 %). Si al anclar ya cruzaría,
    queda en el mejor precio (llena en la vela siguiente al precio de mercado, maker).
  * Ninguna llena en R minutos → se re-ancla; ventana W. Entrada sin llenar en W = no se entra.
    Salida sin llenar: sigue cobrando y se re-arma (hasta 3 ventanas), después swap + taker.
  * Una pierna sola: `now` = la otra a mercado (taker / swap) en la vela siguiente (≈ piloto
    BscLimitLeg); `keep T/x` = la otra sigue a su precio hasta T min o hasta que el precio se aleje
    x % en contra → mercado; `chase T/x` = la otra pasa al precio de la que llenó (empate) con el
    mismo T/x. `gate`: además dispara por swap si el basis ejecutable cumple el umbral de prod antes
    de que llene alguna pierna (mezcla según libro).

    python -u scripts/paired_limit_study.py [--notional 2100] [--workers 8] [--quick]
Salida: reports/paired_limit_study_YYYYMMDD.md
"""
import argparse
import sys
from concurrent.futures import ProcessPoolExecutor
from dataclasses import dataclass, replace
from datetime import datetime
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
from execution_study import FEE, HOLD, QUARTER_MS  # noqa: E402
from tranche_study import Bars, Policy, attempts, cost_table, trade, universe  # noqa: E402

MAKER = 0.02             # perp maker, %
EDGE_MIN, EDGE_ADD = 0.10, 0.10
RETRIES = 3              # ventanas de salida antes de forzar swap
RHO = 0.8


@dataclass(frozen=True)
class Pair:
    name: str
    anchor: str = "mid"      # mid | perp | spot
    theta: float = 0.2       # % a favor entre las dos órdenes
    R: int = 60              # minutos antes de re-anclar
    W: int = 1440            # ventana de una pierna (entrada / cada intento de salida)
    ctrl: str = "now"        # now | keep | chase
    T: int = 30
    x: float = 1.0
    edge: float = None       # None = máx(EDGE_MIN, comisión + EDGE_ADD)
    gate: bool = False       # además swap si el basis ejecutable cumple el umbral de prod
    perp_limit: bool = True  # False = perp taker al llenar el spot (piloto BscLimitLeg)


def _first(mask):
    if not len(mask):
        return -1
    i = int(np.argmax(mask))
    return i if mask[i] else -1


class Engine:
    def __init__(self, d, B, usd):
        self.d, self.B, self.usd = d, B, usd
        valid = ~np.isnan(B.sc)
        self.sh = np.where(valid, d["sh"], np.nan)
        self.sl = np.where(valid, d["sl"], np.nan)
        self.n = B.n
        self.c_swap = B.a + B.b * usd           # costo del swap del nocional entero

    def ref_spot(self, k):
        last = int(self.B.last[min(k, self.n - 1)])
        return None if last < 0 or k - last > 60 else self.B.sc[last]

    def spot_fill(self, a, b, L, buy, edge):
        """Vela en [a, b) en que la límite on-chain termina de llenar, o −1."""
        if b <= a:
            return -1
        if buy:
            exc = (L * (1 - edge / 100) / self.sl[a:b] - 1) * 100
        else:
            exc = (self.sh[a:b] / (L * (1 + edge / 100)) - 1) * 100
        exc = np.where(np.isnan(exc), 0.0, np.maximum(exc, 0.0))
        bslope = self.B.b if self.B.b > 0 else 1e-9
        cap = np.cumsum(exc / bslope)
        return _first(cap >= self.usd) if exc.any() else -1

    def perp_fill(self, a, b, L, sell):
        if b <= a:
            return -1
        d = self.d
        return _first(d["ph"][a:b] > L) if sell else _first(d["pl"][a:b] < L)

    def market_perp(self, k, sell):
        p = self.d["pc"][min(k, self.n - 1)]
        return p, FEE

    def market_spot(self, k, buy):
        s = self.ref_spot(k)
        if s is None:
            return None
        return s * (1 + self.c_swap / 100) if buy else s * (1 - self.c_swap / 100)

    def leg(self, t0, entry, pol: Pair):
        """(vela de cierre, ejecución %, minutos con una pierna sola, mov. adverso %) o None."""
        d, B = self.d, self.B
        buy_spot = entry
        edge = pol.edge if pol.edge is not None else max(EDGE_MIN, B.a + EDGE_ADD)
        end = min(t0 + pol.W, self.n - 2)
        k = t0
        side = "buy" if entry else "sell"
        while k < end:
            s0, p0 = self.ref_spot(k), d["pc"][k]
            if s0 is None:
                k += 1
                continue
            if pol.gate:
                ex = B.exec_at(side, p0, s0, self.c_swap)
                if ex >= (0.25 if entry else -0.15):
                    return k + 1, ex - FEE, 0, 0.0
            P = {"mid": (s0 + p0) / 2, "perp": p0, "spot": s0}[pol.anchor]
            h = pol.theta / 200
            Ls = P * (1 - h) if entry else P * (1 + h)
            Lp = P * (1 + h) if entry else P * (1 - h)
            # Según libro: cada pierna toma el mejor precio entre su límite y el mercado.
            ms = s0 * (1 + self.c_swap / 100) if entry else s0 * (1 - self.c_swap / 100)
            spot_now = (ms <= Ls) if entry else (ms >= Ls)
            perp_now = pol.perp_limit and ((p0 >= Lp) if entry else (p0 <= Lp))  # post-only en el mejor precio
            if spot_now and perp_now:
                return k + 1, (((p0 / ms) if entry else (ms / p0)) * 100 - 100 - MAKER), 0, 0.0
            seg = min(k + pol.R, end)
            if spot_now:
                js_abs, s_px = k, ms
            else:
                js = self.spot_fill(k + 1, seg + 1, Ls, entry, edge)
                js_abs, s_px = (k + 1 + js, Ls) if js >= 0 else (None, None)
            if perp_now:
                jp_abs, p_px = k, p0
            elif pol.perp_limit:
                jp = self.perp_fill(k + 1, seg + 1, Lp, sell=entry)
                jp_abs, p_px = (k + 1 + jp, Lp) if jp >= 0 else (None, None)
            else:
                jp_abs, p_px = None, None
            if js_abs is None and jp_abs is None:
                if pol.gate:
                    fire = B.fire(side, self.c_swap, 0.25 if entry else -0.15, 1)
                    g = _first(fire[k + 1:seg + 1])
                    if g >= 0:
                        j = k + 1 + g
                        return j + 1, B.exec_at(side, d["pc"][j], self.ref_spot(j), self.c_swap) - FEE, 0, 0.0
                k = seg
                continue
            if not pol.perp_limit:
                # piloto: perp taker al llenar el spot
                if js_abs is None:
                    k = seg
                    continue
                pp, fee = self.market_perp(js_abs + 1, sell=entry)
                return js_abs + 1, (((pp / s_px) if entry else (s_px / pp)) * 100 - 100 - fee), 1, 0.0
            if js_abs is not None and jp_abs is not None and js_abs == jp_abs:
                return js_abs, (((p_px / s_px) if entry else (s_px / p_px)) * 100 - 100 - MAKER), 0, 0.0
            first_spot = jp_abs is None or (js_abs is not None and js_abs < jp_abs)
            j1 = js_abs if first_spot else jp_abs
            filled_px = s_px if first_spot else p_px
            # La otra pierna: su límite (keep) o el precio de la que llenó (chase = empate).
            if first_spot:
                tgt = filled_px if pol.ctrl == "chase" else Lp
            else:
                tgt = filled_px if pol.ctrl == "chase" else Ls
            lim = min(j1 + 1 + (0 if pol.ctrl == "now" else pol.T), self.n - 2)
            j2, how = j1 + 1, "mkt"
            if pol.ctrl != "now":
                a = j1 + 1
                if first_spot:   # falta el perp
                    w = self.perp_fill(a, lim, tgt, sell=entry)
                    ref = d["pc"][a:lim]
                    adverse = (ref < tgt * (1 - pol.x / 100)) if entry else (ref > tgt * (1 + pol.x / 100))
                else:            # falta el spot
                    w = self.spot_fill(a, lim, tgt, entry, edge)
                    sc = B.sc[a:lim]
                    with np.errstate(invalid="ignore"):
                        adverse = (sc > tgt * (1 + pol.x / 100)) if entry else (sc < tgt * (1 - pol.x / 100))
                    adverse = np.where(np.isnan(sc), False, adverse)
                st = _first(adverse)
                if w >= 0 and (st < 0 or w < st):
                    j2, how = a + w, "lim"
                else:
                    j2 = a + st if st >= 0 else lim
            if first_spot:
                spot_px = filled_px
                perp_px, pfee = (tgt, MAKER) if how == "lim" else self.market_perp(j2, sell=entry)
            else:
                perp_px, pfee = filled_px, MAKER
                spot_px = tgt if how == "lim" else self.market_spot(j2, entry)
                if spot_px is None:
                    return None
            ex = ((perp_px / spot_px) if entry else (spot_px / perp_px)) * 100 - 100 - pfee
            seg_p = d["pc"][j1:j2 + 1]
            if first_spot == entry:   # largo spot sin cubrir: riesgo de caída
                adv = (1 - np.nanmin(seg_p) / d["pc"][j1]) * 100
            else:                     # corto sin spot: riesgo de subida
                adv = (np.nanmax(seg_p) / d["pc"][j1] - 1) * 100
            return j2, ex, j2 - j1, max(adv, 0.0)
        return None

    def round_trip(self, t, pol: Pair):
        """(neto, ejecución, funding, entró, min_sola, adverso) en % del nocional."""
        B = self.B
        e = self.leg(t, True, pol)
        if e is None:
            return 0.0, 0.0, 0.0, 0.0, 0.0, 0.0
        je, xe, me, ae = e
        start, x = max(t + HOLD, je + 1), None
        for _ in range(RETRIES):
            x = self.leg(start, False, pol)
            if x is not None:
                break
            start += pol.W
        if x is None:
            k = min(start, self.n - 2)
            s, p = self.ref_spot(k), self.d["pc"][k]
            if s is None:
                return None
            x = (k + 1, B.exec_at("sell", p, s, self.c_swap) - FEE, 0, 0.0)
        jx, xx, mx, ax = x
        jx = min(jx, self.n - 1)
        fund = B.fc[jx] - B.fc[min(je, self.n - 1)]
        return xe + xx + fund, xe + xx, fund, 1.0, me + mx, max(ae, ax)


def policies(quick=False):
    out = [Pair("piloto: límite on-chain anclada al perp + perp taker", anchor="perp", theta=0.4, R=10,
                W=360, perp_limit=False)]
    anchors = ("mid", "perp") if not quick else ("mid",)
    thetas = (0.0, 0.2, 0.4, 0.6)
    for an in anchors:
        for th in thetas:
            for R in (15, 60):
                for W in (360, 1440):
                    for ctrl in ("now", "keep", "chase"):
                        out.append(Pair(f"{an} θ{th} R{R} W{W // 60}h {ctrl}", anchor=an, theta=th, R=R,
                                        W=W, ctrl=ctrl))
    return out


def refine(best):
    out = []
    for b in best:
        for ctrl in ("keep", "chase"):
            for T in (5, 30, 120):
                for x in (0.5, 1.0, 2.0):
                    out.append(replace(b, name=f"{b.name.rsplit(' ', 1)[0]} {ctrl} T{T} x{x}", ctrl=ctrl, T=T, x=x))
        out.append(replace(b, name=b.name + " +gate swap", gate=True))
        for e in (0.3, 0.7):
            out.append(replace(b, name=b.name + f" edge {e}", edge=e))
    return out


# ── corrida (por token en paralelo) ─────────────────────────────────────
_SHARED = {}


def _token(job):
    sym, d, f_ts, f_pct, daily, usd, min_daily, pols, c = job
    B = Bars(d, c, f_ts, f_pct, True)
    eng = Engine(d, B, usd)
    rows = []
    for t in attempts(d, f_ts, daily, min_daily):
        ms = int(d["ts"][t])
        base = {"sym": sym, "q": ms // QUARTER_MS, "t": t}
        for name, pol in (("prod actual", Policy("prod actual")), ("ejecutar ahora", Policy("ahora", wait=False))):
            r = trade(B, t, usd, pol, RHO)
            if r is not None:
                rows.append({**base, "pol": name, "net": r[0], "exec": r[1], "fund": r[2], "in": float(r[4]),
                             "solo": 0.0, "adv": 0.0})
        for p in pols:
            r = eng.round_trip(t, p)
            if r is not None:
                rows.append({**base, "pol": p.name, "net": r[0], "exec": r[1], "fund": r[2], "in": r[3],
                             "solo": r[4], "adv": r[5]})
    return rows


def run(args, pols):
    cost = cost_table()
    jobs = [(sym, d, f_ts, f_pct, daily, args.notional, args.min_daily, pols, cost(sym))
            for sym, d, f_ts, f_pct, daily in universe(args)]
    rows = []
    with ProcessPoolExecutor(args.workers) as ex:
        for r in ex.map(_token, jobs):
            rows += r
            print(f"  {r[0]['sym'] if r else '?'}: {len(r)} filas", flush=True)
    return pd.DataFrame(rows)


def summarize(df):
    g = df.groupby("pol")
    out = pd.DataFrame({
        "n": g.size(), "entra": g["in"].mean() * 100, "media": g.net.mean(), "mediana": g.net.median(),
        "p5": g.net.quantile(0.05), "> 0": g.net.apply(lambda v: (v > 0).mean() * 100),
        "ejec": g["exec"].mean(), "funding": g.fund.mean(),
        "min sola p95": g.solo.quantile(0.95), "adverso p95": g.adv.quantile(0.95)})
    return out.sort_values("media", ascending=False)


def quarters(df, names, base="prod actual"):
    q = df.pivot_table(index="q", columns="pol", values="net", aggfunc="mean")
    out = {}
    for n in names:
        if n in q:
            out[n] = int((q[n] >= q[base]).sum()), int(q[n].notna().sum())
    return out


def walk_forward(df, names):
    qs = sorted(df.q.unique())
    oos, picks = [], []
    for i, q in enumerate(qs[1:], 1):
        past = df[df.q.isin(qs[:i]) & df.pol.isin(names)]
        best = past.groupby("pol").net.mean().idxmax()
        picks.append((q, best))
        oos.append(df[(df.q == q) & (df.pol == best)].net)
    v = pd.concat(oos) if oos else pd.Series(dtype=float)
    ref = df[df.q.isin(qs[1:]) & (df.pol == "prod actual")].net
    return picks, v, ref


def passes(s, base, entra_min=90.0):
    return (s["media"] >= base["media"]) and (s["p5"] >= base["p5"] - 1.0) and (s["entra"] >= entra_min)


def report(args, s1, s2, df, qwin, wf) -> str:
    base = s1.loc["prod actual"]
    L = [f"# Límites pareados on-chain + perp — {datetime.now():%Y-%m-%d}", "",
         f"Nocional {args.notional:,.0f} USD, 1x, intentos cada 6 h con funding ≥ {args.min_daily} %/día, hold 7 d, "
         "21 tokens Alpha (2025-10 → 2026-09). Mismo motor que `tranche_study`: velas planas fuera, comisión del pool + "
         "impacto por token, funding liquidado. Resultado por intento = ejecución ida y vuelta + funding (% del "
         "nocional); entrada que no llena = 0.", "",
         "Criterio (usuario): media ≥ `prod actual`, p5 ≥ p5 de prod − 1 pp y ≥ 90 % de intentos con entrada.", ""]

    def table(s, title, top=None):
        rows = s if top is None else s.head(top)
        L.extend([f"## {title}", "",
                  "| Política | n | Entra | Media | Mediana | p5 | > 0 | Ejecución | Funding | min sola p95 | adverso p95 | pasa |",
                  "|---|---|---|---|---|---|---|---|---|---|---|---|"])
        keep = list(rows.index)
        for extra in ("prod actual", "ejecutar ahora"):
            if extra not in keep and extra in s.index:
                keep.append(extra)
        for name in keep:
            r = s.loc[name]
            ok = "**sí**" if name not in ("prod actual", "ejecutar ahora") and passes(r, base) else ""
            L.append(f"| {name} | {int(r.n)} | {r.entra:.0f} % | {r.media:+.2f} | {r.mediana:+.2f} | {r.p5:+.2f} | "
                     f"{r['> 0']:.0f} % | {r.ejec:+.2f} | {r.funding:+.2f} | {r['min sola p95']:.0f} | "
                     f"{r['adverso p95']:.2f} % | {ok} |")
        L.append("")

    table(s1, "Barrido inicial (orden única por pierna)", top=25)
    table(s2, "Refinamiento del control, gate por swap y sensibilidad del edge", top=30)
    L += ["## Trimestres ≥ prod (media por trimestre)", ""]
    for n, (w, t) in sorted(qwin.items(), key=lambda kv: -kv[1][0])[:15]:
        L.append(f"- {n}: {w}/{t}")
    picks, oos, ref = wf
    L += ["", "## Walk-forward (elige la mejor media de los trimestres anteriores)", ""]
    L += [f"- Trimestre {q}: {p}" for q, p in picks]
    if len(oos):
        L.append(f"\nFuera de muestra: media {oos.mean():+.2f}, p5 {oos.quantile(0.05):+.2f} (n {len(oos)}); "
                 f"prod en los mismos trimestres: media {ref.mean():+.2f}, p5 {ref.quantile(0.05):+.2f}.")
    L += ["", "## Fees: una orden vs tramos", "",
          "- Límite Kyber: quien pone la orden no paga comisión ni impacto; el arbitrajista que la llena paga el pool "
          "y el gas. Una orden única (todo el nocional) no cuesta más que en tramos: llena por partes a medida que el "
          "pool pasa el precio (ARIA: 13 llenados de una orden).",
          "- Swap: la comisión del pool es un % del nocional (igual en 1 o 6 tramos); trocear solo suma gas "
          "(~0.05–0.1 USD por tx) y reduce el impacto, que crece con el tamaño de cada swap.",
          f"- Perp: maker {MAKER} % vs taker {FEE} %: la límite ahorra {FEE - MAKER:.2f} pp por pierna.", "",
          "## Diferencias con `execution_study_20261108` (\"dos límites mismo precio\" −0.73 %)", "",
          "- Allí el precio era el punto medio sin margen a favor (mejor caso −0.04 %); aquí θ ∈ {0, 0.2, 0.4, 0.6} %.",
          "- Allí la pierna que faltaba se cubría a mercado con el impacto del nocional entero y se excluían los "
          "intentos sin llenado; aquí hay controles (keep/chase con T/x), el no-llenado cuenta como 0 y el spot "
          "llena por capacidad del pool.",
          "- Allí las velas planas (sin trades) contaban como llenados; aquí se descartan y la límite Kyber exige el "
          "edge calibrado con órdenes reales.", ""]
    return "\n".join(L)


def main():
    sys.stdout.reconfigure(encoding="utf-8")
    ap = argparse.ArgumentParser()
    ap.add_argument("--notional", type=float, default=2100)
    ap.add_argument("--min-daily", type=float, default=0.05)
    ap.add_argument("--workers", type=int, default=8)
    ap.add_argument("--quick", action="store_true", help="barrido reducido (prueba)")
    ap.add_argument("--tag", default="")
    args = ap.parse_args()
    pols = policies(args.quick)
    print(f"Barrido: {len(pols)} políticas", flush=True)
    df1 = run(args, pols)
    s1 = summarize(df1)
    cands = [p for p in pols if p.perp_limit and p.name in s1.index]
    best = sorted(cands, key=lambda p: -s1.loc[p.name, "media"])[:4]
    pols2 = refine(best)
    print(f"Refinamiento: {len(pols2)} políticas sobre {[b.name for b in best]}", flush=True)
    df2 = run(args, pols2)
    df = pd.concat([df1, df2[~df2.pol.isin(["prod actual", "ejecutar ahora"])]])
    s2 = summarize(pd.concat([df2]))
    allnames = [n for n in summarize(df).index if n not in ("prod actual", "ejecutar ahora")]
    qwin = quarters(df, allnames)
    wf = walk_forward(df, allnames)
    tag = f"_{args.tag}" if args.tag else ""
    out = ROOT / "reports" / f"paired_limit_study_{datetime.now():%Y%m%d}{tag}.md"
    out.write_text(report(args, s1, s2, df, qwin, wf), encoding="utf-8")
    df.to_pickle(ROOT / "cache" / f"paired_limit_rows{tag}.pkl")
    print(f"-> {out}")


if __name__ == "__main__":
    main()
