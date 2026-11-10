#!/usr/bin/env python3
"""Descompone el costo real de los swaps BSC por tramo — LOCAL, read-only.

Para cada job con `swaps` registrados: hora de bloque, precio perp al segundo (aggTrades
público), basis por tramo y costo en USD contra el mid al iniciar:

    latencia  = (mid del tramo 1 − mid inicial) · cantidad total
    comisión + impacto = Σ (precio del tramo − mid del tramo) · cantidad del tramo
    deriva    = Σ (mid del tramo − mid del tramo 1) · cantidad del tramo

mid del tramo = precio cotizado / (1 ± impact), con el impact que midió el bot al operar.
La suma es exacta salvo redondeo y coincide con `entry_slippage.spot`.

    python scripts/execution_decomp.py [--job 35]
"""
import argparse
import json
import statistics
import sys
from pathlib import Path

import requests
from sqlalchemy import text

sys.path.insert(0, str(Path(__file__).resolve().parent))
from swap_audit import _engine, _json, rpc  # noqa: E402

FAPI = "https://fapi.binance.com/fapi/v1/aggTrades"
ENGINE_RHO = 0.8     # scripts/_exec_engine.py
MIN_PUSH = 0.0005    # por debajo de 0.05 % el empuje no se distingue del ruido


def block_time(tx):
    receipt = rpc("eth_getTransactionReceipt", [tx])
    return int(rpc("eth_getBlockByNumber", [receipt["blockNumber"], False])["timestamp"], 16)


def perp_at(symbol, ts):
    """Último trade del perp en los 30 s previos a ts. None si no hay."""
    rows = requests.get(FAPI, params={"symbol": f"{symbol}USDT", "startTime": int((ts - 30) * 1000),
                                      "endTime": int(ts * 1000), "limit": 1000}, timeout=20).json()
    return float(rows[-1]["p"]) if isinstance(rows, list) and rows else None


def swaps_of(result):
    return result.get("swaps") or (result.get("exec") or {}).get("swaps") or []


def decompose(job):
    res, params = job["result"], job["params"]
    buy = job["kind"] == "open"
    sym = params.get("symbol")
    ref = (res.get("ref_mids") or (res.get("exec") or {}).get("exit_ref_mids") or {}).get("spot_mid")
    rows = []
    for s in swaps_of(res):
        qty = s["out"] if buy else s["in"]
        usdt = s["in"] if buy else s["out"]
        quoted = s["in"] / s["quoted_out"] if buy else s["quoted_out"] / s["in"]
        ts = block_time(s["tx"])
        rows.append({"ts": ts, "qty": qty, "px": usdt / qty,
                     "mid": quoted / (1 + s["impact"]) if buy else quoted / (1 - s["impact"]),
                     "impact": s["impact"], "perp": perp_at(sym, ts)})
    if not rows or not ref:
        return None
    sign = 1 if buy else -1
    total = sum(r["qty"] for r in rows)
    first = rows[0]["mid"]
    latency = sign * (first - ref) * total
    cost = sign * sum((r["px"] - r["mid"]) * r["qty"] for r in rows)
    drift = sign * sum((r["mid"] - first) * r["qty"] for r in rows)
    return {"job": job["id"], "symbol": sym, "kind": job["kind"], "ref_mid": ref, "rows": rows,
            "latency": latency, "cost": cost, "drift": drift, "total": latency + cost + drift,
            "reported_spot_slip": (res.get("entry_slippage") or res.get("exit_slippage") or {}).get("spot"),
            "basis": res.get("basis_pct") if buy else (res.get("exec") or {}).get("exit_basis_pct"),
            "fired_at": params.get("fired_at")}


def retention(d):
    """Fracción del empuje teórico de cada tramo que sigue en el pool al siguiente, descontando
    lo que se movió el perp. Empuje teórico = 2·(impacto sobre el piso de comisión); con un
    solo job es una estimación ruidosa."""
    rows = [r for r in d["rows"] if r["perp"]]
    if len(rows) < 3:
        return None
    fee = min(r["impact"] for r in rows)
    pairs = zip(rows[:-1], rows[1:])
    moves = [(b["mid"] / a["mid"] - 1) - (b["perp"] / a["perp"] - 1) for a, b in pairs]
    return {"mean_excess_move_pct": sum(moves) / len(moves) * 100, "n": len(moves), "fee_floor_pct": fee * 100}


def rho_from_swap(s):
    """Retención del empuje por minuto medida tras un tramo (`recovery`): empuje = cuánto se separó
    el mid del perp respecto de antes del swap. None si no hay datos o el empuje es ruido."""
    buy = s.get("side", "buy") == "buy"
    marks = [m for m in s.get("recovery") or [] if m.get("mid") and m.get("perp")]
    if len(marks) < 2 or not (s.get("perp_ref") and s.get("impact") and s.get("quoted_out") and s.get("in")):
        return None
    quoted = s["in"] / s["quoted_out"] if buy else s["quoted_out"] / s["in"]
    before = (quoted / (1 + s["impact"]) if buy else quoted / (1 - s["impact"])) / s["perp_ref"]
    push = [(1 if buy else -1) * (m["mid"] / m["perp"] - before) / before for m in marks]
    if push[0] < MIN_PUSH:
        return None
    t0 = marks[0]["t"]
    rates = [max(p / push[0], 0.0) ** (60 / (m["t"] - t0)) for m, p in zip(marks[1:], push[1:]) if m["t"] > t0]
    return {"rho": statistics.median(rates), "push_pct": push[0] * 100, "n": len(rates)} if rates else None


def rho_report(jobs):
    """ρ por token con todos los tramos que tienen recuperación medida."""
    by_token = {}
    for job in jobs:
        for s in swaps_of(job["result"]):
            r = rho_from_swap(s)
            if r:
                by_token.setdefault(job["params"].get("symbol"), []).append(r["rho"])
    print(f"\nρ medido por minuto (el motor asume {ENGINE_RHO}):")
    for sym, vals in sorted(by_token.items()):
        print(f"  {sym}: mediana {statistics.median(vals):.3f} ({len(vals)} tramos)")
    if not by_token:
        print("  sin tramos con recuperación medida")


def main():
    sys.stdout.reconfigure(encoding="utf-8")
    ap = argparse.ArgumentParser()
    ap.add_argument("--job", type=int)
    args = ap.parse_args()
    q = "select id, kind, params_json, result_json from execution_jobs where status='done' order by id"
    with _engine().connect() as conn:
        jobs = [{"id": r.id, "kind": r.kind, "params": _json(r.params_json), "result": _json(r.result_json)}
                for r in conn.execute(text(q))]
    jobs = [j for j in jobs if swaps_of(j["result"]) and (args.job is None or j["id"] == args.job)]
    print(f"{len(jobs)} jobs con swaps registrados")
    out = []
    for job in jobs:
        d = decompose(job)
        if not d:
            continue
        out.append(d)
        print(f"\nJob {d['job']} {d['symbol']} {d['kind']}  mid inicial {d['ref_mid']:.8f}  basis {d['basis']}")
        t0 = d["fired_at"] or d["rows"][0]["ts"]
        for r in d["rows"]:
            basis = (r["perp"] / r["px"] - 1) * 100 if r["perp"] else float("nan")
            print(f"  +{r['ts'] - t0:>4.0f}s  px {r['px']:.8f}  mid {r['mid']:.8f}  perp {r['perp'] or 0:.6f}  "
                  f"basis {basis:+.3f} %  impact {r['impact'] * 100:.2f} %")
        print(f"  USD: latencia {d['latency']:+.2f} · comisión+impacto {d['cost']:+.2f} · deriva {d['drift']:+.2f} "
              f"= {d['total']:+.2f} (reportado spot {d['reported_spot_slip']})")
        print("  retención:", json.dumps(retention(d)))
    rho_report(jobs)


if __name__ == "__main__":
    main()
