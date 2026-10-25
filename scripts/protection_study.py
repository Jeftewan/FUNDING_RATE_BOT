#!/usr/bin/env python3
"""Estudio de los SEGUROS spot_perp (SL del short + venta límite del spot) — LOCAL, read-only.

Pregunta: cuando el precio sube hasta el seguro, ¿qué pierna se llena primero y
cuánto cuesta cada caso?
  * A  el SL del short (mark price) dispara y la venta límite del spot NO →
       spot largo desnudo hasta que el vigilante lo vende a mercado.
  * B  la venta límite del spot se llena y el SL NO → short desnudo. Se compara
       recomprarlo ya (comportamiento previo) vs dejarlo correr con su SL duro +
       trailing stop de callback c.
  * AB ambas en el mismo minuto (salida cubierta).
Barre el offset δ de la venta spot (sl·(1−δ)): con δ>0 el spot tiende a salir
primero (B, riesgo acotado por el SL) en vez de quedar desnudo (A).

Datos: velas 1m de data.binance.vision (spot, perp last y perp MARK), símbolos
con spot y carry alto en cache/binance/fr_binance.csv. Una entrada por día y
símbolo, hold máx 30 d. Resultado por evento en % del precio de entrada, medido
contra la salida ideal (ambas piernas exactamente en el SL); el funding cobrado
por el short suelto NO se suma (sesgo conservador para el runner).
Limitación: el spot on-chain BSC (tokens solo-Alpha) no está en Binance.

Uso:
    python scripts/protection_study.py                  # 20 símbolos, 12 meses
    python scripts/protection_study.py --symbols 30 --months 6
Salida: reports/protection_study_YYYYMMDD.md (velas cacheadas en cache/binance/klines1m/).
"""
import argparse
import io
import sys
import zipfile
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
from binance_dataset import OUT_CSV, VISION, _get  # noqa: E402

KL_DIR = ROOT / "cache" / "binance" / "klines1m"
FRACTION = 0.80           # SL al 80 % del camino entrada → liquidación
MMR = 0.01                # mantenimiento típico de alts en el primer tramo
SLIP = 0.001              # deslizamiento de las órdenes a mercado / stop
HOLD_MIN = 30 * 1440
LEVERAGES = (1, 2, 3)
DELTAS = (0.0, 0.005, 0.01, 0.02)
CALLBACKS = (0.01, 0.02, 0.03, 0.05, 0.08)
TP_FALLS = (0.03, 0.05, 0.10, 0.20, 0.30)              # caída desde la venta spot
TIMEOUTS = (5, 15, 30, 60, 120, 240, 720, 1440, HOLD_MIN)   # minutos
KINDS = {"spot": "spot/monthly/klines", "perp": "futures/um/monthly/klines",
         "mark": "futures/um/monthly/markPriceKlines"}


# ── datos ───────────────────────────────────────────────────────────────────
def pick_symbols(n: int, months: list) -> list:
    df = pd.read_csv(OUT_CSV, usecols=["symbol", "rate", "interval_hours", "funding_ts", "has_spot"])
    t0 = int(datetime.strptime(months[0], "%Y-%m").replace(tzinfo=timezone.utc).timestamp() * 1000)
    df = df[(df.funding_ts >= t0) & (df.has_spot == 1)]
    df["daily"] = df.rate * 24 / df.interval_hours
    g = df.groupby("symbol").agg(daily=("daily", "mean"), n=("rate", "size"))
    g = g[(g.n >= 300) & ~g.index.str.match(r"^1+0+")]   # 1000PEPE: el spot no es el mismo
    g = g.sort_values("daily", ascending=False)
    return list(g.index[:n])


def _month_bars(sym: str, kind: str, month: str):
    path = KL_DIR / kind / f"{sym}-{month}.npz"
    if path.exists():
        z = np.load(path)
        return z["a"] if z["a"].size else None
    path.parent.mkdir(parents=True, exist_ok=True)
    pair = f"{sym}USDT"                  # el dataset guarda la base (BTC), vision el par
    r = _get(f"{VISION}/data/{KINDS[kind]}/{pair}/1m/{pair}-1m-{month}.zip", timeout=120)
    a = np.empty((0, 5))
    if r is not None:
        with zipfile.ZipFile(io.BytesIO(r.content)) as zf:
            raw = zf.read(zf.namelist()[0]).decode()
        rows = [ln.split(",")[:5] for ln in raw.splitlines() if ln and ln[0].isdigit()]
        a = np.array(rows, dtype=float)
        a[:, 0] = np.where(a[:, 0] > 1e14, a[:, 0] // 1000, a[:, 0])   # spot 2025+: µs
    np.savez_compressed(path, a=a)
    return a if a.size else None


def load_symbol(sym: str, months: list):
    """DataFrame por minuto (ts) con spot_high, perp_open/high/low/close, mark_high."""
    parts = {}
    for kind in KINDS:
        arrs = [a for a in (_month_bars(sym, kind, m) for m in months) if a is not None]
        if not arrs:
            return None
        a = np.vstack(arrs)
        parts[kind] = pd.DataFrame(a, columns=["ts", "o", "h", "l", "c"]).drop_duplicates("ts").set_index("ts")
    df = pd.DataFrame({"spot_h": parts["spot"].h, "spot_c": parts["spot"].c,
                       "perp_o": parts["perp"].o, "perp_h": parts["perp"].h,
                       "perp_l": parts["perp"].l, "perp_c": parts["perp"].c,
                       "mark_h": parts["mark"].h}).dropna().sort_index()
    return df if len(df) > HOLD_MIN else None


# ── simulación ──────────────────────────────────────────────────────────────
def _first(mask: np.ndarray) -> int:
    i = int(np.argmax(mask))
    return i if mask[i] else -1


def runner_exit(d: dict, t: int, end: int, sl: float, c: float) -> float:
    """Short suelto desde el minuto t: SL duro (mark ≥ sl) o trailing c sobre el
    mínimo del last. Precio de recompra."""
    lo = d["perp_c"][t]
    for k in range(t + 1, end):
        if d["mark_h"][k] >= sl:
            return max(sl, d["perp_o"][k]) * (1 + SLIP)
        trig = lo * (1 + c)
        if d["perp_h"][k] >= trig:
            return max(trig, d["perp_o"][k]) * (1 + SLIP)
        lo = min(lo, d["perp_l"][k])
    return d["perp_c"][end - 1] * (1 + SLIP)


def tp_time_exits(d: dict, t: int, end: int, sl: float, sp: float, e: float) -> dict:
    """Short suelto desde el minuto t: SL duro (mark ≥ sl), recompra al caer f desde
    la venta spot (take profit) o a mercado al vencer T minutos. Mismo minuto SL y
    TP → cuenta el SL (conservador)."""
    w_sl = _first(d["mark_h"][t + 1:end] >= sl)
    out = {}
    for f in TP_FALLS:
        px = sp * (1 - f)
        w_tp = _first(d["perp_l"][t + 1:end] <= px)
        for T in TIMEOUTS:
            lim = min(T, end - t - 1)
            hits = [(w, kind) for w, kind in ((w_sl, "sl"), (w_tp, "tp")) if 0 <= w < lim]
            if hits:
                w, kind = min(hits)
                k = t + 1 + w
                buy = max(sl, d["perp_o"][k]) if kind == "sl" else min(px, d["perp_o"][k])
            else:
                buy = d["perp_c"][t + lim]
            out[f"tp{int(f * 100)}_{T}"] = (sp - buy * (1 + SLIP)) / e * 100
    return out


def simulate(sym: str, df: pd.DataFrame) -> list:
    d = {k: df[k].to_numpy() for k in df.columns}
    n = len(df)
    out = []
    for i in range(0, n - 1440, 1440):                     # una entrada por día
        end = min(n, i + HOLD_MIN)
        e = d["perp_c"][i]
        for lev in LEVERAGES:
            sl = e + FRACTION * (e * (1 / lev - MMR))
            tp = _first(d["mark_h"][i + 1:end] >= sl)
            tp = tp + i + 1 if tp >= 0 else -1
            for delta in DELTAS:
                sp = sl * (1 - delta)
                ts = _first(d["spot_h"][i + 1:end] > sp)
                ts = ts + i + 1 if ts >= 0 else -1
                if tp < 0 and ts < 0:
                    out.append({"sym": sym, "lev": lev, "delta": delta, "case": "none"})
                    continue
                perp_fill = lambda k: max(sl, d["perp_o"][k]) * (1 + SLIP)   # noqa: E731
                row = {"sym": sym, "lev": lev, "delta": delta}
                if ts >= 0 and (tp < 0 or ts < tp):
                    # B: spot vendido a sp; el short sigue.
                    row["case"] = "B"
                    for tag, lag in (("now60", 1), ("now10", 0)):
                        k = min(ts + lag, end - 1)
                        hit = _first(d["mark_h"][ts + 1:k + 1] >= sl) if k > ts else -1
                        buy = perp_fill(ts + 1 + hit) if hit >= 0 else d["perp_c"][k] * (1 + SLIP)
                        row[tag] = (sp - buy) / e * 100
                    for c in CALLBACKS:
                        row[f"run{int(c * 100)}"] = (sp - runner_exit(d, ts, end, sl, c)) / e * 100
                    row.update(tp_time_exits(d, ts, end, sl, sp, e))
                elif tp >= 0 and (ts < 0 or tp < ts):
                    # A: short recomprado por el SL; spot desnudo hasta venderlo.
                    row["case"] = "A"
                    buy = perp_fill(tp)
                    for tag, lag in (("now60", 1), ("now10", 0)):
                        k = min(tp + lag, end - 1)
                        hit = _first(d["spot_h"][tp + 1:k + 1] > sp) if k > tp else -1
                        sell = sp if hit >= 0 else d["spot_c"][k] * (1 - SLIP)
                        row[tag] = (sell - buy) / e * 100
                else:
                    row["case"] = "AB"
                    row["now60"] = row["now10"] = (sp - perp_fill(tp)) / e * 100
                out.append(row)
    return out


# ── reporte ─────────────────────────────────────────────────────────────────
def report(res: pd.DataFrame, syms: list, months: list) -> str:
    run_cols = [f"run{int(c * 100)}" for c in CALLBACKS]
    L = [f"# Estudio de seguros spot_perp — {datetime.now():%Y-%m-%d}", "",
         f"Símbolos ({len(syms)}): {', '.join(syms)}  ", f"Meses: {months[0]} → {months[-1]}; "
         f"una entrada por día, hold ≤ 30 d; SL al {FRACTION:.0%} del camino a liquidación "
         f"(mmr {MMR:.0%}), slippage {SLIP:.1%}.  ",
         "Resultado = % del precio de entrada vs salida ideal (ambas piernas en el SL). "
         "`now60/now10` = cerrar la otra pierna a mercado tras 60 s / 10 s; "
         "`runN` = caso B dejando el short con SL duro + trailing N %.", ""]
    for lev in LEVERAGES:
        r = res[res.lev == lev]
        L += [f"## Leverage {lev}x", "",
              "| δ | entradas | % con evento | A | B | AB | A now60 | A now10 | B now60 | "
              + " | ".join(f"B {c}" for c in run_cols) + " |",
              "|" + "---|" * (9 + len(run_cols))]
        for delta in DELTAS:
            g = r[r.delta == delta]
            ev = g[g.case != "none"]
            a, b = ev[ev.case == "A"], ev[ev.case == "B"]
            cells = [f"{delta:.1%}", f"{len(g)}", f"{len(ev) / max(len(g), 1):.1%}",
                     f"{len(a)}", f"{len(b)}", f"{(ev.case == 'AB').sum()}",
                     f"{a.now60.mean():+.2f}" if len(a) else "—",
                     f"{a.now10.mean():+.2f}" if len(a) else "—",
                     f"{b.now60.mean():+.2f}" if len(b) else "—"]
            cells += [f"{b[c].mean():+.2f}" if len(b) else "—" for c in run_cols]
            L.append("| " + " | ".join(cells) + " |")
        L.append("")
        # Esperado por evento combinando la política (A now10, B runner) por δ.
        L += ["Esperado por evento (A con vigilante caliente 10 s, AB cubierto, B con cada política):", "",
              "| δ | B recompra 60 s | " + " | ".join(f"B {c}" for c in run_cols) + " |",
              "|" + "---|" * (2 + len(run_cols))]
        for delta in DELTAS:
            ev = r[(r.delta == delta) & (r.case != "none")]
            if ev.empty:
                continue
            base = np.where(ev.case == "A", ev.now10, np.where(ev.case == "AB", ev.now60, np.nan))
            cells = [f"{delta:.1%}"]
            for col in ["now60"] + run_cols:
                v = np.where(ev.case == "B", ev[col], base)
                cells.append(f"{np.nanmean(v):+.2f}")
            L.append("| " + " | ".join(cells) + " |")
        L.append("")
        bb = r[(r.case == "B")]
        if len(bb):
            L += ["Distribución caso B (todas las δ), percentiles p10/p50/p90:", ""]
            for col in ["now60"] + run_cols:
                q = bb[col].quantile([.1, .5, .9]).round(2).tolist()
                L.append(f"- `{col}`: {q[0]:+.2f} / {q[1]:+.2f} / {q[2]:+.2f}")
            L.append("")
    L += ["## Caso B: short suelto con TP por caída y tiempo máximo", "",
          "Media por evento B (δ = 0.5 %) en % de la entrada; filas = tiempo máximo antes de "
          "recomprar a mercado, columnas = caída desde la venta spot que activa la recompra (TP). "
          "El SL duro sigue vivo. Referencia: recomprar en 60 s.", ""]
    tlabel = lambda T: "30 d" if T == HOLD_MIN else (f"{T // 60} h" if T >= 60 else f"{T} min")  # noqa: E731
    for lev in LEVERAGES:
        b = res[(res.lev == lev) & (res.case == "B") & (res.delta == 0.005)]
        if b.empty:
            continue
        L += [f"### Leverage {lev}x — {len(b)} eventos B; recomprar ya: media {b.now60.mean():+.2f}, "
              f"mediana {b.now60.median():+.2f}", "",
              "| T máx | " + " | ".join(f"TP −{int(f * 100)} %" for f in TP_FALLS) + " |",
              "|" + "---|" * (1 + len(TP_FALLS))]
        for T in TIMEOUTS:
            cols = [f"tp{int(f * 100)}_{T}" for f in TP_FALLS]
            L.append(f"| {tlabel(T)} | "
                     + " | ".join(f"{b[c].mean():+.2f} ({b[c].median():+.2f})" for c in cols) + " |")
        L += ["", "Celdas: media (mediana).", ""]
    return "\n".join(L)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--symbols", type=int, default=20)
    ap.add_argument("--months", type=int, default=12)
    ap.add_argument("--end", default=None, help="último mes YYYY-MM (default: el anterior al actual)")
    ap.add_argument("--workers", type=int, default=12)
    args = ap.parse_args()
    end = pd.Period(args.end, "M") if args.end else pd.Period(datetime.now(timezone.utc).strftime("%Y-%m"), "M") - 1
    months = [str(end - k) for k in range(args.months - 1, -1, -1)]
    syms = pick_symbols(args.symbols, months)
    print(f"símbolos: {syms}", flush=True)
    with ThreadPoolExecutor(args.workers) as ex:
        list(ex.map(lambda t: _month_bars(*t), [(s, k, m) for s in syms for k in KINDS for m in months]))
    rows = []
    for s in syms:
        df = load_symbol(s, months)
        if df is None:
            print(f"  {s}: sin datos suficientes", flush=True)
            continue
        rows += simulate(s, df)
        print(f"  {s}: {len(df)} minutos", flush=True)
    res = pd.DataFrame(rows)
    if res.empty:
        sys.exit("sin datos: nada que simular")
    out = ROOT / "reports" / f"protection_study_{datetime.now():%Y%m%d}.md"
    out.write_text(report(res, syms, months), encoding="utf-8")
    print(f"-> {out}")


if __name__ == "__main__":
    main()
