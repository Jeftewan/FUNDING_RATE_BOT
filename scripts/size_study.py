#!/usr/bin/env python3
"""¿Cuánto nocional poner por token? Reglas de tamaño contra tamaño fijo — LOCAL, read-only.

Usa el motor de exec_study con la metodología vigente (pausa 15 min, salida 24 h). Las reglas y
el criterio se fijan aquí antes de correr. Costo = foto única de alpha_cost.csv: por eso cada
regla debe aguantar costo ×1.5 y ρ = 1.

    python scripts/size_study.py [--capital 10500] [--min-daily 0.05]
"""
import argparse
import sys
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent))
import _exec_engine as E  # noqa: E402
import exec_study as X  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
POL = E.Pol("vigente", gap=15, win_x=1440)
SIZES = (400, 800, 1200, 1575, 2100, 2625, 3000, 4200)
Z = np.array(SIZES, float)
HOLD = 7 * 1440
HOLD_MS = HOLD * 60_000
SCENARIOS = (("base", 1.0, E.RHO), ("costo ×1.5", 1.5, E.RHO), ("costo ×2", 2.0, E.RHO),
             ("ρ 0.9", 1.0, 0.9), ("ρ 1", 1.0, 1.0))
GATED = ("base", "costo ×1.5")
CAPS = (0.45, 0.6, 0.9)
EV_HAIRCUTS = (0.5, 0.75)
TARGETS = (1575, 2100, 2625)      # 2,100 USD × size_mult 0.75 / 1 / 1.25
ALLOC_REFS = (2100, 3000)
P5_TOL = 1.0
SLOTS = (5, 8)


def simulate(min_daily, mult, rho):
    """Una fila por intento con el neto (% del nocional) a cada tamaño."""
    syms = E.cached_symbols()
    funding, cost = E.funding_table(syms), E.cost_table()
    rows, cols = [], [f"u{u}" for u in SIZES]
    for idx, (sym, months) in enumerate(sorted(syms.items())):
        if sym not in funding:
            continue
        data = E.load(sym, months)
        if data is None or len(data["ts"]) < 40000:
            continue
        a, b = cost(sym)
        S = E.Sym(sym, data, funding[sym], (a * mult, b * mult))
        with np.errstate(invalid="ignore"):
            if np.nanmean(np.abs(S.pc / S.sc - 1) * 100) > 3:
                continue
        for t in S.attempts(HOLD, 360, min_daily, tail=4 * POL.win_x + 240):
            k = np.searchsorted(S.f_ts, S.ts[t], "right")
            res = [E.lifecycle(S, t, u, POL, HOLD, rho=rho) for u in SIZES]
            if any(r is None for r in res):
                continue
            ts = int(S.ts[t])
            rows.append([idx, ts // E.QUARTER_MS, ts // E.WEEK_MS, ts, a, b, S.daily[k - 3:k].mean()]
                        + [r[0] for r in res])
    return pd.DataFrame(rows, columns=["s", "q", "w", "ts", "a", "b", "f"] + cols)


def fixed(df, usd):
    return np.full(len(df), SIZES.index(usd))


def impact_cap(df, cap):
    """Mayor tamaño cuyo impacto a tamaño completo (con la foto SIN estrés) no pasa de `cap`."""
    imp = df.a.to_numpy()[:, None] + df.b.to_numpy()[:, None] * Z
    return np.where(imp <= cap, np.arange(len(SIZES)), 0).max(1)


def expected_value(df, haircut):
    """Tamaño que maximiza USD esperados: funding de 7 d recortado menos costo de ida y vuelta."""
    a, b, f = (df[c].to_numpy()[:, None] for c in ("a", "b", "f"))
    n = np.clip(np.ceil((a + b * Z) / POL.imp), 1, POL.smax)
    return (Z * (f * 7 * haircut - 2 * (a + b * Z / n) - 2 * E.PERP_COST)).argmax(1)


def outcome(df, sel):
    """(USD por intento, % del nocional)."""
    pct = df[[f"u{u}" for u in SIZES]].to_numpy()[np.arange(len(df)), sel]
    return pct * Z[sel] / 100, pct


def compare(df, sel, ref_sel):
    usd, pct = outcome(df, sel)
    ref_usd, ref_pct = outcome(df, ref_sel)
    meta = df[["s", "q", "w"]].to_numpy()
    g = X.gate(usd[:, None], ref_usd[:, None], meta)
    g["dp5"] = float(np.percentile(pct, 5) - np.percentile(ref_pct, 5))
    g["ok"] = bool(g["lo"] > 0 and g["dp5"] >= -P5_TOL and g["total"] and g["wins"] >= 0.8 * g["total"])
    return g


def portfolio(df, sel, capital, slots):
    """Cartera en orden temporal, mayor funding primero. Sin capital para el tamaño pedido, baja al
    tamaño que quepa. Un intento sin disparo no ocupa capital."""
    pct = df[[f"u{u}" for u in SIZES]].to_numpy()
    order = df.assign(i=np.arange(len(df))).sort_values(["ts", "f"], ascending=[True, False])
    live, total, by_q, used = [], 0.0, {}, []
    for r in order.itertuples():
        live = [p for p in live if p[0] > r.ts]
        if len(live) >= slots or any(p[2] == r.s for p in live):
            continue
        free = capital - sum(p[1] for p in live)
        fit = np.where(Z[:sel[r.i] + 1] <= free)[0]
        if not len(fit):
            continue
        j = int(fit.max())
        value = pct[r.i, j]
        if value == 0.0:
            continue
        usd = value * Z[j] / 100
        total += usd
        by_q[r.q] = by_q.get(r.q, 0.0) + usd
        live.append((r.ts + HOLD_MS, Z[j], r.s))
        used.append(Z[j])
    return {"usd": total, "by_q": by_q, "trades": len(used), "avg": float(np.mean(used)) if used else 0.0}


def rules(df):
    out = {f"impacto ≤ {c:g} %": impact_cap(df, c) for c in CAPS}
    out.update({f"valor esperado ×{h:g}": expected_value(df, h) for h in EV_HAIRCUTS})
    return out


def capped(df, target, cap):
    return np.minimum(fixed(df, target), impact_cap(df, cap))


def cell(g):
    return f"{g['d']:+.2f} [{g['lo']:+.2f}, {g['hi']:+.2f}] · {g['dp5']:+.2f} · {g['wins']}/{g['total']}"


def main():
    sys.stdout.reconfigure(encoding="utf-8")
    ap = argparse.ArgumentParser()
    ap.add_argument("--capital", type=float, default=10500)
    ap.add_argument("--min-daily", type=float, default=0.05)
    args = ap.parse_args()
    frames = {}
    for tag, mult, rho in SCENARIOS:
        frames[tag] = simulate(args.min_daily, mult, rho)
        print(f"{tag}: {len(frames[tag])} intentos", flush=True)
    base = frames["base"]
    now = datetime.now(timezone.utc)
    L = [f"# Tamaño de posición por token — {now:%Y-%m-%d}", "",
         f"Motor de `exec_study`, pausa {POL.gap} min, salida {POL.win_x // 60} h, θ {POL.th_e}, funding ≥ {args.min_daily} %/día, "
         f"{len(base)} intentos. Costo: una sola foto de cotizaciones aplicada a 12 meses, por eso se exige costo ×1.5 y ρ = 1. "
         "Δ en USD por intento [IC 95 %] · Δp5 en pp del nocional · trimestres.", "",
         f"Criterio: en {' y '.join(GATED)}, IC sobre cero, Δp5 ≥ −{P5_TOL:g} pp, ≥ 80 % de trimestres; con ρ 1, Δ media ≥ 0; "
         "en cartera de 5 cupos, más USD que el fijo en esos mismos escenarios.", ""]

    L += ["## Tamaño fijo", "", "| USD | " + " | ".join(t for t, _, _ in SCENARIOS) + " |", "|---|" + "---|" * len(SCENARIOS)]
    for usd in SIZES:
        L.append(f"| {usd:,} | " + " | ".join(f"{outcome(frames[t], fixed(frames[t], usd))[0].mean():+.2f}"
                                               for t, _, _ in SCENARIOS) + " |")
    L += [""]

    def verdict(make, ref):
        """(pasa, celdas): `make(df)` y `ref(df)` devuelven el tamaño elegido por intento."""
        cells, ok = [], True
        for tag, _, _ in SCENARIOS:
            df = frames[tag]
            g = compare(df, make(df), ref(df))
            cells.append(cell(g))
            ok &= g["ok"] if tag in GATED else (g["d"] >= 0 if tag == "ρ 1" else True)
        for tag in GATED:
            df = frames[tag]
            ok &= portfolio(df, make(df), args.capital, 5)["usd"] > portfolio(df, ref(df), args.capital, 5)["usd"]
        return ok, cells

    head = "| Regla | Referencia | " + " | ".join(t for t, _, _ in SCENARIOS) + " | Pasa |"
    sep = "|---|---|" + "---|" * (len(SCENARIOS) + 1)
    L += ["## Regla de asignación (puede subir o bajar el tamaño)", "", head, sep]
    passed = {}
    for name in rules(base):
        for ref_usd in ALLOC_REFS:
            ok, cells = verdict(lambda df, n=name: rules(df)[n], lambda df, u=ref_usd: fixed(df, u))
            passed[(name, ref_usd)] = ok
            L.append(f"| {name} | fijo {ref_usd:,} | " + " | ".join(cells) + f" | {'**SÍ**' if ok else 'no'} |")
    L += ["", "## Tope sobre el capital que elige el usuario (solo baja)", "", head, sep]
    cap_ok = {}
    for cap in CAPS:
        for target in TARGETS:
            ok, cells = verdict(lambda df, t=target, c=cap: capped(df, t, c), lambda df, t=target: fixed(df, t))
            cap_ok[(cap, target)] = ok
            L.append(f"| impacto ≤ {cap:g} % | {target:,} | " + " | ".join(cells) + f" | {'**SÍ**' if ok else 'no'} |")

    L += ["", f"## Cartera ({args.capital:,.0f} USD de nocional)", "",
          "| Regla | Cupos | " + " | ".join(f"{t} USD" for t in GATED) + " | Operaciones | Nocional medio |", "|---|---|" + "---|" * (len(GATED) + 2)]
    book = {f"fijo {u:,}": (lambda df, u=u: fixed(df, u)) for u in ALLOC_REFS}
    book.update({n: (lambda df, n=n: rules(df)[n]) for n in rules(base)})
    book.update({f"2,100 con tope {c:g} %": (lambda df, c=c: capped(df, 2100, c)) for c in CAPS})
    for name, make in book.items():
        for slots in SLOTS:
            res = {t: portfolio(frames[t], make(frames[t]), args.capital, slots) for t in GATED}
            L.append(f"| {name} | {slots} | " + " | ".join(f"{res[t]['usd']:+,.0f}" for t in GATED)
                     + f" | {res['base']['trades']} | {res['base']['avg']:,.0f} |")

    winners = [c for c in CAPS if all(cap_ok[(c, t)] for t in TARGETS)]
    alloc = [n for n in rules(base) if all(passed[(n, u)] for u in ALLOC_REFS)]
    L += ["", f"**Topes que pasan en los tres tamaños objetivo:** {', '.join(f'{c:g} %' for c in winners) or 'ninguno'}.",
          f"**Reglas de asignación que pasan contra ambos fijos:** {', '.join(alloc) or 'ninguna'}.", ""]
    path = ROOT / "reports" / f"size_study_{now:%Y%m%d}.md"
    path.write_text("\n".join(L) + "\n", encoding="utf-8")
    print("Reporte:", path)
    print("Topes:", winners, "| asignación:", alloc)


if __name__ == "__main__":
    main()
