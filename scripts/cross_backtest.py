#!/usr/bin/env python3
"""Backtest cross perp (Binance/Bybit/OKX/Bitget) vs spot_perp Binance — LOCAL, read-only.

Pregunta: ¿reactivar `cross_exchange`? Se compara a IGUAL RIESGO (leverage ≤ 3x por
pierna, drawdown ≤ 5 %) contra spot_perp Binance con el mismo motor:

  * Rejilla común de 8 h (00/08/16 UTC). Cada venue suma sus liquidaciones del bucket
    (8 h | 4 h ×2 | 1 h ×8) → Δf emparejado por tiempo aunque los intervalos difieran.
  * Par cross (A long, B short): Δ_bucket = f_B − f_A. spot_perp = mismo motor con la
    pierna long = spot (funding 0).
  * Entrada en la frontera j (justo después del pago, no lo cobra); salida tras DOS
    buckets seguidos con Δ ≤ 0 (equivale a CLOSE_SUGGESTED / política B de
    scripts/exit_policy_study.py), tope 30 d (H30). Fin de datos → se valora a la fecha.
  * Leverage L por pierna: cross N = slot·L/2 por pierna; spot_perp N = slot·L/(L+1).
  * Riesgo por pierna con klines 1d: seguro (SL) al 80 % de la distancia a liquidación
    (1/L − mmr) desde el precio de referencia → cierre anticipado + costo de evento.
    Con rebalanceo diario el precio de referencia se re-centra al cierre si el movimiento
    supera la mitad de la distancia (cross: transferencia entre venues, costo fijo; spot_perp:
    vender spot / reducir short, costo proporcional). Latencia = 1 día (granularidad 1d).
  * Cartera como scripts/exit_policy_study.py: 5 slots, 5.200 USD, un símbolo una vez.
  * Walk-forward mensual (12 meses) eligiendo la config con datos ANTERIORES (DD ≤ 5 %)
    + hold-out intocado de 30 d. Fees: base (taker) y optimista (maker / realizado).

Entradas: cache/cross/fr_*.csv + klines_1d.csv (scripts/cross_dataset.py),
cache/binance/fr_binance.csv y cache/binance/exit_study_rows.pkl (rankings del modelo
vigente y del heurístico, scripts/exit_policy_study.py).
Salida: reports/cross_vs_spotperp_YYYYMMDD.md
"""
import argparse
import math
import re
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
CROSS = ROOT / "cache" / "cross"
BIN_CSV = ROOT / "cache" / "binance" / "fr_binance.csv"
ROWS = ROOT / "cache" / "binance" / "exit_study_rows.pkl"
UROWS = ROOT / "cache" / "binance" / "universe_rows.pkl"     # CEX ∪ Alpha (attribution_study.py)
ALPHA = ROOT / "cache" / "binance" / "alpha_tokens.json"
REPORT_DIR = ROOT / "reports"

H = 3_600_000
BK = 8                              # horas por bucket de decisión
CAPITAL, SLOTS = 5200.0, 5
SLOT = CAPITAL / SLOTS
MIN_VOLUME = 100_000
BASE_DAILY = 0.0003                 # 0.01 %/8 h → piso del scan
MAX_BUCKETS = 90                    # 30 d
HOLDOUT_DAYS, WF_MONTHS, MAX_DD = 30, 12, 0.05
VENUES = ("binance", "bybit", "okx", "bitget")
TAKER = {"binance": 0.0005, "bybit": 0.00055, "okx": 0.0005, "bitget": 0.0006}
MAKER, SLIP, SLIP_MAKER = 0.0002, 0.0002, 0.0001
SP_FEE = {"base": 0.003, "opt": 0.002}          # convención de los estudios previos / realizado
ALPHA_FEE = 0.0041                               # mediana real de los trades Alpha (attribution_20261025)
MMR, SL_FRAC, REBAL_FRAC, EVENT_COST = 0.01, 0.80, 0.50, 0.003
REBAL_USD, ENTRY_USD = 1.0, 1.0                 # retiro USDT entre venues / pre-posicionar capital
SP_REBAL_COST = 0.0019                           # (spot taker + perp taker + 2 slip) por unidad movida
LEVERAGES = (1, 2, 3)
THRESHOLDS = (0.0003, 0.0006, 0.001)
SIGNALS = ("s3", "s7", "cur")
TRADFI = {"TSLA", "NVDA", "AAPL", "MSTR", "COIN", "HOOD", "AMZN", "GOOGL", "GOOG", "META", "MSFT",
          "SPY", "QQQ", "XAU", "XAG", "CRCL", "INTC", "AMD", "NFLX", "PLTR", "BABA", "GME", "SPX",
          "NDX", "XAUT", "PAXG", "CL", "NATGAS", "COPPER", "EWY", "EWJ", "MU", "ORCL", "AVGO", "TSM"}
_MULT = re.compile(r"^(1000000|100000|10000|1000)(?=[A-Z])")


def canon(base: str) -> str:
    return _MULT.sub("", base.upper().removesuffix("USDT")) + "USDT"


# ── Libro de series sobre la rejilla de 8 h ─────────────────────────────────
class Grid:
    def __init__(self, h0_ms, t_end_ms):
        self.h0 = h0_ms
        self.n_hours = int((t_end_ms - h0_ms) // H) + 1
        self.nb = (self.n_hours + BK - 1) // BK + 1

    def time(self, j):
        return pd.Timestamp(self.h0 + int(j) * BK * H, unit="ms", tz="UTC")

    def bucket(self, ts):
        return int(math.ceil((pd.Timestamp(ts).value // 1_000_000 - self.h0) / (BK * H)))


def build_leg(df, grid, extra=()):
    """Matrices por símbolo: D (suma del bucket), cov (cubierto), cur (tasa diaria del
    último pago ≤ frontera) y columnas `extra` forward-filled a la frontera."""
    syms = sorted(df["symbol"].unique())
    sidx = {s: k for k, s in enumerate(syms)}
    ns, nb = len(syms), grid.nb
    D = np.zeros((ns, nb))
    cov = np.zeros((ns, nb), bool)
    cur = np.full((ns, nb), np.nan)
    ext = {c: np.full((ns, nb), np.nan) for c in extra}
    bounds = np.arange(nb) * BK
    df = df.sort_values(["symbol", "funding_ts"])
    for sym, g in df.groupby("symbol", sort=False):
        s = sidx[sym]
        h = np.rint((g["funding_ts"].to_numpy(np.int64) - grid.h0) / H).astype(np.int64)
        ok = (h >= 0) & (h < grid.n_hours)
        if not ok.any():
            continue
        h, g = h[ok], g[ok]
        rate = g["rate"].to_numpy(float)
        ih = np.clip(g["interval_hours"].to_numpy(float), 1, 8)
        b = np.ceil(h / BK).astype(np.int64)
        np.add.at(D[s], b, rate)
        # Cubierto entre pagos consecutivos separados ≤ max(3·intervalo, 24 h).
        diff = np.diff(h)
        good = diff <= np.maximum(3 * ih[1:], 24)
        delta = np.zeros(nb + 1, np.int64)
        np.add.at(delta, b[:-1][good], 1)
        np.add.at(delta, b[1:][good] + 1, -1)
        cov[s] = np.cumsum(delta)[:nb] > 0
        k = np.searchsorted(h, bounds, side="right") - 1
        has = k >= 0
        cur[s, has] = rate[k[has]] * 24 / ih[k[has]]
        for c in extra:
            v = g[c].to_numpy(float)
            ext[c][s, has] = v[k[has]]
    return {"syms": syms, "sidx": sidx, "D": D, "cov": cov, "cur": cur, **ext}


def next_true(mask):
    """nt[:, m] = menor m' ≥ m con mask[:, m'] (o nb si no hay)."""
    ns, nb = mask.shape
    nt = np.full((ns, nb + 1), nb, np.int64)
    for m in range(nb - 1, -1, -1):
        nt[:, m] = np.where(mask[:, m], m, nt[:, m + 1])
    return nt


def window_sum(x, w):
    c = np.cumsum(np.nan_to_num(x), axis=1)
    out = np.full(x.shape, np.nan)
    out[:, w - 1:] = c[:, w - 1:] - np.concatenate([np.zeros((x.shape[0], 1)), c[:, :-w]], axis=1)
    return out


def pair_candidates(DP, valid, curd, gate, last_b):
    """Candidatos (s, j) de un par dirigido + label con la regla de salida."""
    ns, nb = DP.shape
    DPz = np.where(valid, DP, 0.0)
    CDP = np.cumsum(DPz, axis=1)
    vc = np.cumsum(valid, axis=1)

    def vwin(w):                                  # válido en toda la ventana [j-w+1, j]
        out = np.zeros_like(valid)
        out[:, w - 1:] = (vc[:, w - 1:] - np.concatenate([np.zeros((ns, 1), np.int64), vc[:, :-w]], axis=1)) == w
        return out

    s3 = np.where(vwin(9), window_sum(DPz, 9) / 3, np.nan)
    s7 = np.where(vwin(21), window_sum(DPz, 21) / 7, np.nan)
    neg = valid & (DP <= 0)
    neg2 = np.zeros_like(neg)
    neg2[:, 1:] = neg[:, 1:] & neg[:, :-1]
    nt_neg2 = next_true(neg2)
    nt_inv = next_true(~valid)
    sig_max = np.fmax(np.fmax(np.nan_to_num(s3, nan=-1), np.nan_to_num(s7, nan=-1)), np.nan_to_num(curd, nan=-1))
    ok = gate & valid & (sig_max >= THRESHOLDS[0])
    ok[:, last_b:] = False
    s, j = np.nonzero(ok)
    if len(s) == 0:
        return None
    jp2 = np.minimum(j + 2, nb)
    e = np.minimum.reduce([nt_neg2[s, jp2], j + MAX_BUCKETS, nt_inv[s, np.minimum(j + 1, nb)] - 1,
                           np.full_like(j, last_b)])
    keep = e >= j + 1
    s, j, e = s[keep], j[keep], e[keep]
    return {"s": s, "j": j, "e": e, "gross": CDP[s, e] - CDP[s, j], "s3": s3[s, j], "s7": s7[s, j],
            "cur": curd[s, j], "trunc": e >= last_b, "CDP": CDP}


# ── Klines (riesgo por pierna) ──────────────────────────────────────────────
def load_klines(grid):
    p = CROSS / "klines_1d.csv"
    if not p.exists():
        return {}, {}
    k = pd.read_csv(p)
    day0 = grid.h0 // (24 * H)
    nd = grid.n_hours // 24 + 2
    out, vol = {}, {}
    for sym, g in k.groupby("symbol"):
        d = (g["day_ts"].to_numpy(np.int64) // (24 * H) - day0)
        ok = (d >= 0) & (d < nd)
        arr = np.full((4, nd), np.nan)
        for r, c in enumerate(("open", "high", "low", "close")):
            arr[r, d[ok]] = g[c].to_numpy(float)[ok]
        v = np.full(nd, np.nan)
        v[d[ok]] = g["quote_volume"].to_numpy(float)[ok]
        out[sym], vol[sym] = arr, v
    return out, vol


def leg_events(K, j, e, L, two_legs, rebal, N):
    """(e_efectivo, evento, costo_rebalanceo_usd, n_rebal, con_precio) para un trade."""
    if K is None:
        return e, False, 0.0, 0, False
    d0, d1 = (j * BK) // 24, (e * BK) // 24
    p_ref = K[0, d0]
    if not np.isfinite(p_ref):
        return e, False, 0.0, 0, False
    dist = 1.0 / L - MMR
    trig = SL_FRAC * dist
    cost, nreb = 0.0, 0
    for d in range(d0, min(d1, K.shape[1] - 1) + 1):
        hi, lo, cl = K[1, d], K[2, d], K[3, d]
        if not np.isfinite(hi):
            continue
        if hi / p_ref - 1 >= trig or (two_legs and 1 - lo / p_ref >= trig):
            e_ev = max(j + 1, min(e, ((d + 1) * 24) // BK - 1))
            return e_ev, True, cost, nreb, True
        if rebal and abs(cl / p_ref - 1) >= REBAL_FRAC * dist:
            cost += REBAL_USD if two_legs else abs(cl / p_ref - 1) * N * SP_REBAL_COST
            nreb += 1
            p_ref = cl
    return e, False, cost, nreb, True


# ── Cartera ─────────────────────────────────────────────────────────────────
def simulate(c, score, thr, L, rebal, kind, klines, cdps, nb, gate=None):
    """Cartera continua sobre todo el periodo. Devuelve acumulados por bucket
    (PnL devengado, por escenario de fee) + lista de trades."""
    m = c[score].to_numpy() >= thr if thr is not None else np.isfinite(c[score].to_numpy())
    if gate is not None:
        m &= gate
    d = c[m]
    d = d.sort_values(["j", score], ascending=[True, False])
    two = kind == "cross"
    N = SLOT * L / 2 if two else SLOT * L / (L + 1)
    acc = {"base": np.zeros(nb + 1), "opt": np.zeros(nb + 1)}
    J = d["j"].to_numpy()
    cols = {k: d[k].to_numpy() for k in ("s", "e", "sym", "pair", "fee_base", "fee_opt", "ksym")}
    open_pos, trades = [], []
    starts = np.flatnonzero(np.r_[True, J[1:] != J[:-1]]) if len(J) else []
    for a, b in zip(starts, list(starts[1:]) + [len(J)]):
        j = J[a]
        open_pos = [p for p in open_pos if p[0] > j]
        free = SLOTS - len(open_pos)
        if free <= 0:
            continue
        held = {p[1] for p in open_pos}
        for i in range(a, b):
            if free <= 0:
                break
            sym = cols["sym"][i]
            if sym in held:
                continue
            e = int(cols["e"][i])
            K = klines.get(cols["ksym"][i])
            e2, ev, rcost, nreb, priced = leg_events(K, j, e, L, two, rebal, N)
            CDP = cdps[cols["pair"][i]]
            s = int(cols["s"][i])
            path = CDP[s, j:e2 + 1]
            inc = np.diff(path) * N
            gross = path[-1] - path[0]
            for fk in ("base", "opt"):
                fee = cols["fee_" + fk][i] * N
                acc[fk][j + 1:e2 + 1] += inc
                acc[fk][j] -= fee / 2 + (ENTRY_USD if two else 0.0)
                acc[fk][e2] -= fee / 2 + rcost + (EVENT_COST * N if ev else 0.0)
            net_base = gross * N - cols["fee_base"][i] * N - rcost - (EVENT_COST * N if ev else 0) \
                - (ENTRY_USD if two else 0)
            trades.append((j, e2, sym, cols["pair"][i], net_base, ev, nreb, priced))
            open_pos.append((e2, sym))
            held.add(sym)
            free -= 1
    tr = pd.DataFrame(trades, columns=["j", "e", "sym", "pair", "net_usd", "event", "nreb", "priced"])
    return acc, tr


def stats(acc, tr, a, b):
    x = acc[a:b]
    pnl = float(x.sum())
    eq = CAPITAL + np.cumsum(x)
    peak = np.maximum.accumulate(np.r_[CAPITAL, eq])[1:]
    dd = float(np.max((peak - eq) / peak)) if len(eq) else 0.0
    days = max(1.0, (b - a) * BK / 24)
    t = tr[(tr["j"] >= a) & (tr["j"] < b)]
    return {"pnl": pnl, "ann": pnl / CAPITAL / days * 365 * 100, "dd": dd * 100, "trades": len(t),
            "win": 100 * float((t["net_usd"] > 0).mean()) if len(t) else 0.0,
            "hold": float(((t["e"] - t["j"]) * BK / 24).mean()) if len(t) else 0.0,
            "events": int(t["event"].sum()), "nreb": int(t["nreb"].sum()),
            "unpriced": int((~t["priced"]).sum())}


# ── Construcción de candidatos ──────────────────────────────────────────────
def spot_perp_candidates(grid, last_b, klines):
    fr = pd.read_csv(BIN_CSV, usecols=["symbol", "rate", "volume_24h", "interval_hours", "funding_ts", "has_spot"])
    fr = fr[fr["funding_ts"] >= grid.h0 - 8 * 86_400_000]
    leg = build_leg(fr, grid, extra=("volume_24h", "has_spot"))
    cex = np.nan_to_num(leg["has_spot"]) > 0
    # Alpha: token en la lista pública de Binance Alpha con listingTime ≤ frontera y sin spot CEX.
    alpha = np.zeros_like(cex)
    if ALPHA.exists():
        import json
        lst = {}
        for t in json.loads(ALPHA.read_text(encoding="utf-8")):
            sy, lt = (t.get("symbol") or "").upper(), int(t.get("listingTime") or 0)
            if sy and (sy not in lst or lt < lst[sy]):
                lst[sy] = lt
        bt = grid.h0 + np.arange(grid.nb) * BK * H
        for sy, k in leg["sidx"].items():
            if sy in lst:
                alpha[k] = (bt >= lst[sy]) & ~cex[k]
    valid = leg["cov"] & (cex | alpha)
    gate = (np.nan_to_num(leg["volume_24h"]) >= MIN_VOLUME) & (np.nan_to_num(leg["cur"], nan=-1) >= BASE_DAILY * (1 - 1e-6))
    pc = pair_candidates(leg["D"], valid, leg["cur"], gate, last_b)
    syms = np.array(leg["syms"], dtype=object)
    c = pd.DataFrame({k: pc[k] for k in ("s", "j", "e", "gross", "s3", "s7", "cur", "trunc")})
    c["sym"] = syms[c["s"]]
    c["ksym"] = [canon(x) for x in c["sym"]]
    c["pair"] = "spot>binance"
    c["alpha"] = alpha[c["s"], c["j"]]
    c["fee_base"] = np.where(c["alpha"], ALPHA_FEE, SP_FEE["base"])
    c["fee_opt"] = np.where(c["alpha"], ALPHA_FEE, SP_FEE["opt"])
    # Rankings de prod (modelo vigente + heurístico): universo CEX ∪ Alpha si existe.
    src = UROWS if UROWS.exists() else ROWS
    if src.exists():
        r = pd.read_pickle(src)[["symbol", "funding_ts", "p_prod", "heur", "net_H30", "ih"]]
        hh = np.rint((r["funding_ts"].to_numpy(np.int64) - grid.h0) / H).astype(np.int64)
        r = r[hh % BK == 0].assign(j=hh[hh % BK == 0] // BK)
        c = c.merge(r.rename(columns={"symbol": "sym"}), on=["sym", "j"], how="left")
    return c, {"spot>binance": pc["CDP"]}, leg


def cross_candidates(grid, last_b, klines, kvol):
    legs = {}
    for v in VENUES:
        p = CROSS / f"fr_{v}.csv"
        if p.exists():
            legs[v] = build_leg(pd.read_csv(p, usecols=["symbol", "rate", "interval_hours", "funding_ts", "has_spot"]),
                                grid, extra=("has_spot",))
    frames, cdps = [], {}
    day_of = (np.arange(grid.nb) * BK) // 24
    for A in legs:
        for B in legs:
            if A == B:
                continue
            common = sorted(set(legs[A]["syms"]) & set(legs[B]["syms"]))
            ia = [legs[A]["sidx"][s] for s in common]
            ib = [legs[B]["sidx"][s] for s in common]
            DP = legs[B]["D"][ib] - legs[A]["D"][ia]
            valid = legs[A]["cov"][ia] & legs[B]["cov"][ib]
            curd = legs[B]["cur"][ib] - legs[A]["cur"][ia]
            # Liquidez: quote volume del día ANTERIOR (klines del venue de referencia).
            gate = np.zeros(valid.shape, bool)
            for r, s in enumerate(common):
                v = kvol.get(s)
                if v is not None:
                    pv = np.r_[np.nan, v][np.minimum(day_of, len(v))]   # día previo
                    gate[r] = np.nan_to_num(pv) >= MIN_VOLUME
            pc = pair_candidates(DP, valid, curd, gate, last_b)
            if pc is None:
                continue
            key = f"{A}>{B}"
            cdps[key] = pc["CDP"]
            f = pd.DataFrame({k: pc[k] for k in ("s", "j", "e", "gross", "s3", "s7", "cur", "trunc")})
            f["sym"] = np.array(common, dtype=object)[f["s"]]
            f["pair"], f["long"], f["short"] = key, A, B
            f["fee_base"] = 2 * (TAKER[A] + TAKER[B]) + 4 * SLIP
            f["fee_opt"] = 2 * (MAKER + MAKER) + 4 * SLIP_MAKER
            frames.append(f)
            print(f"  {key}: {len(common)} símbolos, {len(f):,} candidatos", flush=True)
    c = pd.concat(frames, ignore_index=True)
    c["ksym"] = c["sym"]
    spot = set()
    if "binance" in legs:
        hs = legs["binance"]["has_spot"]
        spot = {s for s, k in legs["binance"]["sidx"].items() if np.nanmax(np.r_[0, hs[k]]) > 0}
    c["has_spot"] = c["sym"].isin(spot)
    c["tradfi"] = c["sym"].str.removesuffix("USDT").isin(TRADFI)
    return c, cdps, legs


# ── Walk-forward ────────────────────────────────────────────────────────────
def configs(kind, c):
    out = []
    for sig in SIGNALS:
        for thr in THRESHOLDS:
            out.append({"rank": sig, "thr": thr, "gate": None, "name": f"{sig}≥{thr * 100:.2f}%"})
    if kind == "spot_perp" and "p_prod" in c:
        out.append({"rank": "p_prod", "thr": None, "gate": (c["p_prod"] > 0).to_numpy(), "name": "p_prod>0"})
        out.append({"rank": "heur", "thr": None, "gate": None, "name": "heur"})
    full = []
    for cf in out:
        for L in LEVERAGES:
            for rb in (False, True):
                full.append({**cf, "L": L, "rebal": rb,
                             "label": f"{cf['name']} L{L}{' reb' if rb else ''}"})
    return full


def run_family(kind, c, cdps, klines, grid, folds, ho, warm_b):
    res = []
    for cf in configs(kind, c):
        t0 = time.time()
        acc, tr = simulate(c, cf["rank"], cf["thr"], cf["L"], cf["rebal"], kind, klines, cdps, grid.nb,
                           gate=cf["gate"])
        avail = c.loc[np.isfinite(c[cf["rank"]].to_numpy(float)), "j"].min()
        res.append({**cf, "acc": acc, "tr": tr, "avail_b": int(avail) if np.isfinite(avail) else grid.nb})
        print(f"  {kind} {cf['label']}: {len(tr)} trades ({time.time() - t0:.0f}s)", flush=True)
    # Selección walk-forward: con datos ANTERIORES al mes, DD ≤ 5 %, máx PnL.
    wf = []
    for fs, fe in folds:
        best = None
        for r in res:
            if r["avail_b"] > fs - 28 * 24 // BK:
                continue
            a = max(warm_b, r["avail_b"])
            st = stats(r["acc"]["base"], r["tr"], a, fs)
            if st["dd"] <= MAX_DD * 100 and (best is None or st["ann"] > best[1]["ann"]):
                best = (r, st)
        if best is None:
            wf.append((fs, fe, None, None, None))
            continue
        r = best[0]
        wf.append((fs, fe, r["label"], stats(r["acc"]["base"], r["tr"], fs, fe),
                   stats(r["acc"]["opt"], r["tr"], fs, fe)))
    # Hold-out: config elegida con TODO lo anterior al hold-out.
    best = None
    for r in res:
        a = max(warm_b, r["avail_b"])
        st = stats(r["acc"]["base"], r["tr"], a, ho[0])
        if st["dd"] <= MAX_DD * 100 and (best is None or st["ann"] > best[1]["ann"]):
            best = (r, st)
    hold = None
    if best is not None:
        r = best[0]
        hold = (r["label"], best[1], stats(r["acc"]["base"], r["tr"], *ho), stats(r["acc"]["opt"], r["tr"], *ho))
    return res, wf, hold


# ── Reporte ─────────────────────────────────────────────────────────────────
def f1(x):
    return f"{x:,.1f}"


def wf_table(name, wf, grid):
    L = [f"### {name}", "", "| mes | config elegida (datos previos) | PnL base USD | anual % | DD % | trades | "
         "eventos SL | PnL opt USD |", "| --- | --- | --- | --- | --- | --- | --- | --- |"]
    tot_b = tot_o = 0.0
    for fs, fe, lab, sb, so in wf:
        if lab is None:
            L.append(f"| {grid.time(fs):%Y-%m} | — (ninguna con DD ≤ 5 %) | 0 | 0 | 0 | 0 | 0 | 0 |")
            continue
        tot_b += sb["pnl"]
        tot_o += so["pnl"]
        L.append(f"| {grid.time(fs):%Y-%m} | {lab} | {f1(sb['pnl'])} | {f1(sb['ann'])} | {f1(sb['dd'])} | {sb['trades']} | "
                 f"{sb['events']} | {f1(so['pnl'])} |")
    return L, tot_b, tot_o


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--start", default="2025-01-01")
    a = ap.parse_args()
    h0 = int(pd.Timestamp(a.start, tz="UTC").value // 1_000_000)
    b_end = pd.read_csv(BIN_CSV, usecols=["funding_ts"])["funding_ts"].max()
    c_end = min(pd.read_csv(CROSS / f"fr_{v}.csv", usecols=["funding_ts"])["funding_ts"].max()
                for v in ("binance", "bybit", "okx"))
    t_end = min(b_end, c_end)                  # mismo final para las dos familias
    grid = Grid(h0, t_end)
    last_b = int((t_end - h0) // (BK * H))
    t_end_ts = grid.time(last_b)
    ho_start = (t_end_ts - timedelta(days=HOLDOUT_DAYS)).floor("D")
    ms = pd.date_range(end=ho_start, periods=WF_MONTHS + 1, freq="MS", tz="UTC")
    folds_ts = [(ms[k], ms[k + 1]) for k in range(WF_MONTHS)]
    folds_ts[-1] = (folds_ts[-1][0], ho_start)
    folds = [(grid.bucket(x), grid.bucket(y)) for x, y in folds_ts]
    ho = (grid.bucket(ho_start), last_b + 1)
    warm_b = 21 + 3                            # ventana s7 + margen
    print(f"rejilla {grid.time(0)} → {t_end_ts} ({grid.nb} buckets); hold-out desde {ho_start}", flush=True)

    klines, kvol = load_klines(grid)
    print(f"klines: {len(klines)} símbolos", flush=True)
    print("candidatos spot_perp…", flush=True)
    sp, sp_cdp, sp_leg = spot_perp_candidates(grid, last_b, klines)
    print(f"  spot_perp: {len(sp):,} candidatos", flush=True)
    print("candidatos cross…", flush=True)
    cx, cx_cdp, legs = cross_candidates(grid, last_b, klines, kvol)
    print(f"  cross: {len(cx):,} candidatos", flush=True)

    # Validación del motor contra exit_policy_study (H30, pares 8 h).
    val = ""
    if "net_H30" in sp:
        v = sp[(sp["ih"] == 8) & sp["net_H30"].notna() & ~sp["trunc"]]
        if len(v):
            diff = (v["gross"] - SP_FEE["base"]) - v["net_H30"]
            val = (f"Motor vs `exit_policy_study` (H30, pares de 8 h, {len(v):,} entradas comunes): "
                   f"diferencia media {diff.mean() * 100:+.4f} pp, mediana |Δ| {diff.abs().median() * 100:.4f} pp, "
                   f"{100 * (diff.abs() < 1e-6).mean():.1f} % idénticas.")

    fams = [("spot_perp CEX", "spot_perp", sp[~sp["alpha"]].reset_index(drop=True), sp_cdp),
            ("spot_perp CEX ∪ Alpha", "spot_perp", sp, sp_cdp),
            ("cross", "cross", cx, cx_cdp)]
    out = {name: run_family(kind, c, cdp, klines, grid, folds, ho, warm_b) for name, kind, c, cdp in fams}
    cx_res = out["cross"][0]

    wf_a, wf_b = folds[0][0], folds[-1][1]
    wf_days = (wf_b - wf_a) * BK / 24
    lines = [f"# Cross perp vs spot_perp Binance — {datetime.now(timezone.utc):%Y-%m-%d}", "",
             "Fase 6 del plan de rentabilidad: decidir con datos si reactivar `cross_exchange` "
             "(Binance/Bybit/OKX/Bitget) a igual riesgo (leverage ≤ 3x por pierna, DD ≤ 5 %). "
             "Solo investigación: no toca producción. Scripts: `scripts/cross_dataset.py`, "
             "`scripts/cross_backtest.py`.", "",
             "## 1. Datos", ""]
    for v, lg in legs.items():
        cnt = int(lg["cov"].any(axis=1).sum())
        first = grid.time(int(np.argmax(lg["cov"].any(axis=0))))
        lines.append(f"- **{v}**: {cnt} perps USDT del universo común (≥ 2 venues) con datos; desde {first:%Y-%m-%d}.")
    lines += [f"- Spot_perp Binance: {len(sp_leg['syms'])} perps (dataset con deslistados); pierna spot en CEX o, "
              f"si no hay spot CEX, en Binance Alpha desde su `listingTime` ({int(sp['alpha'].sum()):,} candidatos "
              f"Alpha, fee {ALPHA_FEE * 100:.2f} %).",
              f"- Rejilla {grid.time(0):%Y-%m-%d} → {t_end_ts:%Y-%m-%d %H:%M} UTC. Walk-forward {WF_MONTHS} meses "
              f"{grid.time(wf_a):%Y-%m-%d} → {grid.time(wf_b):%Y-%m-%d}; **hold-out intocado** "
              f"{ho_start:%Y-%m-%d} → {t_end_ts:%Y-%m-%d}.",
              f"- Candidatos (frontera de 8 h con alguna señal ≥ {THRESHOLDS[0] * 100:.2f} %/día y liquidez "
              f"≥ {MIN_VOLUME:,} USD): spot_perp CEX ∪ Alpha {len(sp):,}, cross {len(cx):,} "
              f"({cx['pair'].nunique()} pares dirigidos de venues).",
              f"- Klines 1d para riesgo por pierna: {len(klines)} símbolos.",
              "- Limitaciones: Bitget publica solo ~90 d de funding por API (sin archivo) → entra al final "
              "del periodo (ver §6). Bybit/OKX/Bitget solo incluyen perps listados hoy (sesgo de supervivencia "
              "leve); Binance incluye deslistados. Precio de ambas piernas = klines del venue de referencia "
              "(sin basis entre venues).", ""]
    if val:
        lines += [val, ""]
    lines += ["## 2. Supuestos", "",
              f"- Cartera: {SLOTS} slots, {CAPITAL:,.0f} USD, decisión cada 8 h, un símbolo una vez.",
              "- Nocional por slot: spot_perp N = slot·L/(L+1); cross N = slot·L/2 por pierna (ambas piernas con margen).",
              f"- Fees round-trip sobre N: spot_perp base {SP_FEE['base'] * 100:.2f} % / optimista "
              f"{SP_FEE['opt'] * 100:.2f} % (realizado); cross base = taker de ambos venues ×2 + slippage "
              f"{SLIP * 100:.2f} %/fill (Binance–Bybit ≈ {(2 * (TAKER['binance'] + TAKER['bybit']) + 4 * SLIP) * 100:.2f} %), "
              f"optimista = maker {MAKER * 100:.2f} % + {SLIP_MAKER * 100:.2f} %/fill "
              f"(≈ {(4 * MAKER + 4 * SLIP_MAKER) * 100:.2f} %).",
              f"- Cross: {ENTRY_USD:.0f} USD por trade para pre-posicionar capital; rebalanceo {REBAL_USD:.0f} USD por transferencia.",
              f"- Seguro por pierna al {SL_FRAC:.0%} de la distancia a liquidación (1/L − {MMR:.0%}); evento = cierre "
              f"anticipado + {EVENT_COST * 100:.1f} % de N. Rebalanceo diario (variante `reb`) re-centra si el "
              f"movimiento supera el {REBAL_FRAC:.0%} de la distancia.",
              "- Salida: dos buckets de 8 h seguidos con Δf ≤ 0, tope 30 d; señales: `s3`/`s7` = Δf diario medio "
              "de 3/7 d, `cur` = Δ de la última tasa liquidada (diaria); spot_perp además `p_prod>0` (modelo "
              "vigente) y `heur` (v11.0), disponibles desde el inicio del walk-forward.", ""]

    # 3. Rejilla completa.
    lines += ["## 3. Rejilla completa (walk-forward 12 m y hold-out, fees base)", "",
              "Cifras in-sample de selección: sirven para ver la forma, no para elegir (ver §4).", ""]
    for kind, (res, _, _) in out.items():
        rows = []
        for r in res:
            a0 = max(wf_a, r["avail_b"])
            sw = stats(r["acc"]["base"], r["tr"], a0, wf_b)
            sh = stats(r["acc"]["base"], r["tr"], *ho)
            so = stats(r["acc"]["opt"], r["tr"], a0, wf_b)
            rows.append((r["label"], sw, sh, so))
        rows.sort(key=lambda x: -x[1]["ann"])
        lines += [f"### {kind} (top 12 por anual WF + mejor DD-factible por leverage)", "",
                  "| config | WF anual % | WF DD % | WF trades | % gan. | hold d | eventos | rebal | "
                  "WF anual % (opt) | hold-out PnL USD | hold-out anual % | hold-out DD % |",
                  "| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |"]
        shown = set()
        pick = rows[:12]
        for L in LEVERAGES:
            feas = [x for x in rows if x[0].split(" L")[1].startswith(str(L)) and x[1]["dd"] <= MAX_DD * 100]
            if feas:
                pick.append(feas[0])
        for lab, sw, sh, so in pick:
            if lab in shown:
                continue
            shown.add(lab)
            lines.append(f"| {lab} | {f1(sw['ann'])} | {f1(sw['dd'])} | {sw['trades']} | {f1(sw['win'])} | "
                         f"{f1(sw['hold'])} | {sw['events']} | {sw['nreb']} | {f1(so['ann'])} | {f1(sh['pnl'])} | "
                         f"{f1(sh['ann'])} | {f1(sh['dd'])} |")
        lines.append("")

    # 4. Walk-forward OOS.
    lines += ["## 4. Walk-forward OOS (config elegida cada mes con datos previos, DD ≤ 5 %)", ""]
    summ = []
    for name, (_, wf, _) in out.items():
        t, pb, po = wf_table(name, wf, grid)
        lines += t + [""]
        summ.append(f"{name} {f1(pb)} USD ({f1(pb / CAPITAL / wf_days * 365 * 100)} % anual; opt {f1(po)} USD)")
    lines += ["**OOS 12 m (suma de los meses):** " + " · ".join(summ) + ".", ""]
    lines += ["## 5. Hold-out intocado (30 d)", "",
              "| familia | config (elegida con todo lo anterior) | pre-hold-out anual % | pre DD % | hold-out PnL base | "
              "anual % | DD % | trades | PnL opt |", "| --- | --- | --- | --- | --- | --- | --- | --- | --- |"]
    for kind, (_, _, hold) in out.items():
        if hold is None:
            lines.append(f"| {kind} | — | | | | | | | |")
            continue
        lab, pre, hb, hopt = hold
        lines.append(f"| {kind} | {lab} | {f1(pre['ann'])} | {f1(pre['dd'])} | {f1(hb['pnl'])} | {f1(hb['ann'])} | "
                     f"{f1(hb['dd'])} | {hb['trades']} | {f1(hopt['pnl'])} |")
    lines.append("")

    # 6. Desgloses cross: par de venues, sin spot / TradFi, Bitget.
    best_cx = max(cx_res, key=lambda r: stats(r["acc"]["base"], r["tr"], max(wf_a, r["avail_b"]), ho[1])["ann"]
                  if stats(r["acc"]["base"], r["tr"], max(wf_a, r["avail_b"]), ho[1])["dd"] <= MAX_DD * 100 else -1e9)
    tr = best_cx["tr"]
    tr = tr[tr["j"] >= wf_a]
    lines += [f"## 6. Desglose cross (config `{best_cx['label']}`, WF + hold-out)", "",
              "| par (long>short) | trades | PnL USD | % ganadoras |", "| --- | --- | --- | --- |"]
    for p, g in tr.groupby("pair"):
        lines.append(f"| {p} | {len(g)} | {f1(g['net_usd'].sum())} | {f1(100 * (g['net_usd'] > 0).mean())} |")
    spot_syms = set(cx.loc[cx["has_spot"], "sym"])
    trad_syms = set(cx.loc[cx["tradfi"], "sym"])
    ns = tr[~tr["sym"].isin(spot_syms)]
    lines += ["", f"- Trades en perps **sin spot en Binance** (no replicables con spot_perp): {len(ns)} de {len(tr)}, "
                  f"PnL {f1(ns['net_usd'].sum())} USD.",
              f"- Perps TradFi (acciones/índices/metales) en el universo común: "
              f"{', '.join(sorted(s.removesuffix('USDT') for s in trad_syms)) or 'ninguno'}; trades: "
              f"{int(tr['sym'].isin(trad_syms).sum())}, PnL {f1(tr.loc[tr['sym'].isin(trad_syms), 'net_usd'].sum())} USD."]
    top = tr.groupby("sym")["net_usd"].agg(["count", "sum"]).sort_values("sum")
    lines += [f"- Mejores símbolos: " + ", ".join(f"{s} {f1(v)}" for s, v in top["sum"].tail(5)[::-1].items()),
              f"- Peores símbolos: " + ", ".join(f"{s} {f1(v)}" for s, v in top["sum"].head(5).items()), ""]
    # Dependencia de outliers: misma config sin los símbolos de mayor PnL en WF.
    pre_b = warm_b
    lines += [f"### Dependencia de outliers (config `{best_cx['label']}` y su par L2)", "",
              "| config | universo | pre-WF anual % | pre-WF DD % | WF anual % | WF DD % | hold-out anual % | "
              "WF anual % (opt) |", "| --- | --- | --- | --- | --- | --- | --- | --- |"]
    for L in sorted({best_cx["L"], 2}):
        _, trf = simulate(cx, best_cx["rank"], best_cx["thr"], L, best_cx["rebal"], "cross", klines, cx_cdp, grid.nb)
        wsum = trf[(trf["j"] >= wf_a) & (trf["j"] < wf_b)].groupby("sym")["net_usd"].sum()
        for lab, drop in (("todo", []), ("sin top-3 símbolos", list(wsum.nlargest(3).index)),
                          ("sin top-10 símbolos", list(wsum.nlargest(10).index))):
            m = ~cx["sym"].isin(drop).to_numpy()
            acc, t2 = simulate(cx[m], best_cx["rank"], best_cx["thr"], L, best_cx["rebal"], "cross", klines, cx_cdp,
                               grid.nb)
            sp_ = stats(acc["base"], t2, pre_b, wf_a)
            sw = stats(acc["base"], t2, wf_a, wf_b)
            sh = stats(acc["base"], t2, *ho)
            so = stats(acc["opt"], t2, wf_a, wf_b)
            name = lab + (f" ({', '.join(d.removesuffix('USDT') for d in drop)})" if 0 < len(drop) <= 3 else "")
            lines.append(f"| {best_cx['rank']} L{L}{' reb' if best_cx['rebal'] else ''} | {name} | {f1(sp_['ann'])} | "
                         f"{f1(sp_['dd'])} | {f1(sw['ann'])} | {f1(sw['dd'])} | {f1(sh['ann'])} | {f1(so['ann'])} |")
    lines.append("")
    # Bitget: ventana donde hay datos de los 4 venues, misma config con y sin Bitget.
    if "bitget" in legs:
        bg0 = int(np.argmax(legs["bitget"]["cov"].any(axis=0))) + 24
        rows_bg = []
        for lab, mask in (("con Bitget", np.ones(len(cx), bool)),
                          ("sin Bitget", ~(cx["long"].eq("bitget") | cx["short"].eq("bitget")).to_numpy())):
            acc, tr2 = simulate(cx[mask], best_cx["rank"], best_cx["thr"], best_cx["L"], best_cx["rebal"], "cross",
                                klines, cx_cdp, grid.nb, gate=None)
            rows_bg.append((lab, stats(acc["base"], tr2, bg0, ho[1]), tr2))
        lines += [f"### Bitget (ventana {grid.time(bg0):%Y-%m-%d} → {t_end_ts:%Y-%m-%d}, config `{best_cx['label']}`)", "",
                  "| variante | PnL USD | anual % | DD % | trades | trades con Bitget |", "| --- | --- | --- | --- | --- | --- |"]
        for lab, st, tr2 in rows_bg:
            t3 = tr2[tr2["j"] >= bg0]
            nbg = int(t3["pair"].str.contains("bitget").sum())
            lines.append(f"| {lab} | {f1(st['pnl'])} | {f1(st['ann'])} | {f1(st['dd'])} | {st['trades']} | {nbg} |")
        lines.append("")

    lines += ["## 7. Veredicto", "", "_(redactado a mano tras revisar las cifras; el script no lo genera)_", ""]
    path = REPORT_DIR / f"cross_vs_spotperp_{datetime.now(timezone.utc):%Y%m%d}.md"
    path.write_text("\n".join(lines), encoding="utf-8")
    print("\n".join(lines))


if __name__ == "__main__":
    main()
