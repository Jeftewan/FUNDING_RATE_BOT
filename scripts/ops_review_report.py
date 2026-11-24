#!/usr/bin/env python3
"""Análisis de scripts/ops_review.py (solo lee cache/ops_review/*.pkl) → reports/ops_review_YYYYMMDD.md.

Descomposición del PnL de precio de un trade (qs, qp = cantidades spot/perp; S, P = precios de ejecución):
    precio = qs·(S₁ − S₀) + qp·(P₀ − P₁)
           = qs·(P₀ − S₀)  −  qs·(P₁ − S₁)  +  (qs − qp)·(P₁ − P₀)
             basis entrada    basis salida     delta residual (0 si cantidad igual)
"""
import collections
import json
import time
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd
import requests

from ops_review import CACHE, DAY, MANUAL_WALLETS, STABLES, _load, _load_env, _ms, _save, manual_events

ROOT = Path(__file__).resolve().parent.parent
REPORT = ROOT / "reports" / f"ops_review_{datetime.now():%Y%m%d}.md"
FAPI_KL = "https://fapi.binance.com/fapi/v1/klines"
FAPI_FR = "https://fapi.binance.com/fapi/v1/fundingRate"


def _j(v):
    if isinstance(v, str) and v:
        try:
            return json.loads(v)
        except ValueError:
            return {}
    return v if isinstance(v, dict) else {}


def _ts(s):
    return datetime.fromtimestamp(s, timezone.utc).strftime("%Y-%m-%d %H:%M")


# ───────────────────────── fórmulas ─────────────────────────
def decompose(qs, qp, S0, P0, S1, P1):
    """(entrada, salida, delta) en USD; suman qs·(S₁−S₀) + qp·(P₀−P₁)."""
    return qs * (P0 - S0), -qs * (P1 - S1), (qs - qp) * (P1 - P0)


def hedge_pnl(X, S0, P0, S1, P1, mode):
    """PnL de precio (USD) de X USD de spot cubierto con 'usd' (mismo USDT) o 'qty' (misma cantidad)."""
    qs = X / S0
    qp = X / P0 if mode == "usd" else qs
    return qs * (S1 - S0) + qp * (P0 - P1)


# ───────────────────────── trades manuales ─────────────────────────
def _swaps(win, sym, meta, blocks):
    """Swaps token↔stable de la wallet en la ventana: [(ts, token_qty firmado, stable firmado)]."""
    addr = win["addr"]
    txs = collections.defaultdict(lambda: collections.defaultdict(float))
    tts = {}
    for lg in win["logs"]:
        tok = lg["address"].lower()
        m = meta.get(tok, {"symbol": "?", "decimals": 18})
        amt = int(lg["data"], 16) / 10 ** m["decimals"] if lg["data"] not in ("0x", "") else 0.0
        sign = -1 if "0x" + lg["topics"][1][-40:] == addr else 1
        k = "STABLE" if tok in STABLES else m["symbol"].upper()
        txs[lg["transactionHash"]][k] += sign * amt
        tts[lg["transactionHash"]] = blocks[int(lg["blockNumber"], 16)]
    out = []
    for h, net in txs.items():
        if abs(net.get(sym.upper(), 0)) > 0 and abs(net.get("STABLE", 0)) > 1:
            out.append((tts[h], net[sym.upper()], net["STABLE"], h))
    return out


def manual_trades():
    db, bn, ch = _load("db"), _load("binance"), _load("chain")
    pos = db["positions"].set_index("id")
    rows = []
    evs = {(e[1], e[3]): e for e in manual_events(db, bn)}
    for pid in sorted({k[0] for k in evs}):
        if (pid, "SELL") not in evs or (pid, "BUY") not in evs:
            continue
        uid, _, sym, _, t_sell, q_sell, usd_sell = evs[(pid, "SELL")]
        _, _, _, _, t_buy, q_buy, usd_buy = evs[(pid, "BUY")]
        if t_buy < t_sell:          # no es short perp + spot largo (p. ej. ZIL #37)
            continue
        w0, w1 = ch["windows"].get((pid, "SELL")), ch["windows"].get((pid, "BUY"))
        if not w0 or not w1:
            continue
        buys = [s for s in _swaps(w0, sym, ch["meta"], ch["blocks"]) if s[1] > 0 and s[2] < 0]
        sells = [s for s in _swaps(w1, sym, ch["meta"], ch["blocks"]) if s[1] < 0 and s[2] > 0]
        if not buys or not sells:
            rows.append({"pid": pid, "uid": uid, "sym": sym, "ok": False})
            continue
        qs = sum(s[1] for s in buys)
        cost = -sum(s[2] for s in buys)
        qs_out = -sum(s[1] for s in sells)
        proceeds = sum(s[2] for s in sells)
        S0, S1 = cost / qs, proceeds / qs_out
        P0, P1 = usd_sell / q_sell, usd_buy / q_buy
        tr = bn[uid]["trades"].get(sym, [])
        fills = [x for x in tr if t_sell - 60_000 <= int(x["time"]) <= t_buy + 60_000]
        perp_fee = sum(float(x["commission"]) for x in fills if x.get("commissionAsset") == "USDT")
        inc = [r for r in bn[uid]["income"] if r.get("symbol") == f"{sym}USDT" and
               t_sell <= int(r["time"]) <= t_buy + 60_000]
        funding = sum(float(r["income"]) for r in inc if r["incomeType"] == "FUNDING_FEE")
        price = qs_out * S1 - qs * S0 + q_sell * P0 - q_buy * P1   # proceeds − cost + perp
        e, x, d = decompose(qs, q_sell, S0, P0, S1, P1)
        p = pos.loc[pid]
        rows.append({
            "pid": pid, "uid": uid, "sym": sym, "ok": True, "src": "manual",
            "t0": t_sell / 1000, "t1": t_buy / 1000, "hours": (t_buy - t_sell) / 3.6e6,
            "spot_lag_in_min": (np.mean([s[0] for s in buys]) - t_sell / 1000) / 60,
            "spot_lag_out_min": (np.mean([s[0] for s in sells]) - t_buy / 1000) / 60,
            "X": cost, "perp_usd": usd_sell, "qs": qs, "qp": q_sell, "S0": S0, "P0": P0, "S1": S1, "P1": P1,
            "b0": (P0 / S0 - 1) * 100, "b1": (P1 / S1 - 1) * 100, "ret": (P1 / P0 - 1) * 100,
            "price": price, "E": e, "Xb": x, "D": d, "resid": price - (e + x + d),
            "funding": funding, "funding_db": float(p.earned_real or 0), "fees": perp_fee,
            "lev": float(p.leverage or 1), "capital": float(p.capital_used or 0),
        })
    return pd.DataFrame(rows)


# ───────────────────────── trades auto ─────────────────────────
def auto_trades():
    db = _load("db")
    pos, hist = db["positions"].set_index("id"), db["history"]
    jobs = db["jobs"]
    rows = []
    for _, h in hist.iterrows():
        e = _j(h.exec_json)
        if not e or h.reason == "manual":
            continue
        pid = h.position_id
        if pd.isna(pid):   # filas viejas: símbolo + closed_at ±10 min
            cand = pos[(pos.user_id == h.user_id) & (pos.symbol == h.symbol) & (pos.source == "auto")]
            cand = cand[cand.closed_at.notna()]
            if cand.empty:
                continue
            dt_ = (pd.to_datetime(cand.closed_at) - pd.Timestamp(h.closed_at)).abs()
            pid = dt_.idxmin()
        pid = int(pid)
        p = pos.loc[pid]
        S0 = e.get("spot_entry") or p.spot_entry_price
        P0 = e.get("perp_entry") or p.entry_price
        S1 = e.get("spot_exit") or e.get("spot_exit_price")
        P1 = e.get("perp_exit") or e.get("perp_exit_price")
        qs, qp = float(p.spot_qty or 0), float(p.perp_qty or 0)
        if not (S0 and P0 and S1 and P1 and qs):
            continue
        ed, xd, dd = decompose(qs, qp, S0, P0, S1, P1)
        price = float(h.price_pnl) if pd.notna(h.price_pnl) else np.nan
        jo = jobs[(jobs.position_id == pid) & (jobs.kind == "open") & (jobs.status == "done")]
        rows.append({
            "pid": pid, "uid": int(h.user_id), "sym": h.symbol, "ok": True, "src": "auto",
            "t0": int(p.entry_time) / 1000, "t1": pd.Timestamp(h.closed_at).timestamp(),
            "hours": float(h.hours or 0), "X": qs * S0, "perp_usd": qp * P0, "qs": qs, "qp": qp,
            "S0": S0, "P0": P0, "S1": S1, "P1": P1,
            "b0": (P0 / S0 - 1) * 100, "b1": (P1 / S1 - 1) * 100, "ret": (P1 / P0 - 1) * 100,
            "price": price, "E": ed, "Xb": xd, "D": dd, "resid": price - (ed + xd + dd),
            "funding": float(h.earned or 0), "funding_db": float(h.earned or 0), "fees": float(h.fees or 0),
            "lev": float(h.leverage or 1), "capital": float(h.capital_used or 0), "reason": h.reason,
            "venue": p.spot_venue, "job_open": int(jo.id.iloc[0]) if len(jo) else None,
        })
    return pd.DataFrame(rows)


def add_counterfactual(df):
    """Mismo trade con la otra regla de cobertura (mismos precios). Manual→cantidad igual, auto→USDT igual.
    Funding escala con la cantidad del short."""
    alt_price, alt_fund = [], []
    for _, r in df.iterrows():
        if r.src == "manual":
            qp2 = r.qs
        else:
            qp2 = r.X / r.P0
        _, _, d2 = decompose(r.qs, qp2, r.S0, r.P0, r.S1, r.P1)
        alt_price.append(r.price - r.D + d2)
        alt_fund.append(r.funding * qp2 / r.qp if r.qp else r.funding)
    df["alt_price"], df["alt_funding"] = alt_price, alt_fund
    df["net"] = df.funding + df.price - df.fees
    df["alt_net"] = df.alt_funding + df.alt_price - df.fees
    return df


# ───────────────────────── Q1 sistemático (filas de profit_study) ─────────────────────────
def perp_hourly(sym, t0, t1):
    """Cierres 1h del perp (ms → close), cacheados en cache/ops_review/kl1h_<sym>.pkl."""
    path = CACHE / f"kl1h_{sym}.pkl"
    if path.exists():
        s = pd.read_pickle(path)
        if s.index.min() <= t0 and s.index.max() >= t1 - 3_600_000:
            return s
    rows, t = [], t0 - 3_600_000
    while t < t1:
        r = requests.get(FAPI_KL, params={"symbol": f"{sym}USDT", "interval": "1h", "startTime": t,
                                          "limit": 1500}, timeout=30).json()
        if not isinstance(r, list) or not r:
            break
        rows += r
        t = int(r[-1][0]) + 3_600_000
        time.sleep(0.15)
    s = pd.Series({int(k[0]): float(k[4]) for k in rows}).sort_index()
    s.to_pickle(path)
    return s


def q1_systematic(vi=0, usd=1000.0):
    """qty − usd (% del nocional) = −b₀·(r − 1) + b₀·funding/100 sobre las operaciones del motor (P0)."""
    import profit_study as ps
    df = pd.read_pickle(ROOT / "cache" / "profit_study.pkl")
    df = df[(df.vi == vi) & (df.usd == usd) & (df.frac > 0)].copy()
    r = np.full(len(df), np.nan)
    for sym, g in df.groupby("sym"):
        s = perp_hourly(sym, int(g.in_ms.min()), int(g.out_ms.max()))
        if s.empty:
            continue
        idx = s.index.to_numpy()
        p_in = s.to_numpy()[np.clip(np.searchsorted(idx, g.in_ms.to_numpy()) - 1, 0, len(idx) - 1)]
        p_out = s.to_numpy()[np.clip(np.searchsorted(idx, g.out_ms.to_numpy()) - 1, 0, len(idx) - 1)]
        r[df.index.get_indexer(g.index)] = p_out / p_in
    df["r"] = r
    df = df[df.r.notna()]
    b0 = df.entry / df.frac.where(df.frac > 0, 1) / 100          # basis de entrada por unidad
    df["d_qty_usd"] = (-b0 * (df.r - 1) + b0 * df.fund / 100) * 100 * df.frac
    j = pd.DataFrame({"sym": df.sym, "t_ms": df.t_ms, "net_b": df.net + df.d_qty_usd, "net_c": df.net})
    g = ps.gate(j)
    corr = float(np.corrcoef(b0, df.r - 1)[0, 1])
    return {"n": len(df), "gate_usd_vs_qty": g, "mean_abs_diff": float(df.d_qty_usd.abs().mean()),
            "corr_b0_ret": corr, "mean_b0": float(b0.mean() * 100), "mean_ret": float((df.r - 1).mean() * 100)}


# ───────────────────────── espera de basis (jobs) y series horarias ─────────────────────────
def jobs_table():
    db, bn = _load("db"), _load("binance")
    j = db["jobs"]
    j = j[(j.created_at >= "2026-11-01") & (j.status.isin(["done", "armed", "cancelled", "expired"]))].sort_values("id")
    rows = []
    for _, r in j.iterrows():
        p, res = _j(r.params_json), _j(r.result_json)
        w, fr = p.get("wait") or {}, p.get("fire_read") or {}
        plan = res.get("slices_plan") or (res.get("exec") or {}).get("slices_plan") or {}
        c = r.created_at.timestamp()
        if r.kind == "open":
            real = res.get("basis_pct")
        else:
            eb = (res.get("exec") or {}).get("exit_basis_pct")
            real = -eb if eb is not None else None          # misma convención que la lectura: + = ganancia
        wait_fund = None
        if r.kind == "close" and r.user_id in bn:
            t_a, t_b = c * 1000, r.updated_at.timestamp() * 1000
            wait_fund = sum(float(x["income"]) for x in bn[r.user_id]["income"]
                            if x["incomeType"] == "FUNDING_FEE" and x.get("symbol") == f"{p.get('symbol')}USDT"
                            and t_a <= int(x["time"]) <= t_b)
        rows.append({"job": int(r.id), "uid": int(r.user_id), "kind": r.kind, "status": r.status,
                     "wait_funding": wait_fund,
                     "sym": p.get("symbol"), "lev": p.get("leverage"), "waited": bool(w),
                     "theta": w.get("theta"), "wait_h": (p["fired_at"] - c) / 3600 if p.get("fired_at") else
                     (r.updated_at.timestamp() - c) / 3600, "total_h": (r.updated_at.timestamp() - c) / 3600,
                     "fire": fr.get("basis_pct"), "first": plan.get("first_basis_pct"), "real": real,
                     "slices": plan.get("slices"),
                     # desde 2026-11-24: lectura al pedir y máxima de la espera (params al disparar)
                     "at_request": (p.get("first_read") or {}).get("basis_pct"),
                     "wait_max": max((x["basis_pct"] for x in p.get("wait_reads") or []
                                      if x.get("basis_pct") is not None), default=None)})
    return pd.DataFrame(rows)


def hourly_stats():
    d = _load("db")["mfs_alpha"].dropna(subset=["basis_pct"]).copy()
    d = d.sort_values(["symbol", "ts_hour"])
    d["b"] = d.basis_pct.astype(float)
    d = d[d.b.abs() < 10]
    d["b_next"] = d.groupby("symbol").b.shift(-1)
    d["gap"] = d.groupby("symbol").ts_hour.shift(-1) - d.ts_hour
    x = d[d.gap == 3600]
    rev = []
    for th in (0.25, 0.5):
        s = x[x.b >= th]
        rev.append({"theta": th, "n": len(s), "frac": len(s) / len(x), "b_h": s.b.mean(),
                    "b_next": s.b_next.mean(), "p_stay": (s.b_next >= th).mean()})
    d["mins"] = (d.next_funding_ts / 1000 - d.ts_hour) / 60
    d["dm"] = d.b - d.groupby(["symbol", d.ts_hour // 86400]).b.transform("mean")
    d["bucket"] = pd.cut(d.mins, [0, 60, 120, 240], labels=["0–60", "60–120", "120–240"])
    clock = d.groupby("bucket", observed=True).dm.agg(["mean", "count"])
    wait = []
    for sym, g in d.groupby("symbol"):
        idx, v = g.ts_hour.to_numpy(), g.b.to_numpy()
        for i in range(len(v)):
            jj = np.searchsorted(idx, idx[i] + 6 * 3600, side="right")
            w = v[i:jj]
            if len(w) < 2:
                continue
            hit = np.where(w >= 0.25)[0]
            k = hit[0] if len(hit) else len(w) - 1
            nxt = v[i + k + 1] if i + k + 1 < len(v) else np.nan
            wait.append((v[i], w[k], nxt, k))
    wt = pd.DataFrame(wait, columns=["now", "rule", "rule_next_h", "wait_h"])
    return {"rows": len(d), "syms": d.symbol.nunique(), "from": d.ts_hour.min(), "to": d.ts_hour.max(),
            "median": d.b.median(), "rev": pd.DataFrame(rev), "clock": clock,
            "wait": {"now": wt.now.mean(), "rule": wt.rule.mean(), "rule_next_h": wt.rule_next_h.mean(),
                     "wait_h": wt.wait_h.mean()}}


# ───────────────────────── puente de rentabilidad por usuario ─────────────────────────
def bridge(uid, t_from=None):
    db, bn = _load("db"), _load("binance")
    inc = pd.DataFrame(bn[uid]["income"])
    inc["income"] = inc.income.astype(float)
    inc["time"] = inc.time.astype("int64")
    pos = db["positions"]
    p = pos[(pos.user_id == uid) & (pos.source == "auto")]
    t0 = int(p.entry_time.min()) if t_from is None else t_from
    inc = inc[(inc.time >= t0) & inc.symbol.isin([f"{s}USDT" for s in p.symbol])]
    funding = inc[inc.incomeType == "FUNDING_FEE"].income.sum()
    perp_fee = -inc[inc.incomeType == "COMMISSION"].income.sum()
    hist = db["history"]
    h = hist[(hist.user_id == uid) & (hist.reason != "manual") &
             (hist.closed_at.apply(lambda v: _ms(pd.Timestamp(v)) or 0) >= t0)]
    price_closed = h.price_pnl.fillna(0).sum()
    mfs = db["mfs"]
    unreal = 0.0
    for _, r in p[p.status == "active"].iterrows():
        m = mfs[mfs.symbol == r.symbol].sort_values("ts_hour")
        if m.empty:
            continue
        m = m.iloc[-1]
        perp = float(m.perp_mid)
        spot = perp / (1 + float(m.basis_pct) / 100)
        unreal += r.spot_qty * (spot - r.spot_entry_price) + r.perp_qty * (r.entry_price - perp)
    cap = db["capital"]
    c = cap[cap.user_id == uid]
    capital = float(c.total_capital.mean()) if len(c) else float(p.capital_used.max())
    days = (time.time() * 1000 - t0) / 86400e3
    k = 30 / days / capital * 100
    return {"uid": uid, "days": days, "capital": capital, "funding": funding, "perp_fee": perp_fee,
            "price_closed": price_closed, "unreal": unreal,
            "net": funding - perp_fee + price_closed + unreal,
            "m_funding": funding * k, "m_fee": -perp_fee * k, "m_price": (price_closed + unreal) * k,
            "m_net": (funding - perp_fee + price_closed + unreal) * k,
            "deployed": float(c.deployed_capital.mean() / c.total_capital.mean()) if len(c) else None}


# ───────────────────────── informe ─────────────────────────
def _f(v, d=2):
    return "—" if v is None or (isinstance(v, float) and np.isnan(v)) else f"{v:+.{d}f}"


def _md(df, cols, fmt=None):
    fmt = fmt or {}
    out = ["| " + " | ".join(c for c, _ in cols) + " |", "|" + "---|" * len(cols)]
    for _, r in df.iterrows():
        cells = []
        for _, k in cols:
            v = r[k]
            f = fmt.get(k)
            cells.append(f(v) if f else (f"{v:,.2f}" if isinstance(v, (float, np.floating)) else str(v)))
        out.append("| " + " | ".join(cells) + " |")
    return chr(10).join(out)


def run():
    _load_env()
    m = manual_trades()
    a = auto_trades()
    df = add_counterfactual(pd.concat([m[m.ok], a], ignore_index=True))
    df["days"] = df.hours / 24
    df["fund_pct_day"] = df.funding / df.X / df.days.clip(lower=0.5) * 100
    df["rt_cost"] = df.b1 - df.b0                       # pp del nocional pagados en basis ida y vuelta
    df["price_over_funding"] = -df.price / df.funding.where(df.funding > 0)
    q1 = q1_systematic()
    jt = jobs_table()
    hs = hourly_stats()
    br5 = bridge(5)
    br4 = bridge(4, t_from=int(a[a.uid == 4].t0.min() * 1000))
    _save("results", {"trades": df, "q1": q1, "jobs": jt, "hourly": hs, "bridge5": br5, "bridge4": br4})
    write_report(df, q1, jt, hs, br5, br4)
    return df, q1, jt, hs, br5


def _roc_month(g):
    """Neto / (capital · días) × 30, ponderado por capital-tiempo (capital = spot + margen)."""
    cap_days = (g.capital * g.days).sum()
    return g.net.sum() / cap_days * 30 * 100 if cap_days else np.nan


def write_report(df, q1, jt, hs, br5, br4):
    man, aut = df[df.src == "manual"], df[df.src == "auto"]
    lucky = man[man.pid == 33]
    man_x = man[man.pid != 33]
    g = q1["gate_usd_vs_qty"]
    jr = jt.dropna(subset=["fire", "real"])
    fmt2 = {k: (lambda v: _f(v, 2)) for k in ("b0", "b1", "rt_cost", "price", "E", "Xb", "D", "funding", "net",
                                               "alt_net", "fees")}
    fmt2.update({"days": lambda v: f"{v:.1f}", "X": lambda v: f"{v:,.0f}", "lev": lambda v: f"{v:g}x",
                 "spot_lag_in_min": lambda v: _f(v, 0), "spot_lag_out_min": lambda v: _f(v, 0)})
    k = 30 / br5["days"] / br5["capital"] * 100
    price5 = br5["price_closed"] + br5["unreal"]
    L = []
    w = L.append
    w(f"# Revisión de la operación real — {datetime.now():%Y-%m-%d}")
    w("")
    w("Fuente: `scripts/ops_review.py` (solo lectura: DB de prod, fills e income de Binance con las keys de cada "
      "usuario, transferencias BEP-20 de las wallets manuales) y `scripts/ops_review_report.py`. "
      "Los usuarios 2 y 4 comparten la misma cuenta Binance (income idéntico); el 5 es caoviedor.")
    w("")
    w("## Resumen")
    w("")
    w(f"- **Q1 — USDT igual vs cantidad igual: la diferencia es ruido.** En los {len(man)} trades manuales "
      f"reconstruidos, el delta residual de cubrir con el mismo USDT sumó {man.D.sum():+.1f} USD; con cantidad "
      f"igual el neto habría sido {man.alt_net.sum():+.1f} vs {man.net.sum():+.1f} USD reales. En 13 meses de "
      f"backtest ({q1['n']:,} operaciones del motor) la diferencia media es {g['d']:+.3f} pp por operación "
      f"(IC {g['lo']:+.3f}/{g['hi']:+.3f}) y gana {g['wins']}/{g['total']} trimestres → **no pasa el criterio**. "
      "Ambas reglas pagan igual la basis de entrada y la de salida; el bot de funding de Binance también cubre "
      "por cantidad.")
    w(f"- **La basis sí afectaba en la era manual, solo que no se medía** (`price_pnl` vacío). Reconstruido: "
      f"{man.price.sum():+.1f} USD de precio, pero {lucky.price.sum():+.1f} viene de un solo trade (ESPORTS #33: "
      f"el spot se vendió 41 min después de recomprar el short, en plena subida). Sin él, el precio fue "
      f"{man_x.price.sum():+.1f} USD = {-man_x.price.sum() / man_x.funding.sum() * 100:.0f} % del funding "
      f"({man_x.funding.sum():.1f}). El funding manual de la DB era estimado: {man.funding_db.sum():.1f} vs "
      f"{man.funding.sum():.1f} USD reales (income).")
    w(f"- **caoviedor ({br5['days']:.0f} d, capital {br5['capital']:,.0f} USD)**: funding "
      f"{br5['m_funding']:+.2f} %/mes, comisiones perp {br5['m_fee']:+.2f}, precio (cerrado + a mercado) "
      f"{br5['m_price']:+.2f} → **{br5['m_net']:+.2f} %/mes**. El precio se come "
      f"{-price5 / br5['funding'] * 100:.0f} % del funding; {31.07 / -price5 * 100:.0f} % de esa pérdida es la "
      "salida por SL de ARIA a 2x. Aun con precio y fees en cero el funding da ~1.5 %/mes: **3 %/mes no está al "
      "alcance a 1x en este mercado** (igual que `profit_study_20261117`: ~2.0 %/mes esperado a 1x).")
    w(f"- **La espera de basis no aparece en los llenados reales**: entre la lectura que dispara y el llenado la "
      f"basis cae {(jr.real - jr.fire).mean():+.2f} pp en promedio (n={len(jr)}). Entradas con espera: "
      f"{jt[jt.waited & (jt.kind == 'open')].real.mean():+.2f} % realizado; sin espera "
      f"{jt[~jt.waited & (jt.kind == 'open')].real.mean():+.2f} %. En el mercado horario la basis revierte: una "
      f"hora ≥ 0.25 % (media {hs['rev'].b_h.iloc[0]:.2f}) promedia {hs['rev'].b_next.iloc[0]:.2f} la siguiente. "
      "Las salidas esperaron 15–25 h y aun así pagaron basis.")
    w(f"- **El costo dominante es la basis de salida**, en manual y en auto (ida y vuelta mediana "
      f"{man.rt_cost.median():.2f} pp manual / {aut.rt_cost.median():.2f} pp auto). Lo que lo diluía en la era "
      f"manual era la **tenencia**: mediana {man.days.median():.0f} d manual vs {aut.days.median():.0f} d auto. "
      f"ROC mensual ponderado por capital-tiempo: manual {_roc_month(man):+.2f} % (sin #33 "
      f"{_roc_month(man_x):+.2f} %), auto {_roc_month(aut):+.2f} %.")
    w("")
    w("## Manual vs auto: qué cambió")
    w("")
    w("| Grupo | n | funding %/día del nocional | precio % del nocional | fees % | días (mediana) | lev | ROC %/mes |")
    w("|---|---|---|---|---|---|---|---|")
    for name, gg in (("Manual", man), ("Manual sin ESPORTS #33", man_x), ("Auto (todos)", aut),
                     ("Auto ≥ 500 USD", aut[aut.X > 500])):
        w(f"| {name} | {len(gg)} | {gg.funding.sum() / (gg.X * gg.days).sum() * 100:.3f} | "
          f"{gg.price.sum() / gg.X.sum() * 100:+.2f} | {gg.fees.sum() / gg.X.sum() * 100:.2f} | "
          f"{gg.days.median():.1f} | {gg.lev.mean():.1f} | {_roc_month(gg):+.2f} |")
    w("")
    w("El costo de precio por operación es del mismo orden en ambas eras (~0.6–0.8 % del nocional). La era manual "
      "rindió más por **funding ~1.7× mayor** (julio–octubre, otro régimen y otra selección) y por **tenencias "
      "el doble de largas**, que reparten el mismo costo de salida entre más días de cobro. No por la regla de "
      "cobertura.")
    w("")
    w("## Q1 — Cobertura por USDT igual vs por cantidad igual")
    w("")
    w("Con X USD por pierna, b = P/S − 1 y r = P₁/P₀:")
    w("")
    w("- USDT igual: PnL/X = (S₁/S₀)·(b₀ − b₁)/(1 + b₀)")
    w("- Cantidad igual: PnL/X = b₀ − (S₁/S₀)·b₁")
    w("- **Cantidad − USDT = −b₀·(r − 1)** (+ b₀·funding, porque el short es b₀ más grande): un delta largo "
      "residual de b₀ del nocional. Con b₀ ±0.3 % y el precio ±20 % son ±0.06 % del nocional, de signo aleatorio.")
    w("")
    w("Descomposición exacta (USD): precio = qs·(P₀ − S₀) **[entrada]** − qs·(P₁ − S₁) **[salida]** + "
      "(qs − qp)·(P₁ − P₀) **[delta]**. P y S son precios de ejecución (incluyen impacto y spread).")
    w("")
    w("### Trades manuales reconstruidos (swaps on-chain + fills del perp)")
    w("")
    w(_md(man, [("user", "uid"), ("#", "pid"), ("token", "sym"), ("días", "days"), ("USD spot", "X"),
                ("b₀ %", "b0"), ("b₁ %", "b1"), ("lag spot entrada min", "spot_lag_in_min"),
                ("lag spot salida min", "spot_lag_out_min"), ("entrada", "E"), ("salida", "Xb"), ("delta", "D"),
                ("precio", "price"), ("funding", "funding"), ("fees perp", "fees"), ("neto", "net"),
                ("neto con cantidad igual", "alt_net")], fmt2))
    w("")
    w("Sin fills: las manuales de marzo–junio (la API da 6 meses de `userTrades`; las de junio de slavado no "
      "aparecen en esa cuenta). ZIL #37 se excluye (no es short perp + spot largo). Fees perp = comisión real; el "
      "costo del swap está dentro del precio.")
    w("")
    w("### Trades auto (exec_json) y contrafactual con USDT igual")
    w("")
    w(_md(aut, [("user", "uid"), ("#", "pid"), ("token", "sym"), ("lev", "lev"), ("días", "days"),
                ("USD spot", "X"), ("b₀ %", "b0"), ("b₁ %", "b1"), ("entrada", "E"), ("salida", "Xb"),
                ("delta", "D"), ("precio", "price"), ("funding", "funding"), ("fees", "fees"), ("neto", "net"),
                ("neto con USDT igual", "alt_net")], fmt2))
    w("")
    w(f"Σ neto auto {aut.net.sum():+.1f} vs {aut.alt_net.sum():+.1f} USD con USDT igual (casi todo por funding: "
      "con b₀ < 0 el short de USDT igual es más grande).")
    w("")
    w("### Backtest sistemático (motor de `profit_study`, política vigente P0, 1,000 USD)")
    w("")
    w(f"- {q1['n']:,} operaciones de 25 tokens, 2025-10 → 2026-10; r con el cierre 1h del perp. Δ (USDT − "
      f"cantidad) {g['d']:+.4f} pp, IC 95 % {g['lo']:+.4f}/{g['hi']:+.4f}, Δp5 {g['dp5']:+.3f} pp, "
      f"{g['wins']}/{g['total']} trimestres → **no pasa**. |Δ| medio {q1['mean_abs_diff']:.3f} pp; correlación de "
      f"b₀ con el retorno posterior {q1['corr_b0_ret']:+.3f} (b₀ no anticipa la dirección).")
    w("- El motor ya valuaba como USDT igual (spot/SE − perp/PE) y prod opera cantidad igual: la diferencia no "
      "cambia ninguna conclusión de los estudios previos.")
    w("")
    w("## Q2 — Por qué caoviedor rinde ~0.5 %/mes")
    w("")
    w("| Concepto | USD | %/mes sobre capital |")
    w("|---|---|---|")
    for name, v in (("Funding (income real)", br5["funding"]), ("Comisiones perp", -br5["perp_fee"]),
                    ("Precio cerrado (ARIA, STAR, CLO)", br5["price_closed"]),
                    ("Precio a mercado (JCT, TAC)", br5["unreal"]), ("**Neto**", br5["net"])):
        w(f"| {name} | {v:+.2f} | {v * k:+.2f} |")
    w("")
    w(f"Capital promedio {br5['capital']:,.0f} USD, desplegado {br5['deployed'] * 100:.0f} %, {br5['days']:.1f} d "
      f"desde la primera apertura. Referencia slavado (auto, {br4['days']:.0f} d): funding {br4['m_funding']:+.2f}, "
      f"fees {br4['m_fee']:+.2f}, precio {br4['m_price']:+.2f} → {br4['m_net']:+.2f} %/mes.")
    w("")
    w("Causas, en orden:")
    w("")
    w("1. **Nivel de funding**: ~33 % APR sobre el nocional; a 1x cobra la mitad del capital → ~1.5 %/mes bruto. "
      "Para 3 %/mes neto haría falta ~75 % APR sostenido o leverage con rebalanceo (estudio previo: 2x ≈ 2.3 %/mes).")
    w("2. **ARIA a 2x cerró por SL** a los 3 d: salida −31.1 USD (35 % de todo el funding del periodo). Las tres "
      "primeras se abrieron a 2x y sin espera (antes del 2026-11-10); desde entonces el usuario abre a 1x.")
    w("3. **Basis de salida** en STAR (+0.64 % de costo, cerrada con «Ejecutar ya» tras 22 h de espera) y CLO "
      "(+0.17 % tras 14.7 h). La espera de salida no consiguió basis ≥ 0 en ningún cierre.")
    w("4. **Tenencias cortas**: 3, 11.5 y 17.6 d. Con ~0.1 %/día de funding sobre el nocional, un costo de ida y "
      "vuelta de 0.6 pp necesita ≥ 30 d para quedar bajo el 20 % del funding.")
    w("")
    w("## ¿Funciona la espera de basis?")
    w("")
    w(_md(jt, [("job", "job"), ("user", "uid"), ("tipo", "kind"), ("token", "sym"), ("estado", "status"),
               ("θ", "theta"), ("espera h", "wait_h"), ("total h", "total_h"), ("lectura al disparar", "fire"),
               ("antes del 1er tramo", "first"), ("realizado", "real"), ("tramos", "slices"),
               ("funding en la espera", "wait_funding")],
          {"theta": lambda v: _f(v), "wait_h": lambda v: f"{v:.2f}", "total_h": lambda v: f"{v:.2f}",
           "fire": lambda v: _f(v), "first": lambda v: _f(v), "real": lambda v: _f(v),
           "slices": lambda v: "—" if v is None or v != v else f"{v:g}", "wait_funding": lambda v: _f(v)}))
    w("")
    w("Basis en %, convención de la lectura (+ = ganancia; en cierres = −`exit_basis_pct`).")
    w("")
    rv = hs["rev"]
    w(f"Mercado horario (`market_feature_snapshots`, {hs['rows']:,} horas, {hs['syms']} tokens Alpha, basis de "
      f"mids): mediana {hs['median']:+.3f} %. Si una hora marca ≥ 0.25 % (media {rv.b_h.iloc[0]:.2f}), la "
      f"siguiente promedia {rv.b_next.iloc[0]:.2f} y solo {rv.p_stay.iloc[0] * 100:.0f} % sigue ≥ 0.25; con "
      f"≥ 0.5 ({rv.b_h.iloc[1]:.2f}) → {rv.b_next.iloc[1]:.2f}. La regla «primera hora ≥ 0.25 en 6 h» lee "
      f"{hs['wait']['rule']:.2f} % vs {hs['wait']['now']:.2f} % al pedir, pero una hora después de esa lectura la "
      f"basis está en {hs['wait']['rule_next_h']:.2f} %: los picos que disparan duran menos que la ejecución "
      "(2–4 tramos cada 15 min).")
    w("")
    w("Lectura: el disparo selecciona picos ruidosos (sesgo del ganador) y los tramos de 15 min llegan cuando el "
      "pico ya revirtió. El motor (`exec_study`) daba +0.16 pp a la espera; con estas pocas ejecuciones reales no "
      "se confirma ni se refuta, pero la dirección del sesgo es clara. ρ (recuperación del pool) sigue sin "
      "medirse: ningún tramo guardó `recovery` utilizable.")
    w("")
    w("## Técnicas evaluadas")
    w("")
    w("| Técnica | Resultado | Decisión |")
    w("|---|---|---|")
    w(f"| Cubrir con USDT igual | Δ {g['d']:+.3f} pp, {g['wins']}/{g['total']} trimestres | No pasa; no cambiar |")
    clock = hs["clock"]
    w("| Disparar según el reloj del settlement | basis vs su media del día por minutos al pago: "
      + ", ".join(f"{i} min {v:+.3f}" for i, v in clock["mean"].items()) + " pp | Sin efecto; descartar |")
    w("| Exigir persistencia (2 lecturas ≥ θ separadas ≥ 5 min) | El exceso sobre la mediana revierte ~50 % en 1 h | "
      "Hipótesis: requiere registrar cada lectura del probe para medirla |")
    w("| Perp maker (post-only) | Ahorro ≤ 0.06 pp por ida y vuelta (0.05 → 0.02 % por lado) con riesgo de "
      "quedar sin cubrir | Menor que el costo de basis; no prioritario |")
    w("| Tope 1x en Alpha | ARIA 2x: −31 USD por SL | Ya recomendado; evaluar bloquear > 1x en BSC |")
    w("| Tenencia larga / no cerrar por señal débil | Manual (mediana 17 d) diluía el mismo costo de salida | "
      "Ya adoptado (3.er settlement sin cobrar); vigilar cierres manuales tempranos |")
    w("")
    w("## Recomendaciones")
    w("")
    w("1. **No cambiar la regla de cobertura** (cantidad igual). Es la del bot de Binance y la diferencia es ruido.")
    w("2. **Registrar cada lectura del probe** (`progress_json` se pisa en cada lectura): basis, hora y tamaño, y la "
      "basis realizada por tramo. Con ~20 ejecuciones se decide si la espera aporta o si conviene ejecutar al "
      "pedir y evitar 15–25 h de espera en las salidas.")
    w("3. **Revisar el margen del guard**: `FIRE_BASIS_MARGIN` deja ejecutar hasta θ − 0.25 (entradas desde 0 %, "
      "salidas hasta −0.25 %). Con la reversión medida, el guard debería comparar contra el umbral completo o "
      "re-armar; probarlo con los datos del punto 2.")
    w("4. **Alpha a 1x siempre** (el SL a 2x fue la mayor pérdida del usuario).")
    w("5. **Expectativa**: en el régimen actual el neto realista a 1x es ~1–2 %/mes; 3 %/mes requiere funding "
      "> 70 % APR o leverage con rebalanceo validado (`feat/signals-telegram`).")
    w("")
    w("Fuentes externas: He, Manela, Ross, von Wachter, *Fundamentals of Perpetual Futures* (arXiv 2212.06888), "
      "bandas de no arbitraje con costos; Binance, *Funding Rate Arbitrage Bot* (FAQ), el bot iguala la "
      "cantidad del futuro a la del spot ejecutado.")
    REPORT.write_text(chr(10).join(L) + chr(10), encoding="utf-8")
    print(f"-> {REPORT}")


if __name__ == "__main__":
    pd.set_option("display.width", 250)
    run()
