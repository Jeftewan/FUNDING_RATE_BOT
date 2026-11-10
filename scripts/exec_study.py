#!/usr/bin/env python3
"""Metodología de entrada, salida y seguros de menor costo (spot BSC/Alpha + short perp) — LOCAL.

Un solo motor (scripts/_exec_engine.py) para todo el ciclo de una posición. Tres experimentos:
  E1  entrada y salida normal: umbral de basis fijo o relativo al propio token, ventana y troceo.
  E2  combinación de las variantes de E1 que pasan en todos los tamaños.
  E3  seguros: F del SL, MARK vs LAST, venta límite Kyber (δ o sin ella), spot desnudo, caso B.

Criterio (fijado antes de correr), frente a la política vigente y en TODOS los tamaños:
  Δ media > 0 con IC 95 % por bloques símbolo × semana sobre cero, p5 ≥ p5 base − 0.25 pp y
  mejor media en ≥ 80 % de los trimestres con ≥ 30 intentos.

    python -u scripts/exec_study.py --fetch           # velas que falten (mes cerrado más reciente)
    python -u scripts/exec_study.py --refresh-cost    # cotizaciones Kyber por tamaño
    python -u scripts/exec_study.py --real            # costo modelado vs fills reales (cache/real_exec.pkl)
    python -u scripts/exec_study.py [--workers 6]     # E1 + E2 + E3 → reports/exec_study_YYYYMMDD.md
"""
import argparse
import pickle
import sys
from concurrent.futures import ProcessPoolExecutor, ThreadPoolExecutor
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
import _exec_engine as E  # noqa: E402
from _exec_engine import Pol  # noqa: E402

SIZES = (1600, 2100, 3000, 4200)
MONTHS = [str(pd.Period("2025-10", "M") + k) for k in range(13)]
PROD_SYMBOLS = ("AGT", "JCT", "IRYS", "TAC", "KOMA", "AIN", "ZEST", "FIGHT", "STAR", "CLO", "ARIA", "4")
ROWS = ROOT / "cache" / "exec_study_rows.pkl"
REAL = ROOT / "cache" / "real_exec.pkl"
BASE = Pol("vigente")
MIN_QUARTER = 30
P5_TOL = 0.25


# ── Políticas ───────────────────────────────────────────────────────────
def entry_exit_policies():
    out = [BASE, Pol("ahora", wait=False)]
    out += [replace(BASE, name=f"entrada θ {v:g}", th_e=v) for v in (0.0, 0.10, 0.40, 0.60)]
    out += [replace(BASE, name=f"entrada p{int(q * 100)} piso {f:g}", q_e=q, th_e=f)
            for q in (0.60, 0.75, 0.90) for f in (0.0, 0.25)]
    out += [replace(BASE, name=f"entrada ventana {w // 60} h", win_e=w) for w in (120, 1440)]
    out += [replace(BASE, name=f"salida θ {v:g}", th_x=v) for v in (-0.50, -0.30, 0.0, 0.15)]
    out += [replace(BASE, name=f"salida p{int(q * 100)} piso {f:g}", q_x=q, th_x=f)
            for q in (0.60, 0.75, 0.90) for f in (-0.50, -0.15)]
    out += [replace(BASE, name=f"salida ventana {w // 60} h", win_x=w) for w in (120, 1440)]
    out += [replace(BASE, name=f"tramo ≤ {v:g} %", imp=v) for v in (0.10, 0.25)]
    out += [replace(BASE, name=f"máx {v} tramos", smax=v) for v in (4, 8)]
    out += [replace(BASE, name=f"tramos cada {v} min", gap=v) for v in (5, 10, 15, 20, 30)]
    return out


def protection_policies(base):
    out = [base]
    out += [replace(base, name=f"F {v:g}", F=v) for v in (0.70, 0.75, 0.85, 0.90)]
    out += [replace(base, name="SL sobre LAST", trig="last")]
    out += [replace(base, name=f"δ {v * 100:g} %", delta=v) for v in (0.0, 0.0025, 0.01, 0.02)]
    out += [replace(base, name="sin límite Kyber", delta=None)]
    out += [replace(base, name="caso B: recomprar ya", b_mode="now")]
    out += [replace(base, name=f"desnudo {n} tramo(s) cada {g} min", a_slices=n, a_gap=g)
            for n, g in ((1, 0), (4, 1), (4, 5), (4, 10), (4, 15), (6, 1))]
    out += [replace(base, name="sin límite + desnudo 4 cada 1 min", delta=None, a_gap=1)]
    return out


FIELDS = {"entrada": ("th_e", "q_e", "win_e"), "salida": ("th_x", "q_x", "win_x"), "tramo": ("imp", "smax", "gap")}


def combine(passed):
    """Producto de las variantes que pasaron, una por grupo (entrada / salida / troceo)."""
    groups = {g: [BASE] for g in FIELDS}
    for p in passed:
        for g, fields in FIELDS.items():
            if any(getattr(p, f) != getattr(BASE, f) for f in fields):
                groups[g].append(p)
    out = []
    for e in groups["entrada"]:
        for x in groups["salida"]:
            for s in groups["tramo"]:
                kw = {f: getattr(src, f) for g, src in (("entrada", e), ("salida", x), ("tramo", s)) for f in FIELDS[g]}
                names = [p.name for p in (e, x, s) if p is not BASE]
                if len(names) >= 2:
                    out.append(replace(BASE, name=" + ".join(names), **kw))
    return out


# ── Corrida ─────────────────────────────────────────────────────────────
def work(job):
    sym, months, fund, cost, pols, sizes, hold, step, min_daily, kw = job
    d = E.load(sym, months)
    if d is None or len(d["ts"]) < 40000:
        return sym, None, None
    S = E.Sym(sym, d, fund, cost)
    with np.errstate(invalid="ignore"):
        if np.nanmean(np.abs(S.pc / S.sc - 1) * 100) > 3:      # el perp no sigue a este token
            return sym, None, None
    ts = list(S.attempts(hold, step, min_daily, tail=4 * max(p.win_x for p in pols) + 240))
    out = np.full((len(pols), len(sizes), len(ts), 6), np.nan, np.float32)
    for si, usd in enumerate(sizes):
        S._cache = {k: v for k, v in S._cache.items() if k[0] == "q"}
        for pi, pol in enumerate(pols):
            for ti, t in enumerate(ts):
                r = E.lifecycle(S, t, usd, pol, hold, **kw)
                if r is not None:
                    out[pi, si, ti] = r
    meta = np.array([(int(S.ts[t]) // E.QUARTER_MS, int(S.ts[t]) // E.WEEK_MS) for t in ts], np.int64).reshape(-1, 2)
    return sym, out, meta


def run(pols, sizes, hold, step, min_daily, workers, **kw):
    syms = E.cached_symbols()
    funding, cost = E.funding_table(syms), E.cost_table()
    jobs = [(s, m, funding[s], cost(s), pols, sizes, hold, step, min_daily, kw) for s, m in sorted(syms.items()) if s in funding]
    data, meta, used = [], [], []
    with ProcessPoolExecutor(workers) as ex:
        for sym, out, m in ex.map(work, jobs):
            if out is None or not len(m):
                continue
            data.append(out)
            meta.append(np.c_[np.full(len(m), len(used)), m])
            used.append(sym)
            print(f"  {sym}: {len(m)} intentos", flush=True)
    return np.concatenate(data, axis=2), np.concatenate(meta), used


# ── Evaluación ──────────────────────────────────────────────────────────
def stats(a):
    a = a[~np.isnan(a[:, 0])]
    fired = a[:, 5] > 0
    fund, cost = a[fired, 3].mean(), -(a[fired, 1] + a[fired, 2]).mean()
    return {"n": len(a), "fire": fired.mean() * 100, "mean": a[:, 0].mean(), "med": np.median(a[fired, 0]),
            "p5": np.percentile(a[:, 0], 5), "entry": a[fired, 1].mean(), "exit": a[fired, 2].mean(),
            "fund": fund, "leak": cost / fund * 100 if fund else np.nan, "frac": a[fired, 4].mean() * 100,
            "ev": (a[:, 5] > 1).mean() * 100}


def gate(cand, base, meta, seed=7):
    ok = ~np.isnan(cand[:, 0]) & ~np.isnan(base[:, 0])
    wins = total = 0
    for q in sorted(set(meta[:, 1])):
        m = ok & (meta[:, 1] == q)
        if m.sum() >= MIN_QUARTER:
            total += 1
            wins += cand[m, 0].mean() > base[m, 0].mean()
    diff = cand[ok, 0] - base[ok, 0]
    blocks = pd.DataFrame({"b": meta[ok, 0] * 100000 + meta[ok, 2], "d": diff}).groupby("b").d.agg(["sum", "size"])
    s, n = blocks["sum"].to_numpy(), blocks["size"].to_numpy(float)
    rng = np.random.default_rng(seed)
    pick = rng.integers(0, len(s), (1000, len(s)))
    boots = s[pick].sum(1) / n[pick].sum(1)
    dp5 = np.percentile(cand[ok, 0], 5) - np.percentile(base[ok, 0], 5)
    lo = float(np.percentile(boots, 2.5))
    return {"d": float(diff.mean()), "lo": lo, "hi": float(np.percentile(boots, 97.5)), "dp5": float(dp5),
            "wins": wins, "total": total,
            "ok": bool(lo > 0 and dp5 >= -P5_TOL and total and wins >= 0.8 * total)}


def evaluate(data, meta, pols, sizes):
    """{nombre: [gate por tamaño]} frente a pols[0]."""
    return {p.name: [gate(data[pi, si], data[0, si], meta) for si in range(len(sizes))]
            for pi, p in enumerate(pols) if pi}


def table(data, pols, sizes, si):
    L = ["| Política | n | Dispara | Media | Mediana disp. | p5 | Entrada | Salida | Funding | Fuga | Eventos |",
         "|---|---|---|---|---|---|---|---|---|---|---|"]
    rows = sorted(((p.name, stats(data[pi, si])) for pi, p in enumerate(pols)), key=lambda r: -r[1]["mean"])
    for name, s in rows:
        L.append(f"| {name} | {s['n']} | {s['fire']:.0f} % | {s['mean']:+.3f} | {s['med']:+.2f} | {s['p5']:+.2f} | "
                 f"{s['entry']:+.2f} | {s['exit']:+.2f} | {s['fund']:+.2f} | {s['leak']:.0f} % | {s['ev']:.2f} % |")
    return L


def gate_table(gates, sizes):
    L = ["| Política | " + " | ".join(f"{s:,} USD: Δ media [IC] · Δp5 · trim." for s in sizes) + " | Pasa |",
         "|---|" + "---|" * (len(sizes) + 1)]
    for name, gs in sorted(gates.items(), key=lambda kv: -np.mean([g["d"] for g in kv[1]])):
        cells = [f"{g['d']:+.3f} [{g['lo']:+.3f}, {g['hi']:+.3f}] · {g['dp5']:+.2f} · {g['wins']}/{g['total']}" for g in gs]
        L.append(f"| {name} | " + " | ".join(cells) + f" | {'**SÍ**' if all(g['ok'] for g in gs) else 'no'} |")
    return L


def passing(gates):
    return [n for n, gs in gates.items() if all(g["ok"] for g in gs)]


TIE = 0.02


def best(gates, names, pols):
    """Mayor Δ media; entre las que quedan a < TIE pp de ella, la de pausas más cortas: el tiempo
    entre tramos va sin seguros y el motor no modela una liquidación en ese lapso."""
    if not names:
        return None
    mean = {n: np.mean([g["d"] for g in gates[n]]) for n in names}
    top = max(mean.values())
    near = [n for n in names if mean[n] >= top - TIE]
    return min(near, key=lambda n: (pols[n].gap, pols[n].a_gap, pols[n].win_x, -mean[n]))


# ── Fills reales ────────────────────────────────────────────────────────
def real_report():
    """Costo del spot por swap real (vs mid de referencia) contra a + b·USD del modelo."""
    raw = pickle.load(open(REAL, "rb"))
    cost = E.cost_table()
    pos = {p["id"]: p for p in raw["positions"]}
    L = ["| Job | Token | Lado | Modo | USD | Tramos | Basis real % | Spot real % | Spot modelo % | Perp real % |", "|---|---|---|---|---|---|---|---|---|---|"]
    diffs = []
    for j in raw["jobs"]:
        r, p = j["result_json"], pos.get(j["position_id"])
        if j["status"] != "done" or not p or p.get("spot_venue") != "bsc":
            continue
        opening = j["kind"] == "open"
        ex = r if opening else (r.get("exec") or {})
        slip = r.get("entry_slippage") if opening else (r.get("exit_slippage") or ex.get("exit_slippage"))
        mids = r.get("ref_mids") if opening else ex.get("exit_ref_mids")
        if not slip or not mids or not mids.get("spot_mid") or slip.get("spot") is None:
            continue
        qty = float(r.get("spot_qty") or ex.get("spot_qty_out") or 0)
        usd = qty * mids["spot_mid"]
        if usd < 100:
            continue
        n = max(1, len(r.get("swaps") or []))
        a, b = cost(p["symbol"])
        model = a + b * usd / n
        real = slip["spot"] / usd * 100
        basis = r.get("basis_pct") if opening else ex.get("exit_basis_pct")
        mode = ((j["params_json"].get("wait") or {}).get("mode") or "ahora") + ("/seguro" if j["params_json"].get("trigger") else "")
        diffs.append(real - model)
        L.append(f"| {j['id']} | {p['symbol']} | {'compra' if opening else 'venta'} | {mode} | {usd:,.0f} | {n} | "
                 f"{basis if basis is not None else float('nan'):+.2f} | {real:+.2f} | {model:.2f} | {(slip.get('perp') or 0) / usd * 100:+.2f} |")
    d = np.array(diffs)
    L += ["", f"Spot real − modelo: media {d.mean():+.2f} pp, mediana {np.median(d):+.2f} pp, |error| mediano "
          f"{np.median(np.abs(d)):.2f} pp (n = {len(d)}). El modelo usa la foto de costos de hoy y no incluye sándwich."]
    return L


# ── main ────────────────────────────────────────────────────────────────
def fetch():
    ids = E.alpha_tokens()
    syms = sorted(set(E.cached_symbols()) | {s for s in PROD_SYMBOLS if s in ids})
    todo = [(s, k, m) for s in syms for m in MONTHS for k in ("alpha", "perp", "mark")
            if not (E.KL_DIR / k / f"{s}-{m}.npz").exists()]
    print(f"{len(syms)} símbolos, {len(todo)} archivos por bajar", flush=True)
    with ThreadPoolExecutor(6) as ex:
        list(ex.map(lambda j: E.month_bars(*j, ids=ids), todo))
    print({s: len(m) for s, m in E.cached_symbols().items()})


def main():
    sys.stdout.reconfigure(encoding="utf-8")
    ap = argparse.ArgumentParser()
    ap.add_argument("--fetch", action="store_true")
    ap.add_argument("--refresh-cost", action="store_true")
    ap.add_argument("--real", action="store_true")
    ap.add_argument("--check", action="store_true", help="solo vigente y ahora a 2,100 USD, sin seguros (tranche_study)")
    ap.add_argument("--workers", type=int, default=6)
    ap.add_argument("--min-daily", type=float, default=0.05)
    args = ap.parse_args()
    if args.fetch:
        return fetch()
    if args.refresh_cost:
        return E.refresh_cost(set(E.cached_symbols()))
    if args.real:
        return print("\n".join(real_report()))
    hold, hold_p = 7 * 1440, 14 * 1440
    if args.check:
        pols = [BASE, Pol("ahora", wait=False)]
        data, meta, _ = run(pols, (2100,), hold, 360, args.min_daily, args.workers, seguros=False)
        return print("\n".join(table(data, pols, (2100,), 0)))

    print("E1 entrada/salida", flush=True)
    p1 = entry_exit_policies()
    d1, m1, syms = run(p1, SIZES, hold, 360, args.min_daily, args.workers)
    g1 = evaluate(d1, m1, p1, SIZES)
    ok1 = [p for p in p1 if p.name in passing(g1)]
    p2 = [BASE] + combine(ok1)
    g2, d2, m2 = {}, None, None
    if len(p2) > 1:
        print(f"E2 combinaciones ({len(p2) - 1})", flush=True)
        d2, m2, _ = run(p2, SIZES, hold, 360, args.min_daily, args.workers)
        g2 = evaluate(d2, m2, p2, SIZES)
    names = {p.name: p for p in p1 + p2}
    allg = {**g1, **g2}
    win = best(allg, passing(allg), names)
    chosen = replace(names[win], name="vigente") if win else BASE
    sens = {}
    if win:
        for rho in (0.9, 1.0):
            d, m, _ = run([BASE, names[win]], SIZES, hold, 360, args.min_daily, args.workers, rho=rho)
            sens[rho] = evaluate(d, m, [BASE, names[win]], SIZES)[win]
    print("E3 seguros", flush=True)
    p3 = protection_policies(chosen)
    runs3 = {}
    for lev in (1.0, 2.0):
        for tag, kw in (("", {}), (" · estrés", {"sl_slip": 0.015, "naked_extra": 0.01})):
            runs3[f"{lev:g}x{tag}"] = run(p3, (2100, 4200), hold_p, 1440, args.min_daily, args.workers, lev=lev, **kw)
    g3 = {k: evaluate(d, m, p3, (2100, 4200)) for k, (d, m, _) in runs3.items()}
    pickle.dump({"p1": p1, "d1": d1, "m1": m1, "p2": p2, "d2": d2, "m2": m2, "p3": p3, "runs3": runs3, "syms": syms}, open(ROWS, "wb"))

    now = datetime.now(timezone.utc)
    L = [f"# Metodología de ejecución y seguros on-chain — {now:%Y-%m-%d}", "",
         f"Tokens ({len(syms)}): {', '.join(syms)}. Meses {MONTHS[0]} → {MONTHS[-1]}. Funding ≥ {args.min_daily} %/día al decidir. "
         f"mmr {E.MMR:.0%}, perp taker {E.PERP_COST} %/lado, ρ {E.RHO}. Todo en % del nocional objetivo; "
         "un intento sin disparo vale 0. Fuga = costo de ejecución ÷ funding. Eventos = intentos que terminan por un seguro.", "",
         "## Criterio", "",
         f"Frente a la vigente y en todos los tamaños: Δ media con IC 95 % (bloques símbolo × semana) sobre cero, "
         f"Δp5 ≥ −{P5_TOL} pp y mejor media en ≥ 80 % de los trimestres con ≥ {MIN_QUARTER} intentos.", "",
         "## Costo modelado vs fills reales", ""] + real_report() + ["",
         "## E1. Entrada y salida normal (hold 7 d, un intento cada 6 h, 1x)", ""]
    for si, usd in enumerate(SIZES):
        L += [f"### {usd:,} USD", ""] + table(d1, p1, SIZES, si) + [""]
    L += ["### Criterio", ""] + gate_table(g1, SIZES) + [""]
    L += ["## E2. Combinaciones de lo que pasó en E1", ""]
    L += (gate_table(g2, SIZES) + [""] + table(d2, p2, SIZES, 1) + [""]) if g2 else ["Menos de dos variantes pasaron: no hay combinaciones.", ""]
    L += [f"**Entrada/salida elegida:** {win or 'la vigente (ninguna variante pasa el criterio)'} "
          f"(mayor Δ media; a menos de {TIE} pp se prefiere la pausa más corta).", ""]
    if sens:
        L += ["Sensibilidad a la recuperación del pool (ρ = 1: el empuje propio no decae nunca):", "",
              "| ρ | " + " | ".join(f"{s:,} USD Δ media · Δp5 · trim." for s in SIZES) + " |",
              "|---|" + "---|" * len(SIZES)]
        L += [f"| {rho:g} | " + " | ".join(f"{g['d']:+.3f} · {g['dp5']:+.2f} · {g['wins']}/{g['total']}" for g in gs) + " |"
              for rho, gs in sens.items()]
        L += [""]
    L += [
          "## E3. Seguros (hold 14 d, una entrada por día)", "",
          "Estrés = SL llena 1.5 % sobre el trigger y el swap del spot desnudo paga 1 % extra (AIN #51).", ""]
    for key, (d, m, _) in runs3.items():
        L += [f"### {key}, 2,100 USD", ""] + table(d, p3, (2100, 4200), 0) + ["", f"Criterio {key} (2,100 y 4,200 USD):", ""]
        L += gate_table(g3[key], (2100, 4200)) + [""]
    by3 = {p.name: p for p in p3}
    ok3 = [n for n in g3["1x"] if all(all(g["ok"] for g in g3[k][n]) for k in ("1x", "1x · estrés"))]
    # Un SL más cerca de liquidación (F, trigger) no se adopta con velas de 1 min: el motor no ve una
    # liquidación dentro del minuto (cuesta ≈ 9.5 % del nocional). El resto se elige por dimensión.
    sl_moved = [n for n in ok3 if by3[n].F != chosen.F or by3[n].trig != chosen.trig]
    win3 = best(g3["1x"], [n for n in ok3 if n not in sl_moved], by3)
    L += [f"**Seguros que pasan a 1x (normal y estrés):** {', '.join(ok3) or 'ninguno; se mantienen los vigentes'}. "
          f"Elegido: {win3 or 'los vigentes'}."
          + (f" No se adoptan ({', '.join(sl_moved)}): mueven el SL hacia la liquidación y el motor de "
             "1 min no ve una liquidación dentro del minuto." if sl_moved else ""), ""]
    path = ROOT / "reports" / f"exec_study_{now:%Y%m%d}.md"
    path.write_text("\n".join(L) + "\n", encoding="utf-8")
    print("Reporte:", path)
    print("Elegida:", win, "| seguros:", win3)


if __name__ == "__main__":
    main()
