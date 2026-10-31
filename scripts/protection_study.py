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
Con --venue bsc el spot son los tokens solo-Alpha (sin spot en Binance, BSC,
liquidez ≥ 200k como en prod): velas 1m de la API pública de Binance Alpha
(precio de los trades on-chain), alineadas al perp con forward-fill en minutos
sin trades. La orden límite Kyber se asume llenada cuando ese precio la cruza.

Uso:
    python scripts/protection_study.py                  # 20 símbolos, 12 meses
    python scripts/protection_study.py --symbols 30 --months 6
    python scripts/protection_study.py --venue bsc      # tokens solo-Alpha (spot on-chain)
Salida: reports/protection_study_YYYYMMDD[_bsc].md (velas cacheadas en cache/binance/klines1m/).
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
# Caso A con espera (AIN #51): tras el SL el spot no se vende ya; queda una venta
# límite al objetivo (sp = la del seguro, perp = precio de recompra del short → salida
# neutra) con stop del spot desnudo X bajo el precio on-chain al
# dispararse el SL (acota lo que se arriesga frente a vender ya) y tiempo máximo T.
WAIT_TARGETS = ("sp", "perp")
WAIT_STOPS = (0.01, 0.02, 0.03, 0.05, 1.0)             # 1.0 = sin stop
WAIT_TIMEOUTS = (5, 15, 30, 60, 240, 1440)             # minutos
LO_EDGE = 0.005           # la orden límite Kyber se llena si el on-chain la supera en ε
TAKER_SLIPS = (0.005, 0.015)   # 1.5 % = lo que costó el swap de AIN #51 (con sándwich)
KINDS = {"spot": "spot/monthly/klines", "perp": "futures/um/monthly/klines",
         "mark": "futures/um/monthly/markPriceKlines"}
ALPHA_LIST_URL = "https://www.binance.com/bapi/defi/v1/public/wallet-direct/buw/wallet/cex/alpha/all/token/list"
ALPHA_KLINES_URL = "https://www.binance.com/bapi/defi/v1/public/alpha-trade/klines"
ALPHA_MIN_LIQ = 200_000              # mismo piso que prod (exchanges/onchain_bsc.py)
SPOT_SLIP = {"binance": SLIP, "bsc": 0.005}   # venta a mercado del spot desnudo (swap DEX)
ALPHA_IDS = {}                       # símbolo → alphaId (ALPHA_191)


# ── datos ───────────────────────────────────────────────────────────────────
def _alpha_bsc() -> dict:
    data = _get(ALPHA_LIST_URL, timeout=30).json()["data"]
    return {t["symbol"].upper(): t["alphaId"] for t in data
            if str(t.get("chainId")) == "56" and float(t.get("liquidity") or 0) >= ALPHA_MIN_LIQ
            and t.get("alphaId")}


def pick_symbols(n: int, months: list, venue: str = "binance") -> list:
    df = pd.read_csv(OUT_CSV, usecols=["symbol", "rate", "interval_hours", "funding_ts", "has_spot"])
    t0 = int(datetime.strptime(months[0], "%Y-%m").replace(tzinfo=timezone.utc).timestamp() * 1000)
    df = df[df.funding_ts >= t0]
    if venue == "bsc":
        ALPHA_IDS.update(_alpha_bsc())
        df = df[(df.has_spot == 0) & df.symbol.str.upper().isin(ALPHA_IDS)]
    else:
        df = df[df.has_spot == 1]
    df["daily"] = df.rate * 24 / df.interval_hours
    g = df.groupby("symbol").agg(daily=("daily", "mean"), n=("rate", "size"))
    g = g[(g.n >= 300) & ~g.index.str.match(r"^1+0+")]   # 1000PEPE: el spot no es el mismo
    g = g.sort_values("daily", ascending=False)
    return list(g.index[:n])


def _kinds(venue: str) -> list:
    return ["alpha", "perp", "mark"] if venue == "bsc" else list(KINDS)


def _alpha_month(sym: str, month: str) -> np.ndarray:
    """Velas 1m on-chain (API pública de Binance Alpha) de un mes, paginadas."""
    p = pd.Period(month, "M")
    t, stop = int(p.start_time.timestamp() * 1000), int((p + 1).start_time.timestamp() * 1000)
    rows = []
    while t < stop:
        r = _get(ALPHA_KLINES_URL, {"symbol": f"{ALPHA_IDS[sym.upper()]}USDT", "interval": "1m",
                                    "limit": 1500, "startTime": t, "endTime": stop - 1}, timeout=30)
        data = (r.json().get("data") or []) if r is not None else []
        if not data:
            break
        rows += [k[:5] for k in data]
        t = int(data[-1][0]) + 60_000
    return np.array(rows, dtype=float) if rows else np.empty((0, 5))


def _month_bars(sym: str, kind: str, month: str):
    path = KL_DIR / kind / f"{sym}-{month}.npz"
    if path.exists():
        z = np.load(path)
        return z["a"] if z["a"].size else None
    path.parent.mkdir(parents=True, exist_ok=True)
    if kind == "alpha":
        a = _alpha_month(sym, month)
        np.savez_compressed(path, a=a)
        return a if a.size else None
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


def load_symbol(sym: str, months: list, venue: str = "binance"):
    """DataFrame por minuto (ts) con spot_high, perp_open/high/low/close, mark_high."""
    parts = {}
    for kind in _kinds(venue):
        arrs = [a for a in (_month_bars(sym, kind, m) for m in months) if a is not None]
        if not arrs:
            return None
        a = np.vstack(arrs)
        parts[kind] = pd.DataFrame(a, columns=["ts", "o", "h", "l", "c"]).drop_duplicates("ts").set_index("ts")
    if venue == "bsc":
        # On-chain hay minutos sin trades: el precio es el último conocido.
        sp = parts.pop("alpha").reindex(parts["perp"].index)
        sp["c"] = sp.c.ffill()
        sp["h"] = sp.h.fillna(sp.c)
        sp["l"] = sp.l.fillna(sp.c)
        parts["spot"] = sp
    df = pd.DataFrame({"spot_h": parts["spot"].h, "spot_l": parts["spot"].l, "spot_c": parts["spot"].c,
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


def simulate(sym: str, df: pd.DataFrame, spot_slip: float = SLIP) -> list:
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
                    if delta == 0.005:
                        row.update(wait_exits(d, tp, end, sp, buy, e))
                        row["sl"] = sl
                    for tag, lag in (("now60", 1), ("now10", 0)):
                        k = min(tp + lag, end - 1)
                        hit = _first(d["spot_h"][tp + 1:k + 1] > sp) if k > tp else -1
                        sell = sp if hit >= 0 else d["spot_c"][k] * (1 - spot_slip)
                        row[tag] = (sell - buy) / e * 100
                else:
                    row["case"] = "AB"
                    row["now60"] = row["now10"] = (sp - perp_fill(tp)) / e * 100
                out.append(row)
    return out


def wait_exits(d: dict, tp: int, end: int, sp: float, buy: float, e: float) -> dict:
    """Caso A: short recomprado en el minuto tp a `buy`. Spot desnudo con venta límite
    al objetivo; sale taker si el on-chain cae al stop o vence T. Mismo minuto
    límite y stop → cuenta el stop (conservador). `now_<slip>` = regla vigente
    (vender ya, vigilante caliente). Resultado en % de la entrada."""
    out = {f"now_{s}": (d["spot_c"][tp] * (1 - s) - buy) / e * 100 for s in TAKER_SLIPS}
    out["sl_fill_lo"] = buy / (1 + SLIP)
    out["sl_fill_hi"] = d["perp_h"][tp]
    start = tp + 1
    if start >= end:
        return out
    for tgt, px in (("sp", sp), ("perp", buy)):
        w_fill = _first(d["spot_h"][start:end] >= px * (1 + LO_EDGE))
        for x in WAIT_STOPS:
            stop = d["spot_c"][tp] * (1 - x)
            w_stop = _first(d["spot_l"][start:end] <= stop)
            for T in WAIT_TIMEOUTS:
                lim = min(T, end - start)
                if 0 <= w_stop < lim and (w_fill < 0 or w_stop <= w_fill):
                    kind, k = "stop", start + w_stop
                elif 0 <= w_fill < lim:
                    kind, k = "fill", start + w_fill
                else:
                    kind, k = "timeout", start + lim - 1
                out[f"k_{tgt}_{x}_{T}"] = kind
                for s in TAKER_SLIPS:
                    if kind == "fill":
                        sell = px
                    else:
                        sell = (min(stop, d["spot_c"][k]) if kind == "stop" else d["spot_c"][k]) * (1 - s)
                    out[f"w_{tgt}_{x}_{T}_{s}"] = (sell - buy) / e * 100
    return out


# ── reporte ─────────────────────────────────────────────────────────────────
def report(res: pd.DataFrame, syms: list, months: list, venue: str = "binance") -> str:
    run_cols = [f"run{int(c * 100)}" for c in CALLBACKS]
    spot_src = ("spot on-chain BSC (velas de Binance Alpha), tokens solo-Alpha; venta del spot "
                f"desnudo con {SPOT_SLIP['bsc']:.1%} de impacto") if venue == "bsc" else "spot Binance"
    L = [f"# Estudio de seguros spot_perp — {datetime.now():%Y-%m-%d} — {spot_src}", "",
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


def wait_report(res: pd.DataFrame, syms: list, months: list, venue: str) -> str:
    """Caso A (δ = 0.5 %): vender el spot ya vs esperar con límite + stop + tiempo máximo.
    Adoptable = media ≥ vender ya y p5 no peor que el de vender ya en más de 0.5 pp,
    con el slippage taker realista (el más alto de TAKER_SLIPS) en los tres leverages."""
    s_hi = TAKER_SLIPS[-1]
    tl = lambda T: f"{T // 60} h" if T >= 60 else f"{T} min"  # noqa: E731
    L = [f"# Salida del spot desnudo tras el SL — {datetime.now():%Y-%m-%d} — venue {venue}", "",
         f"Símbolos ({len(syms)}): {', '.join(syms)}  ", f"Meses: {months[0]} → {months[-1]}; caso A con "
         f"δ = 0.5 %, SL al {FRACTION:.0%} del camino a liquidación.  ",
         "Resultado por evento en % del precio de entrada vs salida ideal (ambas piernas en el SL). "
         f"`ya` = regla vigente (vender el spot taker en el minuto del SL). Espera: venta límite al "
         f"objetivo (`sp` = la del seguro, `perp` = precio de recompra del short), llena si el on-chain "
         f"la supera en {LO_EDGE:.1%}; stop X bajo el precio on-chain del minuto del SL y tiempo máximo T (al vencer, taker). "
         f"Slippage taker {' / '.join(f'{x:.1%}' for x in TAKER_SLIPS)}.", ""]
    keys = [(t, x, T) for t in WAIT_TARGETS for x in WAIT_STOPS for T in WAIT_TIMEOUTS]
    ok_all = {k: True for k in keys}
    score = {k: 0.0 for k in keys}
    for lev in LEVERAGES:
        a = res[(res.lev == lev) & (res.case == "A") & (res.delta == 0.005)].dropna(subset=[f"now_{s_hi}"])
        if a.empty:
            continue
        base = a[f"now_{s_hi}"]
        b_mean, b_p5 = base.mean(), base.quantile(.05)
        slip_sl = (a.sl_fill_lo / a.sl - 1) * 100, (a.sl_fill_hi / a.sl - 1) * 100
        L += [f"## Leverage {lev}x — {len(a)} eventos A", "",
              f"Vender ya: media {a[f'now_{TAKER_SLIPS[0]}'].mean():+.2f} (slip {TAKER_SLIPS[0]:.1%}) / "
              f"{b_mean:+.2f} (slip {s_hi:.1%}), p5 {b_p5:+.2f}, peor {base.min():+.2f}.  ",
              f"Fill del SL vs trigger (cota baja = max(SL, open) / alta = high del minuto): mediana "
              f"{slip_sl[0].median():+.2f} % / {slip_sl[1].median():+.2f} %, p90 {slip_sl[0].quantile(.9):+.2f} % / "
              f"{slip_sl[1].quantile(.9):+.2f} %.", "",
              f"| objetivo | stop | T máx | media {TAKER_SLIPS[0]:.1%} | media {s_hi:.1%} | p5 | peor | "
              "% límite | % stop | % tiempo | adoptable |", "|" + "---|" * 11]
        rows = []
        for t, x, T in keys:
            v = a[f"w_{t}_{x}_{T}_{s_hi}"]
            kind = a[f"k_{t}_{x}_{T}"]
            ok = v.mean() >= b_mean and v.quantile(.05) >= b_p5 - 0.5
            ok_all[(t, x, T)] &= bool(ok)
            score[(t, x, T)] += v.mean()
            rows.append((v.mean(), [t, "sin stop" if x >= 1 else f"{x:.0%}", tl(T),
                                    f"{a[f'w_{t}_{x}_{T}_{TAKER_SLIPS[0]}'].mean():+.2f}", f"{v.mean():+.2f}",
                                    f"{v.quantile(.05):+.2f}", f"{v.min():+.2f}",
                                    f"{(kind == 'fill').mean():.0%}", f"{(kind == 'stop').mean():.0%}",
                                    f"{(kind == 'timeout').mean():.0%}", "sí" if ok else "no"]))
        for _, cells in sorted(rows, key=lambda r: -r[0]):
            L.append("| " + " | ".join(cells) + " |")
        L.append("")
    best = [k for k in keys if ok_all[k]]
    best.sort(key=lambda k: -score[k])
    L += ["## Veredicto", ""]
    if best:
        t, x, T = best[0]
        L.append(f"Adoptable en los tres leverages y mejor media: objetivo `{t}`, stop "
                 f"{'sin stop' if x >= 1 else f'{x:.0%}'}, T {tl(T)}. Otras adoptables: "
                 + (", ".join(f"{a}/{'—' if b >= 1 else f'{b:.0%}'}/{tl(c)}" for a, b, c in best[1:6]) or "ninguna")
                 + ".")
    else:
        L.append("Ninguna política de espera cumple el criterio en los tres leverages: se mantiene vender ya.")
    return "\n".join(L) + "\n"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--symbols", type=int, default=20)
    ap.add_argument("--months", type=int, default=12)
    ap.add_argument("--end", default=None, help="último mes YYYY-MM (default: el anterior al actual)")
    ap.add_argument("--workers", type=int, default=12)
    ap.add_argument("--venue", choices=("binance", "bsc"), default="binance")
    args = ap.parse_args()
    end = pd.Period(args.end, "M") if args.end else pd.Period(datetime.now(timezone.utc).strftime("%Y-%m"), "M") - 1
    months = [str(end - k) for k in range(args.months - 1, -1, -1)]
    syms = pick_symbols(args.symbols, months, args.venue)
    print(f"símbolos: {syms}", flush=True)
    with ThreadPoolExecutor(args.workers) as ex:
        list(ex.map(lambda t: _month_bars(*t),
                    [(s, k, m) for s in syms for k in _kinds(args.venue) for m in months]))
    rows = []
    for s in syms:
        df = load_symbol(s, months, args.venue)
        if df is None:
            print(f"  {s}: sin datos suficientes", flush=True)
            continue
        rows += simulate(s, df, SPOT_SLIP[args.venue])
        print(f"  {s}: {len(df)} minutos", flush=True)
    res = pd.DataFrame(rows)
    if res.empty:
        sys.exit("sin datos: nada que simular")
    suffix = "_bsc" if args.venue == "bsc" else ""
    out = ROOT / "reports" / f"protection_study_{datetime.now():%Y%m%d}{suffix}.md"
    out.write_text(report(res, syms, months, args.venue), encoding="utf-8")
    print(f"-> {out}")
    out = ROOT / "reports" / f"naked_spot_exit_{datetime.now():%Y%m%d}{suffix}.md"
    out.write_text(wait_report(res, syms, months, args.venue), encoding="utf-8")
    print(f"-> {out}")


if __name__ == "__main__":
    main()
