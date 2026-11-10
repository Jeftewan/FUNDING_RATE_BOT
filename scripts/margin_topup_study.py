#!/usr/bin/env python3
"""Seguros BSC: ¿recargar margen y/o vender el spot al precio del SL? — LOCAL, read-only.

Motivo: ARIA #56 (2026-11-10). Delta-neutral, +34 % en 3 días → el SL (80 % del camino a
liquidación, 2x) cerró el short; la venta límite del spot estaba a sl·(1−0.5 %) y el cierre
costó ≈ 0.9 % del nocional, todo el funding cobrado. Sin el SL la posición sobrevivía.

Dos palancas, medidas sobre las mismas velas 1m de protection_study (tokens solo-Alpha,
spot on-chain de la API de Binance Alpha, perp/mark de data.binance.vision):
  1. Salida (`exit`): venta límite del spot a sl·(1−δ), δ ∈ {0, 0.25, 0.5 %}. Si el SL
     dispara y la límite no llenó: `now` = swap al minuto siguiente (prod), o `wait x/T` =
     la límite sigue viva y el spot se vende por swap si el on-chain cae x % bajo el SL o
     pasan T minutos (propuesta del usuario).
  2. Margen (`topup`): reserva común R (fracción del nocional de la cohorte). Cuando el
     mark sube, se suma margen aislado para que el máximo visto quede a ≤ f_top del camino
     a la nueva liquidación (tramos de 2.5 % del nocional) y los seguros suben con él. Sin
     reserva suficiente la posición queda con su SL en ese nivel.

Cohortes: cada día UTC se abren K=5 posiciones (los tokens con más funding liquidado en
los 3 días previos), hold 14 d, mismo nocional. Capital = spot + margen + reserva. PnL =
funding liquidado (fr_binance.csv) + precio de ambas piernas (cada una contra su entrada)
− 0.30 % de fees ida y vuelta. Venta límite Kyber: se llena si el on-chain la supera en
LO_EDGE. Mismo minuto llenado y stop → stop; mismo minuto SL y límite → ambas (AB).

Uso:  python scripts/margin_topup_study.py [--k 5] [--hold-days 14]
Salida: reports/margin_topup_study_YYYYMMDD_bsc.md
"""
import argparse
import sys
from datetime import datetime
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
import protection_study as ps  # noqa: E402
from binance_dataset import OUT_CSV  # noqa: E402

MONTHS = [str(pd.Period("2025-10", "M") + k) for k in range(12)]
F = 0.80                 # SL al 80 % del camino a liquidación (PROTECTION_LIQ_FRACTION)
MMR = 0.05               # ARIA #56: liq real 0.22354 a 2x ⇒ mmr ≈ 5 % (protection_study usaba 1 %)
SLIP = 0.001             # stop market / recompra del perp
LO_EDGE = 0.005          # la límite Kyber se llena si el on-chain la supera en 0.5 %
RT_FEES = 0.003          # fees ida y vuelta (fee_drag del proyecto)
TAKERS = (0.005, 0.015)  # swap del spot desnudo; 1.5 % = AIN #51 (sándwich)
DELTAS = (0.0, 0.0025, 0.005)
WAIT_X = (0.01, 0.02, 0.03, 0.05)
WAIT_T = (15, 60, 240)
CHUNK = 0.025            # tramo de recarga, fracción del nocional
RUNNER_T, RUNNER_TP = 60, 0.20   # caso B en BSC (RUNNER_RULES): TP −20 % / 1 h
CUR = ("d0.5", "now")


def exit_policies():
    out = []
    for d in DELTAS:
        out.append((f"d{d * 100:g}", "now"))
        out += [(f"d{d * 100:g}", f"w{int(x * 100)}/{t}") for x in WAIT_X for t in WAIT_T]
    return out


def _first(mask):
    if not len(mask):
        return -1
    i = int(np.argmax(mask))
    return i if mask[i] else -1


def sl_level(m):
    """Precio del SL (ratio a la entrada del perp) con margen aislado m (fracción del nocional)."""
    liq = (1 + m) / (1 + MMR)
    return 1 + F * (liq - 1)


def need_margin(p_max, m0, f_top):
    liq_req = 1 + (p_max - 1) / f_top
    return np.maximum(0.0, liq_req * (1 + MMR) - 1 - m0)


# ── datos ───────────────────────────────────────────────────────────────────
def load(k_hold):
    syms = sorted({p.name.split("-")[0] for p in (ps.KL_DIR / "alpha").glob("*.npz")})
    data = {}
    for s in syms:
        df = ps.load_symbol(s, MONTHS, "bsc")
        if df is not None:
            data[s] = {c: df[c].to_numpy() for c in df.columns} | {"ts": df.index.to_numpy(dtype=np.int64)}
    fr = pd.read_csv(OUT_CSV, usecols=["symbol", "rate", "funding_ts"])
    fr = fr[fr.symbol.isin(list(data))]
    for s, g in fr.groupby("symbol"):
        data[s]["f_ts"] = g.funding_ts.to_numpy(dtype=np.int64)
        data[s]["f_rate"] = g.rate.to_numpy()
    data = {s: d for s, d in data.items() if "f_ts" in d}
    return data


def entries(d, k_hold):
    """(día UTC, índice) de la primera vela de cada día con hold completo."""
    ts = d["ts"]
    out = {}
    days = np.unique(ts // 86_400_000)
    for day in days:
        i = int(np.searchsorted(ts, day * 86_400_000))
        if i + k_hold <= len(ts) and ts[i] - day * 86_400_000 < 3_600_000 \
                and ts[i + k_hold - 1] - ts[i] < (k_hold + 720) * 60_000:
            out[int(day)] = i
    return out


def trailing_funding(d, day):
    t1 = day * 86_400_000
    m = (d["f_ts"] >= t1 - 3 * 86_400_000) & (d["f_ts"] < t1)
    return float(d["f_rate"][m].sum()) if m.any() else -1.0


def funding(d, i, j):
    """Funding cobrado por el short entre las velas i (excl.) y j (incl.), fracción del nocional."""
    ts = d["ts"]
    m = (d["f_ts"] > ts[i]) & (d["f_ts"] <= ts[j])
    if not m.any():
        return 0.0
    k = np.clip(np.searchsorted(ts, d["f_ts"][m]), i, j)
    return float((d["f_rate"][m] * d["perp_c"][k] / d["perp_c"][i]).sum())


# ── una posición ────────────────────────────────────────────────────────────
def position(d, i, k_hold, sl_arr, delta, exits):
    """Resultado por política de salida. sl_arr: precio del SL (ratio a perp_e) por minuto
    en [i, i+k_hold). Devuelve {exit: (pnl, event_pnl|None, case)}."""
    end = i + k_hold
    pe, se = d["perp_c"][i], d["spot_c"][i]
    mark = d["mark_h"][i + 1:end] / pe
    sl = sl_arr[1:]
    sp = sl * (1 - delta)
    tp = _first(mark >= sl)
    ts_ = _first(d["spot_h"][i + 1:end] / pe >= sp * (1 + LO_EDGE))
    tp = tp + i + 1 if tp >= 0 else -1
    ts_ = ts_ + i + 1 if ts_ >= 0 else -1
    res = {}
    if tp < 0 and ts_ < 0:
        j = end - 1
        pnl = d["spot_c"][j] / se - 1 + 1 - d["perp_c"][j] / pe + funding(d, i, j) - RT_FEES
        return {e: (pnl, None, "none") for e in exits}
    if ts_ >= 0 and (tp < 0 or ts_ < tp):
        # B: el spot se vendió a sp; short suelto con SL duro, TP −20 % o 1 h.
        k0 = ts_
        sl_k, sp_px = sl_arr[k0 - i] * pe, sp[k0 - i - 1] * pe
        lim = min(end, k0 + 1 + RUNNER_T)
        w_sl = _first(d["mark_h"][k0 + 1:lim] >= sl_k)
        w_tp = _first(d["perp_l"][k0 + 1:lim] <= sp_px * (1 - RUNNER_TP))
        hits = [(w, kd) for w, kd in ((w_sl, "sl"), (w_tp, "tp")) if w >= 0]
        if hits:
            w, kd = min(hits)
            j = k0 + 1 + w
            buy = (max(sl_k, d["perp_o"][j]) if kd == "sl" else min(sp_px * (1 - RUNNER_TP), d["perp_o"][j]))
        else:
            j = lim - 1
            buy = d["perp_c"][j]
        ev = sp_px / se - 1 + 1 - buy * (1 + SLIP) / pe
        pnl = ev + funding(d, i, j) - RT_FEES
        return {e: (pnl, ev, "B") for e in exits}
    # A o AB: el SL recompró el short en tp.
    sl_px = sl_arr[tp - i] * pe
    sp_px = sl_px * (1 - delta)
    buy = max(sl_px, d["perp_o"][tp]) * (1 + SLIP)
    fund = funding(d, i, tp)
    if ts_ == tp:
        ev = sp_px / se - buy / pe
        return {e: (ev + fund - RT_FEES, ev, "AB") for e in exits}
    for name, w in exits:
        for tk in TAKERS:
            if w == "now":
                j = min(tp + 1, end - 1)
                sell = sp_px if d["spot_h"][j] >= sp_px * (1 + LO_EDGE) else d["spot_c"][j] * (1 - tk)
            else:
                x, t = w[1:].split("/")
                stop = sl_px * (1 - int(x) / 100)
                lim = min(end, tp + 1 + int(t))
                w_fill = _first(d["spot_h"][tp + 1:lim] >= sp_px * (1 + LO_EDGE))
                w_stop = _first(d["spot_l"][tp + 1:lim] <= stop)
                if w_stop >= 0 and (w_fill < 0 or w_stop <= w_fill):
                    sell = min(stop, d["spot_c"][tp + 1 + w_stop]) * (1 - tk)
                elif w_fill >= 0:
                    sell = sp_px
                else:
                    sell = d["spot_c"][lim - 1] * (1 - tk)
            ev = sell / se - buy / pe
            res[(name, w, tk)] = (ev + fund - RT_FEES, ev, "A")
    return res


# ── cohortes ────────────────────────────────────────────────────────────────
def cohort(data, picks, k_hold, lev, delta, exits, reserve, f_top):
    """picks: [(sym, i)]. Asigna la reserva común por orden de llegada (tramos CHUNK)."""
    m0 = 1 / lev
    paths = []
    for s, i in picks:
        d = data[s]
        pmax = np.maximum.accumulate(d["mark_h"][i:i + k_hold] / d["perp_c"][i])
        need = np.ceil(need_margin(pmax, m0, f_top) / CHUNK - 1e-9) * CHUNK if reserve > 0 \
            else np.zeros(k_hold)
        paths.append(need)
    pool = reserve * len(picks)
    alloc = [0.0] * len(picks)
    frozen = [None] * len(picks)
    ev = []
    for n, need in enumerate(paths):
        steps = np.flatnonzero(np.diff(need, prepend=0.0) > 1e-12)
        ev += [(int(t), n, float(need[t])) for t in steps]
    for t, n, lvl in sorted(ev):
        if frozen[n] is not None:
            continue
        inc = lvl - alloc[n]
        if inc <= pool + 1e-12:
            pool -= inc
            alloc[n] = lvl
        else:
            frozen[n] = t
    out = []
    for n, (s, i) in enumerate(picks):
        need = paths[n]
        m = np.minimum(need, alloc[n]) if frozen[n] is not None else need
        if frozen[n] is not None:
            m = m.copy()
            m[frozen[n]:] = alloc[n]
        out.append(position(data[s], i, k_hold, sl_level(m0 + m), delta, exits))
    return out


def flat_exits(exits):
    """Política de salida con taker → clave del dict de position()."""
    out = []
    for name, w in exits:
        for tk in TAKERS:
            out.append((name, w, tk))
    return out


def pick(res, key):
    """res[key] o, si el caso no depende de la salida (none/B/AB), cualquier valor."""
    return res.get(key) or next(iter(res.values()))


# ── main ────────────────────────────────────────────────────────────────────
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--k", type=int, default=5)
    ap.add_argument("--hold-days", type=int, default=14)
    args = ap.parse_args()
    k_hold = args.hold_days * 1440
    data = load(k_hold)
    syms = sorted(data)
    print(f"{len(syms)} símbolos: {', '.join(syms)}", flush=True)
    ent = {s: entries(data[s], k_hold) for s in syms}
    days = sorted({day for e in ent.values() for day in e})
    exits = exit_policies()
    keys = flat_exits(exits)

    # 1) Por evento: todas las entradas (símbolo × día), sin recarga.
    ev_rows = []
    for lev in (2.0, 1.5):
        for delta in DELTAS:
            ex_d = [e for e in exits if e[0] == f"d{delta * 100:g}"]
            for s in syms:
                d = data[s]
                for day, i in ent[s].items():
                    sl_arr = np.full(k_hold, sl_level(1 / lev))
                    r = position(d, i, k_hold, sl_arr, delta, ex_d)
                    for name, w in ex_d:
                        for tk in TAKERS:
                            pnl, evp, case = pick(r, (name, w, tk))
                            ev_rows.append({"lev": lev, "exit": f"{name} {w}", "tk": tk, "sym": s, "day": day,
                                            "case": case, "ev": evp, "pnl": pnl})
        print(f"eventos lev {lev} listos", flush=True)
    evdf = pd.DataFrame(ev_rows)

    # 2) Cohortes: K tokens por día, hold, con y sin reserva.
    configs = [(lev, 0.0, 0.7) for lev in (1.0, 1.5, 2.0)] + \
              [(2.0, r, f) for r in (0.10, 0.20, 0.30) for f in (0.6, 0.7)]
    co_rows = []
    for day in days:
        cands = [(trailing_funding(data[s], day), s) for s in syms if day in ent[s]]
        cands = [c for c in sorted(cands, reverse=True) if c[0] > 0][:args.k]
        if len(cands) < args.k:
            continue
        picks = [(s, ent[s][day]) for _, s in cands]
        for lev, reserve, f_top in configs:
            for delta in DELTAS:
                ex_d = [e for e in exits if e[0] == f"d{delta * 100:g}"]
                res = cohort(data, picks, k_hold, lev, delta, ex_d, reserve, f_top)
                cap = len(picks) * (1 + 1 / lev + reserve)
                for name, w in ex_d:
                    for tk in TAKERS:
                        vals = [pick(r, (name, w, tk)) for r in res]
                        co_rows.append({"day": day, "lev": lev, "reserve": reserve, "f_top": f_top,
                                        "exit": f"{name} {w}", "tk": tk,
                                        "ret": sum(v[0] for v in vals) / cap,
                                        "events": sum(v[2] in ("A", "B", "AB") for v in vals),
                                        "n": len(vals)})
    codf = pd.DataFrame(co_rows)
    print(f"cohortes: {codf.day.nunique()}", flush=True)
    out = ROOT / "reports" / f"margin_topup_study_{datetime.now():%Y%m%d}_bsc.md"
    out.write_text(report(evdf, codf, syms, args), encoding="utf-8")
    evdf.to_pickle(ROOT / "cache" / "margin_topup_events.pkl")
    codf.to_pickle(ROOT / "cache" / "margin_topup_cohorts.pkl")
    print(f"-> {out}")


def _quarter(day):
    return str(pd.Timestamp(day * 86_400_000, unit="ms").to_period("Q"))


def report(evdf, codf, syms, args) -> str:
    hold = args.hold_days
    L = [f"# Recarga de margen y salida al precio del SL — {datetime.now():%Y-%m-%d} — BSC", "",
         f"Símbolos ({len(syms)}): {', '.join(syms)}  ",
         f"Meses {MONTHS[0]} → {MONTHS[-1]}. SL al {F:.0%} del camino a liquidación (mmr {MMR:.0%}); "
         f"venta límite Kyber llena si el on-chain la supera en {LO_EDGE:.1%}; caso B con la regla BSC "
         f"(TP −{RUNNER_TP:.0%} / {RUNNER_T} min). Swap del spot desnudo con {TAKERS[0]:.1%} o {TAKERS[1]:.1%}. "
         f"Valores en % del nocional.", "",
         "Salida: `d<δ> now` = venta límite a sl·(1−δ); si el SL dispara sin llenarla, swap al minuto "
         "siguiente (prod: `d0.5 now`). `d<δ> w<x>/<T>` = la límite sigue viva; swap si el on-chain cae "
         "x % bajo el SL o pasan T min.", ""]
    # ── por evento
    for lev in (2.0, 1.5):
        e = evdf[(evdf.lev == lev) & evdf.ev.notna()]
        L += [f"## Por evento de seguro — {lev:g}x (todas las entradas símbolo × día)", ""]
        n_ent = evdf[(evdf.lev == lev)].groupby(["exit", "tk"]).size().iloc[0]
        L += ["| salida | swap | eventos (A/B/AB) | % de entradas | media | p5 | peor | Δmedia vs prod | Δp5 | adoptable |",
              "|---|---|---|---|---|---|---|---|---|---|"]
        rows = []
        for (ex, tk), g in e.groupby(["exit", "tk"]):
            v = g.ev * 100
            c = g.case.value_counts()
            rows.append((ex, tk, len(g), f"{c.get('A', 0)}/{c.get('B', 0)}/{c.get('AB', 0)}",
                         len(g) / n_ent * 100, v.mean(), v.quantile(0.05), v.min()))
        base = {tk: next(r for r in rows if r[0] == "d0.5 now" and r[1] == tk) for tk in TAKERS}
        rows.sort(key=lambda r: -r[5])
        for ex, tk, n, cs, pct, mu, p5, worst in rows:
            b = base[tk]
            # El evento cambia con δ: la comparación justa es por entrada (todas las entradas).
            dm, dp = mu * n / n_ent - b[5] * b[2] / n_ent, p5 - b[6]
            ok = dm > 0 and dp >= -1.0
            L.append(f"| {ex} | {tk:.1%} | {n} ({cs}) | {pct:.1f} | {mu:+.2f} | {p5:+.2f} | {worst:+.2f} | "
                     f"{dm:+.3f} | {dp:+.2f} | {'**sí**' if ok and ex != 'd0.5 now' else ''} |")
        L += ["", "Δmedia = costo medio de los seguros **por entrada** (eventos × media / entradas) vs prod; "
              "Δp5 = p5 por evento vs prod. Criterio: Δmedia > 0 y Δp5 ≥ −1 pp.", ""]
    # ── cohortes
    L += [f"## Cartera: K={args.k} tokens/día por funding de 3 d, hold {hold} d, capital = spot + margen + reserva", ""]
    qs = sorted({_quarter(d) for d in codf.day.unique()})
    codf = codf.assign(q=codf.day.map(_quarter))
    base = codf[(codf.lev == 2.0) & (codf.reserve == 0) & (codf.exit == "d0.5 now")]
    L += ["| leverage | reserva | f_top | salida | swap | ROC anual | p5 cohorte | eventos/100 pos | "
          + " | ".join(qs) + " | trimestres ≥ prod |",
          "|---|---|---|---|---|---|---|---|" + "---|" * len(qs) + "---|"]
    rows = []
    for (lev, r, f, ex, tk), g in codf.groupby(["lev", "reserve", "f_top", "exit", "tk"]):
        if r == 0 and f != 0.7:
            continue
        roc = g.ret.mean() * 365 / hold * 100
        p5 = g.ret.quantile(0.05) * 100
        evr = g.events.sum() / g.n.sum() * 100
        b = base[base.tk == tk]
        qv = [g[g.q == q].ret.mean() * 365 / hold * 100 for q in qs]
        bq = [b[b.q == q].ret.mean() * 365 / hold * 100 for q in qs]
        win = sum(a >= c - 1e-9 for a, c in zip(qv, bq))
        rows.append((roc, lev, r, f, ex, tk, p5, evr, qv, win))
    rows.sort(key=lambda x: -x[0])
    keep = [x for x in rows if x[4] in ("d0.5 now", "d0 now", "d0.25 now")] + \
        [x for x in rows if x[4] not in ("d0.5 now", "d0 now", "d0.25 now")][:20]
    for roc, lev, r, f, ex, tk, p5, evr, qv, win in sorted(keep, key=lambda x: -x[0]):
        L.append(f"| {lev:g}x | {r:.0%} | {f:g} | {ex} | {tk:.1%} | {roc:+.1f} % | {p5:+.2f} | {evr:.1f} | "
                 + " | ".join(f"{v:+.1f}" for v in qv) + f" | {win}/{len(qs)} |")
    L += ["", f"ROC anual = media del retorno de la cohorte × 365/{hold}; p5 = retorno de la cohorte "
          f"({hold} d) en % del capital. Se listan las salidas `now` de todas las configuraciones y las 20 "
          "mejores con espera.", ""]
    return "\n".join(L)


if __name__ == "__main__":
    main()
