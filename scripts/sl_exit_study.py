#!/usr/bin/env python3
"""Salida pareada anticipada antes del SL (soft exit) — LOCAL, read-only.

Motivo: cuando el SL del short dispara, la salida es asimétrica (STOP_MARKET recompra el perp
sobre el trigger; el spot on-chain depende de una límite Kyber a sl·(1−δ) que puede no
llenarse → swap taker). ARIA #56 (2026-11-10): −24.68 USD de precio. Propuesta del usuario:
antes de llegar al SL, empezar a cerrar las dos piernas a la vez, por tramos, como en las
entradas/salidas normales, dejando el SL duro como respaldo anti-liquidación.

Motor: el de scripts/margin_topup_study.py (velas 1m perp/mark de data.binance.vision +
on-chain Alpha, tokens solo-Alpha, mmr 5 %, funding liquidado). Política base = prod
(`d0.5 now`): SL al F del camino a liquidación + límite Kyber a sl·(1−δ); caso A → swap
al minuto siguiente; caso B → short suelto (TP −20 % / 60 min).

Soft exit: cuando el mark (máximo del minuto) cruza s del camino a liquidación, cierra la
fracción q de lo que queda en N tramos pareados (impacto ≤ 0.15 %/tramo, modelo por token de
tranche_study: fee del pool + pendiente × USD, empuje que decae ρ = 0.8/min), separados
`gap` min. Cada tramo: venta spot a spot_c·(1−costo) y recompra perp a perp_c·(1+slip).
  - `b`: el tramo solo sale si el basis ejecutable (spot·(1−costo)/perp − 1) ≥ b %, salvo
    que el perp llegue a F − 0.05 (sale igual).
  - `h` (histéresis): si el perp vuelve bajo s − h, se detiene; el remanente sigue con sus
    seguros y el gatillo se re-arma. Sin `h` el episodio termina siempre.
  - El SL duro y la límite Kyber siguen vivos sobre el remanente (keep_perp_sl).
Justicia: el cierre normal al final del hold paga el mismo modelo de impacto (troceado) y
el mismo slippage del perp; el funding se corta al salir (capital ocioso: sesgo en contra
del soft exit).

Uso:  python -u scripts/sl_exit_study.py [--quick] [--notional 2100] [--workers 8]
      python -u scripts/sl_exit_study.py --check   (base vs margin_topup_events.pkl)
Salida: reports/sl_exit_study_YYYYMMDD_bsc.md, cache/sl_exit_rows.pkl
"""
import argparse
import math
import sys
from concurrent.futures import ProcessPoolExecutor
from dataclasses import dataclass
from datetime import datetime
from itertools import product
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
import margin_topup_study as mt  # noqa: E402
import protection_study as ps  # noqa: E402
from binance_dataset import OUT_CSV  # noqa: E402
from tranche_study import SLICE_IMPACT, SLICE_MAX, cost_table  # noqa: E402

MMR, LO_EDGE, SLIP, RT_FEES = mt.MMR, mt.LO_EDGE, mt.SLIP, mt.RT_FEES
RUNNER_T, RUNNER_TP = mt.RUNNER_T, mt.RUNNER_TP
RHO = 0.8
TK_LO, TK_HI = 0.005, 0.015      # swap del spot desnudo (caso A); 1.5 % = AIN #51
LEVS = (1.0, 1.5, 2.0)
HOLD_DAYS = 14
K = 5
PROD = "base F0.8 d0.5"


@dataclass(frozen=True)
class Pol:
    name: str
    s: float = None      # gatillo: fracción del camino a liquidación (None = sin soft exit)
    q: float = 1.0       # fracción de lo que queda que cierra cada episodio
    gap: int = 1         # min entre tramos
    b: float = None      # basis ejecutable mínimo, % (None = sin espera)
    h: float = None      # histéresis (None = termina siempre)
    F: float = 0.80      # SL duro
    delta: float = 0.005


def policies(quick=False):
    out = [Pol(f"base F{F:g} d{d * 100:g}", F=F, delta=d)
           for F in ((0.8,) if quick else (0.8, 0.9)) for d in (0.005, 0.0025, 0.0)]
    grid = product((0.5, 0.6), (1.0,), (1,), (None, -0.15), (None, 0.1), (0.8,)) if quick else \
        product((0.4, 0.5, 0.6, 0.7), (0.5, 1.0), (1, 5), (None, -0.15, -0.5), (None, 0.1, 0.2), (0.8, 0.9))
    for s, q, gap, b, h, F in grid:
        name = (f"soft s{s:g} q{q:g} g{gap} b{'–' if b is None else f'{b:g}'} "
                f"{'fin' if h is None else f'h{h:g}'} F{F:g}")
        out.append(Pol(name, s=s, q=q, gap=gap, b=b, h=h, F=F))
    return out


def _first(mask):
    if not len(mask):
        return -1
    i = int(np.argmax(mask))
    return i if mask[i] else -1


def _n(b, usd):
    imp = b * usd
    return max(1, min(SLICE_MAX, math.ceil(imp / SLICE_IMPACT))) if imp > 0 else 1


def prot_exit(d, t, end, pe, se, sl_px, lim_px):
    """Seguros disparados en la vela t (idéntico a margin_topup_study.position, salida `now`).
    Devuelve (ev_lo, ev_hi, j, caso) por unidad de posición."""
    mark, sh, sc = d["mark_h"], d["spot_h"], d["spot_c"]
    p_hit = mark[t] >= sl_px
    l_hit = sh[t] >= lim_px * (1 + LO_EDGE)
    if l_hit and not p_hit:
        lim = min(end, t + 1 + RUNNER_T)
        w_sl = _first(mark[t + 1:lim] >= sl_px)
        w_tp = _first(d["perp_l"][t + 1:lim] <= lim_px * (1 - RUNNER_TP))
        hits = [(w, kd) for w, kd in ((w_sl, "sl"), (w_tp, "tp")) if w >= 0]
        if hits:
            w, kd = min(hits)
            j = t + 1 + w
            buy = max(sl_px, d["perp_o"][j]) if kd == "sl" else min(lim_px * (1 - RUNNER_TP), d["perp_o"][j])
        else:
            j = lim - 1
            buy = d["perp_c"][j]
        ev = lim_px / se - buy * (1 + SLIP) / pe
        return ev, ev, j, "B"
    buy = max(sl_px, d["perp_o"][t]) * (1 + SLIP)
    if l_hit:
        ev = lim_px / se - buy / pe
        return ev, ev, t, "AB"
    j = min(t + 1, end - 1)
    if sh[j] >= lim_px * (1 + LO_EDGE):
        ev = lim_px / se - buy / pe
        return ev, ev, j, "A"
    return sc[j] * (1 - TK_LO) / se - buy / pe, sc[j] * (1 - TK_HI) / se - buy / pe, j, "A"


def sim(d, i, end, lev, pol, ab, usd, end_cost=True):
    """Una posición. Devuelve (pnl_lo, pnl_hi, caso, frac_soft, episodios) en fracción del nocional."""
    a, b = ab
    mark, sh, sc, pc = d["mark_h"], d["spot_h"], d["spot_c"], d["perp_c"]
    pe, se = pc[i], sc[i]
    lr = (1 + 1 / lev) / (1 + MMR)

    def lv(f):
        return pe * (1 + f * (lr - 1))

    sl_px = lv(pol.F)
    lim_px = sl_px * (1 - pol.delta)
    lim_trig = lim_px * (1 + LO_EDGE)
    soft = pol.s is not None
    soft_px = lv(pol.s) if soft else np.inf
    rearm_px = lv(pol.s - pol.h) if soft and pol.h else None
    force_px = lv(pol.F - 0.05)
    rem, lo, hi, frac_soft, eps, case = 1.0, 0.0, 0.0, 0.0, 0, "none"
    arm_from = i + 1 if soft else end
    k = i + 1

    def piece(frac, j, sell, buy):
        nonlocal lo, hi
        v = frac * (sell / se - buy / pe + mt.funding(d, i, j) - RT_FEES)
        lo += v
        hi += v

    while rem > 1e-9:
        if k >= end:
            k = end - 1
        t_p = _first(mark[k:end] >= sl_px)
        t_l = _first(sh[k:end] >= lim_trig)
        prot = min([t for t in (t_p, t_l) if t >= 0], default=-1)
        t_s = -1
        if arm_from < end:
            a0 = max(k, arm_from)
            w = _first(mark[a0:end] >= soft_px)
            t_s = a0 - k + w if w >= 0 else -1
        if t_s >= 0 and (prot < 0 or t_s < prot):
            ts = k + t_s
            eps += 1
            ep = rem * pol.q
            n = _n(b, ep * usd)
            u = ep * usd / n
            c = a + b * u
            m, done, push, prev, stop = ts + 1, 0, 0.0, ts, None
            while done < n and m < end:
                seg = slice(m, end)
                w_p = _first((mark[seg] >= sl_px) | (sh[seg] >= lim_trig))
                if pol.b is None:
                    w_ok = 0
                else:
                    with np.errstate(invalid="ignore", divide="ignore"):
                        ex = (sc[seg] * (1 - c / 100) / pc[seg] - 1) * 100
                    w_ok = _first((ex >= pol.b) | (pc[seg] >= force_px))
                w_r = _first(pc[seg] < rearm_px) if rearm_px else -1
                opts = [(w, r) for w, r in ((w_p, 0), (w_r, 1), (w_ok, 2)) if w >= 0]
                if not opts:
                    stop = "end"
                    break
                w, kind = min(opts)
                if kind == 0:
                    stop, k = "prot", m + w
                    break
                if kind == 1:
                    stop, k = "rearm", m + w + 1
                    arm_from = k
                    break
                j = m + w + (pol.b is not None)   # basis visto en j → llena en la vela siguiente
                if j >= end:
                    stop = "end"
                    break
                p = push * RHO ** max(j - prev, 1)
                piece(ep / n, j, sc[j] * (1 - (c + p) / 100), pc[j] * (1 + SLIP))
                rem -= ep / n
                frac_soft += ep / n
                done += 1
                push, prev, m = p + 2 * b * u, j, j + pol.gap
            if stop in ("prot", "rearm"):
                continue
            if stop == "end" or m >= end:
                break                       # el remanente se cierra al final del hold
            k = m
            if rearm_px:
                w = _first(pc[k:end] < rearm_px)
                arm_from = k + w + 1 if w >= 0 else end
            else:
                arm_from = end
            continue
        if prot >= 0:
            ev_lo, ev_hi, j, case = prot_exit(d, k + prot, end, pe, se, sl_px, lim_px)
            f = mt.funding(d, i, j) - RT_FEES
            lo += rem * (ev_lo + f)
            hi += rem * (ev_hi + f)
            rem = 0.0
        break
    if rem > 1e-9:
        j = end - 1
        if end_cost:
            usd_r = rem * usd
            ec = a + b * usd_r / _n(b, usd_r)
            piece(rem, j, sc[j] * (1 - ec / 100), pc[j] * (1 + SLIP))
        else:
            piece(rem, j, sc[j], pc[j])
    return lo, hi, case, frac_soft, eps


# ── trabajo por símbolo ───────────────────────────────────────────────────────
def _load_one(sym, f_ts, f_rate):
    df = ps.load_symbol(sym, mt.MONTHS, "bsc")
    if df is None:
        return None
    d = {c: df[c].to_numpy() for c in df.columns} | {"ts": df.index.to_numpy(dtype=np.int64)}
    d["f_ts"], d["f_rate"] = f_ts, f_rate
    return d


def run_symbol(job):
    sym, f_ts, f_rate, ab, usd, pols, k_hold, check = job
    d = _load_one(sym, f_ts, f_rate)
    if d is None:
        return [], []
    ent = mt.entries(d, k_hold)
    tf = [(sym, day, mt.trailing_funding(d, day)) for day in ent]
    rows = []
    for lev in ((2.0,) if check else LEVS):
        for day, i in ent.items():
            for pi, pol in enumerate(pols):
                lo, hi, case, fs, eps = sim(d, i, i + k_hold, lev, pol, ab, usd, end_cost=not check)
                rows.append((sym, day, lev, pi, lo, hi, case, fs, eps))
    return rows, tf


# ── reporte ───────────────────────────────────────────────────────────────────
def _quarter(day):
    return str(pd.Timestamp(day * 86_400_000, unit="ms").to_period("Q"))


def per_entry(df):
    base = df[df.pol == PROD].set_index(["lev", "sym", "day"])
    out = []
    for (lev, pol), g in df.groupby(["lev", "pol"], sort=False):
        g = g.set_index(["lev", "sym", "day"])
        bg = base.loc[g.index]
        soft = g.fs > 0
        qs = g.assign(dq=(g.lo - bg.lo).values).groupby("q").dq.mean()
        out.append({
            "lev": lev, "pol": pol, "n": len(g),
            "media": g.lo.mean() * 100, "p5": g.lo.quantile(0.05) * 100, "peor": g.lo.min() * 100,
            "media_hi": g.hi.mean() * 100, "p5_hi": g.hi.quantile(0.05) * 100,
            "soft_pct": soft.mean() * 100,
            "falsa_pct": (soft & (bg.case.values == "none")).mean() * 100,
            "A": int((g.case == "A").sum()), "A_base": int((bg.case == "A").sum()),
            "dmedia": (g.lo.mean() - bg.lo.mean()) * 100,
            "dp5": (g.lo.quantile(0.05) - bg.lo.quantile(0.05)) * 100,
            "dmedia_hi": (g.hi.mean() - bg.hi.mean()) * 100,
            "q_win": int((qs > 0).sum()), "q_n": len(qs),
        })
    return pd.DataFrame(out)


def cohorts(df, tf, k=K):
    tf = tf[tf.tf > 0].sort_values(["day", "tf"], ascending=[True, False])
    picks = tf.groupby("day").head(k)
    full = picks.groupby("day").sym.count()
    picks = picks[picks.day.isin(full[full == k].index)][["sym", "day"]]
    m = df.merge(picks, on=["sym", "day"])
    g = m.groupby(["lev", "pol", "day"]).agg(lo=("lo", "sum"), hi=("hi", "sum")).reset_index()
    g["cap"] = k * (1 + 1 / g.lev)
    g["ret"], g["ret_hi"] = g.lo / g.cap, g.hi / g.cap
    g["q"] = g.day.map(_quarter)
    out = []
    for (lev, pol), x in g.groupby(["lev", "pol"], sort=False):
        b = g[(g.lev == lev) & (g.pol == PROD)].set_index("day")
        x = x.set_index("day")
        dq = (x.ret - b.ret.loc[x.index]).groupby(x.q).mean()
        out.append({"lev": lev, "pol": pol, "roc": x.ret.mean() * 365 / HOLD_DAYS * 100,
                    "roc_hi": x.ret_hi.mean() * 365 / HOLD_DAYS * 100,
                    "p5": x.ret.quantile(0.05) * 100, "q_win": int((dq > 0).sum()), "q_n": len(dq),
                    "cohortes": len(x)})
    return pd.DataFrame(out)


def walk_forward(df):
    """Por trimestre Q: elige la mejor política soft por media en los trimestres previos
    (1x) y mide su Δ media vs prod en Q."""
    rows = []
    qs = sorted(df.q.unique())
    base = df[df.pol == PROD].set_index(["lev", "sym", "day"])
    for lev in LEVS:
        x = df[df.lev == lev]
        for n, q in enumerate(qs[1:], 1):
            prev = x[x.q.isin(qs[:n]) & x.pol.str.startswith("soft")].groupby("pol").lo.mean()
            if prev.empty:
                continue
            best = prev.idxmax()
            cur = x[(x.q == q) & (x.pol == best)].set_index(["lev", "sym", "day"])
            bq = base.loc[cur.index]
            rows.append({"lev": lev, "q": q, "pol": best,
                         "dmedia": (cur.lo.mean() - bq.lo.mean()) * 100,
                         "dp5": (cur.lo.quantile(0.05) - bq.lo.quantile(0.05)) * 100})
    return pd.DataFrame(rows)


def adoptable(r):
    return r.dmedia > 0 and r.dp5 >= -1.0 and r.q_win == r.q_n


def _f(v, nd=2, sign=True):
    return f"{v:+.{nd}f}" if sign else f"{v:.{nd}f}"


def report(args, syms, pe, co, wf, ev) -> str:
    L = [f"# Salida pareada anticipada antes del SL — {datetime.now():%Y-%m-%d} — BSC", "",
         f"Símbolos ({len(syms)}): {', '.join(syms)}  ",
         f"Meses {mt.MONTHS[0]} → {mt.MONTHS[-1]}; una entrada por símbolo × día, hold {HOLD_DAYS} d, "
         f"nocional {args.notional:,.0f} USD; mmr {MMR:.0%}; límite Kyber llena si el on-chain la "
         f"supera en {LO_EDGE:.1%}; swap del spot desnudo {TK_LO:.1%} (col. `hi`: {TK_HI:.1%}). "
         f"Valores en % del nocional. Prod = `{PROD}` (SL al 80 % + límite a sl·(1−0.5 %)).", "",
         "Soft exit `s/q/g/b/h/F`: gatillo al s del camino a liquidación (mark), cierra q de lo que "
         "queda en tramos pareados cada g min; `b` = basis ejecutable mínimo (– = sin espera); "
         "`h` = histéresis (fin = termina siempre); F = SL duro. Criterio (usuario): Δmedia > 0, "
         "Δp5 ≥ −1 pp y gana en todos los trimestres.", ""]
    for lev in LEVS:
        x = pe[pe.lev == lev].copy()
        x["ok"] = x.apply(adoptable, axis=1)
        bases = x[x.pol.str.startswith("base")]
        top = x[~x.pol.str.startswith("base")].sort_values("dmedia", ascending=False).head(15)
        n_ok = int(x.ok.sum())
        L += [f"## Por entrada — {lev:g}x ({int(x.n.iloc[0])} entradas; {n_ok} políticas adoptables)", "",
              "| política | media | p5 | peor | media hi | % soft | % falsa alarma | A (base) | "
              "Δmedia | Δp5 | Δmedia hi | trimestres | adoptable |",
              "|---|---|---|---|---|---|---|---|---|---|---|---|---|"]
        for _, r in pd.concat([bases, top]).iterrows():
            L.append(f"| {r.pol} | {_f(r.media)} | {_f(r.p5)} | {_f(r.peor)} | {_f(r.media_hi)} | "
                     f"{r.soft_pct:.1f} | {r.falsa_pct:.1f} | {r.A} ({r.A_base}) | {_f(r.dmedia, 3)} | "
                     f"{_f(r.dp5)} | {_f(r.dmedia_hi, 3)} | {r.q_win}/{r.q_n} | {'**sí**' if r.ok else ''} |")
        L.append("")
    L += [f"## Cartera — K={K} tokens/día por funding de 3 d, hold {HOLD_DAYS} d, capital = spot + margen", "",
          "| lev | política | ROC anual | ROC hi | p5 cohorte | trimestres vs prod |", "|---|---|---|---|---|---|"]
    for lev in LEVS:
        x = co[co.lev == lev]
        sel = pd.concat([x[x.pol.str.startswith("base")],
                         x[~x.pol.str.startswith("base")].sort_values("roc", ascending=False).head(8)])
        for _, r in sel.iterrows():
            L.append(f"| {lev:g}x | {r.pol} | {_f(r.roc, 1)} % | {_f(r.roc_hi, 1)} % | {_f(r.p5)} | "
                     f"{r.q_win}/{r.q_n} |")
    L += ["", "## Desglose por caso de la base (mejor soft por Δmedia, % del nocional)", "",
          "| lev | política | caso base | n | media prod | media soft | Δ |", "|---|---|---|---|---|---|---|"]
    for r in ev:
        L.append(f"| {r['lev']:g}x | {r['pol']} | {r['case']} | {r['n']} | {_f(r['b'])} | {_f(r['s'])} | "
                 f"{_f(r['s'] - r['b'])} |")
    L += ["", "## Walk-forward (elige en trimestres previos, mide en el siguiente; Δ vs prod)", "",
          "| lev | trimestre | política elegida | Δmedia | Δp5 |", "|---|---|---|---|---|"]
    for _, r in wf.iterrows():
        L.append(f"| {r.lev:g}x | {r.q} | {r.pol} | {_f(r.dmedia, 3)} | {_f(r.dp5)} |")
    L += ["", "`% falsa alarma` = entradas donde el soft exit cerró y la base no tuvo ningún evento de "
              "seguro (el precio volvió). `A (base)` = casos de spot desnudo con la política vs prod.", ""]
    return "\n".join(L)


def breakdown(df, pe):
    out = []
    base = df[df.pol == PROD].set_index(["lev", "sym", "day"])
    for lev in LEVS:
        x = pe[(pe.lev == lev) & ~pe.pol.str.startswith("base")]
        if x.empty:
            continue
        best = x.sort_values("dmedia", ascending=False).iloc[0].pol
        s = df[(df.lev == lev) & (df.pol == best)].set_index(["lev", "sym", "day"])
        b = base.loc[s.index]
        for case in ("none", "A", "AB", "B"):
            m = (b.case == case).values
            if m.sum():
                out.append({"lev": lev, "pol": best, "case": case, "n": int(m.sum()),
                            "b": b.lo.values[m].mean() * 100, "s": s.lo.values[m].mean() * 100})
    return out


# ── main ──────────────────────────────────────────────────────────────────────
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--notional", type=float, default=2100)
    ap.add_argument("--workers", type=int, default=8)
    ap.add_argument("--quick", action="store_true")
    ap.add_argument("--check", action="store_true", help="reproduce margin_topup (2x, prod, sin costo final)")
    ap.add_argument("--tag", default="")
    args = ap.parse_args()
    k_hold = HOLD_DAYS * 1440
    syms = sorted({p.name.split("-")[0] for p in (ps.KL_DIR / "alpha").glob("*.npz")})
    fr = pd.read_csv(OUT_CSV, usecols=["symbol", "rate", "funding_ts"])
    fr = fr[fr.symbol.isin(syms)]
    fmap = {s: (g.funding_ts.to_numpy(dtype=np.int64), g.rate.to_numpy()) for s, g in fr.groupby("symbol")}
    syms = [s for s in syms if s in fmap]
    pols = [p for p in policies(args.quick) if not args.check or p.name == PROD]
    cost = cost_table()
    abf = {s: (float(cost(s)[0]), float(cost(s)[1])) for s in syms}
    jobs = [(s, *fmap[s], abf[s], args.notional, pols, k_hold, args.check) for s in syms]
    print(f"{len(syms)} símbolos, {len(pols)} políticas", flush=True)
    rows, tfs = [], []
    with ProcessPoolExecutor(max_workers=args.workers) as ex:
        for s, (r, tf) in zip(syms, ex.map(run_symbol, jobs)):
            rows += r
            tfs += tf
            print(f"  {s}: {len(r)} filas", flush=True)
    df = pd.DataFrame(rows, columns=["sym", "day", "lev", "pi", "lo", "hi", "case", "fs", "eps"])
    df["pol"] = df.pi.map(lambda i: pols[i].name)
    df["q"] = df.day.map(_quarter)
    if args.check:
        ref = pd.read_pickle(ROOT / "cache" / "margin_topup_events.pkl")
        ref = ref[(ref.lev == 2.0) & (ref.exit == "d0.5 now") & (ref.tk == 0.005)]
        m = df.merge(ref, on=["sym", "day"], suffixes=("", "_ref"))
        diff = (m.lo - m.pnl).abs()
        print(f"check: {len(m)} filas, |Δpnl| máx {diff.max():.2e}, media {diff.mean():.2e}, "
              f"casos iguales {(m.case == m.case_ref).mean():.1%}")
        return
    tf = pd.DataFrame(tfs, columns=["sym", "day", "tf"])
    pe = per_entry(df)
    co = cohorts(df, tf)
    wf = walk_forward(df)
    ev = breakdown(df, pe)
    tag = f"_{args.tag}" if args.tag else ""
    out = ROOT / "reports" / f"sl_exit_study_{datetime.now():%Y%m%d}{tag}_bsc.md"
    out.write_text(report(args, syms, pe, co, wf, ev), encoding="utf-8")
    df.drop(columns=["pi"]).to_pickle(ROOT / "cache" / f"sl_exit_rows{tag}.pkl")
    print(f"-> {out}")


if __name__ == "__main__":
    main()
