#!/usr/bin/env python3
"""Costo de ejecución ida y vuelta del spot on-chain (tokens solo-Alpha) — LOCAL, read-only.

Pregunta: ¿conviene esperar un basis favorable para entrar y salir, y frente a qué
método (swap inmediato, swap paciente, orden límite Kyber)?

Datos: velas 1m cacheadas por scripts/protection_study.py --venue bsc (on-chain y perp),
funding liquidado de cache/binance/fr_binance.csv y curva de impacto por tamaño de
cache/binance/alpha_depth_20261027.csv (mediana si el token no está).

Muestra: un intento cada 6 h por token, solo con funding alto (media diaria de los
últimos 3 pagos ≥ --min-daily %), hold fijo de 7 días. Todo se mide en % del nocional,
incluye comisión perp taker y ejecuta en la vela siguiente a la señal.

Política ganadora elegida walk-forward: cada trimestre usa la mejor mediana de los
trimestres anteriores. Veredicto: la espera se activa por defecto solo si mejora la
mediana y el p5 frente al swap inmediato troceado en la mayoría de trimestres.

Uso:  python scripts/execution_study.py [--notional 1600] [--slices 4]
Salida: reports/execution_study_YYYYMMDD.md
"""
import argparse
import sys
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
from binance_dataset import OUT_CSV  # noqa: E402
from protection_study import KL_DIR, _month_bars  # noqa: E402

DEPTH_CSV = ROOT / "cache" / "binance" / "alpha_depth_20261027.csv"
REPORT = ROOT / "reports"
FEE = 0.05                # perp taker, %
HOLD = 7 * 1440
STEP = 360
WINDOWS = (30, 120, 360, 1440)
THETAS = (0.2, 0.4)
THETA_EXIT = 0.0
LIMIT_MARGIN = 0.1        # el on-chain debe cruzar la orden por este % para que un taker la llene
LIMIT_THETA = 0.2
LIMIT_PEG = 1             # minutos entre re-anclados
QUARTER_MS = 91 * 86400 * 1000


def symbols_months() -> dict:
    out = defaultdict(list)
    for f in (KL_DIR / "alpha").glob("*.npz"):
        sym, month = f.stem.split("-", 1)
        if (KL_DIR / "perp" / f.name).exists():
            out[sym].append(month)
    return {s: sorted(m) for s, m in out.items()}


def load(sym: str, months: list):
    alpha = [a for a in (_month_bars(sym, "alpha", m) for m in months) if a is not None]
    perp = [a for a in (_month_bars(sym, "perp", m) for m in months) if a is not None]
    if not alpha or not perp:
        return None
    a, p = np.vstack(alpha), np.vstack(perp)
    a, p = a[np.argsort(a[:, 0])], p[np.argsort(p[:, 0])]
    ts, a_ts = p[:, 0].astype(np.int64), a[:, 0].astype(np.int64)
    i = np.searchsorted(a_ts, ts)
    ok = i < len(a_ts)
    ok[ok] = a_ts[i[ok]] == ts[ok]
    sl, sh, sc = (np.full(len(ts), np.nan) for _ in range(3))
    sh[ok], sl[ok], sc[ok] = a[i[ok], 2], a[i[ok], 3], a[i[ok], 4]
    return {"ts": ts, "po": p[:, 1], "ph": p[:, 2], "pl": p[:, 3], "pc": p[:, 4],
            "sl": sl, "sh": sh, "sc": sc}


def impact_fn(depth: pd.DataFrame):
    sizes = np.array([1000, 2600, 5000, 13000, 26000], float)
    cols = [f"imp_{int(s)}" for s in sizes]
    median = depth[cols].median().to_numpy()
    table = {r.symbol: r[cols].to_numpy(float) for _, r in depth.iterrows()}

    def impact(sym: str, usd: float) -> float:
        curve = table.get(sym, median)
        # Bajo 1000 USD el impacto se escala lineal desde cero.
        return float(np.interp(usd, np.r_[0, sizes], np.r_[0, np.maximum(curve, 0)]))
    return impact


def signal(basis: np.ndarray) -> np.ndarray:
    """Mediana de 3 velas con al menos 2 con trades on-chain."""
    arr = np.vstack([basis, np.r_[np.nan, basis[:-1]], np.r_[np.nan, np.nan, basis[:-2]]])
    n = (~np.isnan(arr)).sum(0)
    out = np.full(len(basis), np.nan)
    out[n >= 2] = np.nanmedian(arr[:, n >= 2], axis=0)
    return out


class Sim:
    def __init__(self, d, funding_ts, funding_pct):
        self.d = d
        self.basis = (d["pc"] / d["sc"] - 1) * 100
        self.sig = signal(self.basis)
        self.real = np.where(~np.isnan(self.basis))[0]
        self.f_ts, self.f_cum = funding_ts, np.r_[0, np.cumsum(funding_pct)]

    def funding(self, i, j):
        ts = self.d["ts"]
        return float(self.f_cum[np.searchsorted(self.f_ts, ts[j], "right")]
                     - self.f_cum[np.searchsorted(self.f_ts, ts[i], "right")])

    def nxt(self, i):
        k = np.searchsorted(self.real, i + 1)
        return None if k >= len(self.real) or self.real[k] > i + 5 else int(self.real[k])

    def buy(self, i, imp):
        k = self.nxt(i)
        return None if k is None else (self.d["pc"][k] / (self.d["sc"][k] * (1 + imp / 100)) * 100 - 100 - FEE, k)

    def sell(self, i, imp):
        k = self.nxt(i)
        return None if k is None else ((self.d["sc"][k] * (1 - imp / 100)) / self.d["pc"][k] * 100 - 100 - FEE, k)

    def immediate(self, t, T, imp):
        e, x = self.buy(t, imp), self.sell(T, imp)
        return None if e is None or x is None else (e[0] + x[0], e[1], x[1])

    def patient(self, t, T, W, theta, imp):
        hit = np.where(self.sig[t:t + W] >= theta)[0]
        if not len(hit):
            return None
        out = np.where(self.sig[T:T + W] <= THETA_EXIT)[0]
        e = self.buy(t + int(hit[0]), imp)
        x = self.sell(T + (int(out[0]) if len(out) else W), imp)
        return None if e is None or x is None else (e[0] + x[0], e[1], x[1])

    def _limit_fill(self, start, W, sign, gate_theta, imp_fallback):
        """Índice de llenado y precio de la orden; sign=+1 compra, −1 venta."""
        d = self.d
        bars = np.arange(start, start + W)
        anchor = start - 1 + ((bars - start) // LIMIT_PEG) * LIMIT_PEG
        limit = d["pc"][anchor] * (1 - sign * LIMIT_THETA / 100)
        allowed = np.ones(len(bars), bool) if gate_theta is None else \
            (self.sig[bars] >= gate_theta if sign > 0 else self.sig[bars] <= gate_theta)
        cross = d["sl"][bars] <= limit * (1 - LIMIT_MARGIN / 100) if sign > 0 \
            else d["sh"][bars] >= limit * (1 + LIMIT_MARGIN / 100)
        hit = np.where(allowed & cross)[0]
        return (int(bars[hit[0]]), float(limit[hit[0]])) if len(hit) else None

    def limit(self, t, T, W, imp, gate):
        d = self.d
        fe = self._limit_fill(t, W, +1, gate, imp)
        if fe is None:
            return None
        e = d["po"][fe[0] + 1] / fe[1] * 100 - 100 - FEE
        fx = self._limit_fill(T, W, -1, 0.0 if gate is not None else None, imp)
        if fx is not None:
            return e + fx[1] / d["po"][fx[0] + 1] * 100 - 100 - FEE, fe[0], fx[0]
        x = self.sell(T + W, imp)          # no llenó: salida por swap al vencer
        return None if x is None else (e + x[0], fe[0], x[1])

    def _two_limits_leg(self, i, W, tm, imp, entering):
        d = self.d
        ref = d["sc"][i]
        if np.isnan(ref):
            return None
        price = (ref + d["pc"][i]) / 2
        bars = np.arange(i, i + W)
        if entering:
            spot, perp = d["sl"][bars] <= price * (1 - LIMIT_MARGIN / 100), d["ph"][bars] >= price
        else:
            spot, perp = d["sh"][bars] >= price * (1 + LIMIT_MARGIN / 100), d["pl"][bars] <= price
        s = np.where(spot)[0]
        p = np.where(perp)[0]
        if not len(s) and not len(p):
            return None
        s0, p0 = (int(s[0]) if len(s) else None), (int(p[0]) if len(p) else None)
        if s0 is not None and p0 is not None and abs(s0 - p0) <= tm:
            return -0.02
        late = bars[min(s0 if s0 is not None else 10 ** 9, p0 if p0 is not None else 10 ** 9)] + tm
        late = min(late, len(d["ts"]) - 2)
        mkt_spot = d["sc"][late] if not np.isnan(d["sc"][late]) else ref
        if s0 is not None and (p0 is None or s0 < p0):      # spot lleno: perp a mercado
            perp_px = d["po"][late + 1]
            return (perp_px / price if entering else price / perp_px) * 100 - 100 - FEE
        spot_px = mkt_spot * (1 + imp / 100) if entering else mkt_spot * (1 - imp / 100)
        return (price / spot_px if entering else spot_px / price) * 100 - 100 - 0.02

    def two_limits(self, t, T, W, tm, imp):
        e, x = self._two_limits_leg(t, W, tm, imp, True), self._two_limits_leg(T, W, tm, imp, False)
        return None if e is None or x is None else (e + x, t, T)


def summarize(values):
    a = np.array(values, float)
    return {"n": len(a), "mean": a.mean(), "median": np.median(a), "p5": np.percentile(a, 5),
            "win": (a > 0).mean() * 100}


def run(args):
    syms = symbols_months()
    depth = pd.read_csv(DEPTH_CSV)
    impact = impact_fn(depth)
    fr = pd.read_csv(OUT_CSV, usecols=["symbol", "rate", "interval_hours", "funding_ts"])
    fr = fr[fr.symbol.isin(syms)].sort_values("funding_ts")
    groups = {s: g for s, g in fr.groupby("symbol")}
    rows = defaultdict(list)          # policy → [(quarter, net)]
    used = 0
    for sym, months in sorted(syms.items()):
        d = load(sym, months)
        g = groups.get(sym)
        if d is None or g is None or len(d["ts"]) < 40000:
            continue
        with np.errstate(invalid="ignore"):
            if np.nanmean(np.abs(d["pc"] / d["sc"] - 1) * 100) > 3:
                continue               # el token on-chain no es el mismo activo que el perp
        used += 1
        f_ts, f_rate = g.funding_ts.to_numpy(np.int64), g.rate.to_numpy(float) * 100
        daily = f_rate * 24 / g.interval_hours.to_numpy(float)
        sim = Sim(d, f_ts, f_rate)
        imp_full, imp_cut = impact(sym, args.notional), impact(sym, args.notional / args.slices)
        for t in range(1440, len(d["ts"]) - HOLD - 1500, STEP):
            k = np.searchsorted(f_ts, d["ts"][t], "right")
            if k < 3 or daily[k - 3:k].mean() < args.min_daily:
                continue
            T = t + HOLD
            q = int(d["ts"][t] // QUARTER_MS)
            base = sim.funding(t, T)

            def add(name, res):
                if res is not None:
                    rows[name].append((q, res[0] + sim.funding(res[1], res[2]) - base))

            add("swap inmediato", sim.immediate(t, T, imp_full))
            add("swap inmediato troceado", sim.immediate(t, T, imp_cut))
            for W in WINDOWS:
                for th in THETAS:
                    add(f"swap paciente {W}m basis≥{th}", sim.patient(t, T, W, th, imp_full))
                    add(f"swap paciente troceado {W}m basis≥{th}", sim.patient(t, T, W, th, imp_cut))
            add("límite en espera 360m", sim.limit(t, T, 360, imp_full, None))
            add("límite con gate 360m basis≥0.4", sim.limit(t, T, 360, imp_full, 0.4))
            for tm in (1, 5, 30):
                add(f"dos límites mismo precio, seguro {tm}m", sim.two_limits(t, T, 120, tm, imp_full))
        print(f"{sym}: {sum(len(v) for v in rows.values())} filas", flush=True)
    return rows, used


def walk_forward(rows):
    """Cada trimestre usa la política patient con mejor mediana en los anteriores."""
    candidates = [k for k in rows if k.startswith("swap paciente troceado")]
    quarters = sorted({q for k in candidates for q, _ in rows[k]})
    picks, oos = [], []
    for i, q in enumerate(quarters[1:], 1):
        past = set(quarters[:i])
        best = max(candidates, key=lambda k: np.median([v for qq, v in rows[k] if qq in past] or [-99]))
        picks.append((q, best))
        oos += [v for qq, v in rows[best] if qq == q]
    return picks, oos


def verdict(rows, best):
    base = rows["swap inmediato troceado"]
    quarters = sorted({q for q, _ in base})
    wins = total = 0
    for q in quarters:
        a, b = [v for qq, v in rows[best] if qq == q], [v for qq, v in base if qq == q]
        if len(a) < 30 or len(b) < 30:
            continue
        total += 1
        wins += np.median(a) > np.median(b) and np.percentile(a, 5) > np.percentile(b, 5)
    return wins, total


def report(rows, used, args):
    lines = [f"# Costo de ejecución on-chain — {datetime.now(timezone.utc):%Y-%m-%d}", "",
             f"{used} tokens Alpha, nocional {args.notional:g} USD, {args.slices} tramos, funding "
             f"≥ {args.min_daily} %/día, hold 7 d. Valores en % del nocional: ejecución ida y vuelta "
             "más el funding ganado o perdido por esperar (vs entrar y salir sin esperar).", "",
             "| Método | n | Media | Mediana | p5 | Veces > 0 |", "|---|---|---|---|---|---|"]
    table = {k: summarize([v for _, v in vals]) for k, vals in rows.items() if len(vals) >= 30}
    for k, s in sorted(table.items(), key=lambda kv: -kv[1]["median"]):
        lines.append(f"| {k} | {s['n']} | {s['mean']:+.2f} | {s['median']:+.2f} | {s['p5']:+.2f} | {s['win']:.0f} % |")
    picks, oos = walk_forward(rows)
    wf = summarize(oos) if oos else None
    lines += ["", "## Walk-forward (elige por mediana de trimestres anteriores)", ""]
    lines += [f"- Trimestre {q}: {name}" for q, name in picks]
    if wf:
        lines.append(f"\nFuera de muestra: media {wf['mean']:+.2f}, mediana {wf['median']:+.2f}, "
                     f"p5 {wf['p5']:+.2f}, veces > 0 {wf['win']:.0f} % (n={wf['n']}).")
    best = picks[-1][1] if picks else None
    if best:
        wins, total = verdict(rows, best)
        ok = total > 0 and wins > total / 2
        lines += ["", "## Veredicto", "",
                  f"Política vigente: **{best}**. Mejora mediana y p5 frente al swap inmediato troceado en "
                  f"{wins}/{total} trimestres → espera por defecto: **{'SÍ' if ok else 'NO'}**."]
    path = REPORT / f"execution_study_{datetime.now(timezone.utc):%Y%m%d}.md"
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print("\n".join(lines))
    print(f"\nReporte: {path}")


def main():
    sys.stdout.reconfigure(encoding="utf-8")
    ap = argparse.ArgumentParser()
    ap.add_argument("--notional", type=float, default=1600)
    ap.add_argument("--slices", type=int, default=4)
    ap.add_argument("--min-daily", type=float, default=0.05)
    args = ap.parse_args()
    rows, used = run(args)
    report(rows, used, args)


if __name__ == "__main__":
    main()
