#!/usr/bin/env python3
"""Auditoría de los swaps BSC reales: ruta, slippage y patrón de sándwich — read-only.

Sándwich: en las ±6 txs vecinas del mismo bloque, una tx antes y otra después mueven
cantidades casi iguales del token en sentido contrario contra uno de nuestros pools,
y la primera va en nuestra misma dirección.

    python scripts/swap_audit.py [--since-position 47]
"""
import argparse
import json
import os
import sys
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor

import requests
from sqlalchemy import create_engine, text

RPCS = ["https://bsc-rpc.publicnode.com", "https://bsc-dataseed.bnbchain.org", "https://bsc.drpc.org"]
TRANSFER = "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"
NEIGHBORS = 6
MATCH_TOL = 0.03


def rpc(method, params):
    last = None
    for url in RPCS:
        for _ in range(2):
            try:
                r = requests.post(url, json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params},
                                  timeout=25).json()
                if r.get("result") is not None:
                    return r["result"]
                last = r.get("error")
            except Exception as e:
                last = e
    raise RuntimeError(f"RPC {method}: {last}")


def transfers(receipt):
    return [(lg["address"].lower(), "0x" + lg["topics"][1][-40:], "0x" + lg["topics"][2][-40:], int(lg["data"], 16))
            for lg in receipt["logs"]
            if lg["topics"] and lg["topics"][0] == TRANSFER and len(lg["topics"]) == 3 and lg["data"] != "0x"]


def pool_flow(trs, token, pools):
    """Entrada neta del token por pool (negativo: el pool entregó tokens)."""
    flow = defaultdict(int)
    for addr, src, dst, value in trs:
        if addr != token:
            continue
        if src in pools:
            flow[src] -= value
        if dst in pools:
            flow[dst] += value
    return flow


def find_sandwich(side, before, after, pools):
    """before/after: [(idx, flujo por pool)]. Devuelve (pool, idx_antes, idx_después, cantidad)."""
    for i1, f1 in before:
        for i2, f2 in after:
            for pool in pools:
                a, b = f1.get(pool, 0), f2.get(pool, 0)
                if not a or not b or a * b > 0:
                    continue
                if abs(abs(a) - abs(b)) / max(abs(a), abs(b)) >= MATCH_TOL:
                    continue
                if (side == "buy" and a < 0) or (side == "sell" and a > 0):
                    return pool, i1, i2, abs(a)
    return None


def audit_swap(side, tx_hash, token):
    receipt = rpc("eth_getTransactionReceipt", [tx_hash])
    idx = int(receipt["transactionIndex"], 16)
    wallet = receipt["from"].lower()
    block = rpc("eth_getBlockByNumber", [receipt["blockNumber"], True])
    ours = transfers(receipt)
    executor = next((dst if src == wallet else src for addr, src, dst, _ in ours
                     if addr == token and wallet in (src, dst)), None)
    pools = {dst if src == executor else src for addr, src, dst, _ in ours
             if addr == token and executor in (src, dst) and wallet not in (src, dst)}
    near = [t for t in block["transactions"] if 0 < abs(int(t["transactionIndex"], 16) - idx) <= NEIGHBORS]
    with ThreadPoolExecutor(6) as ex:
        receipts = list(ex.map(lambda t: rpc("eth_getTransactionReceipt", [t["hash"]]), near))
    flows = [(int(r["transactionIndex"], 16), pool_flow(transfers(r), token, pools)) for r in receipts]
    found = find_sandwich(side, [f for f in flows if f[0] < idx], [f for f in flows if f[0] > idx], pools)
    return {"idx": idx, "txs": len(block["transactions"]), "sandwich": found}


def _engine():
    try:
        from dotenv import load_dotenv
        load_dotenv(".env")
    except ImportError:
        pass
    url = os.environ.get("DATABASE_URL", "")
    if not url:
        sys.exit("ERROR: DATABASE_URL no está configurada.")
    url = url.replace("postgres://", "postgresql+psycopg2://", 1).replace("postgresql://", "postgresql+psycopg2://", 1)
    return create_engine(url, pool_pre_ping=True, connect_args={"options": "-c default_transaction_read_only=on"})


def _json(value):
    return json.loads(value) if isinstance(value, str) else (value or {})


def collect(conn, since):
    q = text("""select p.id, p.symbol, p.spot_qty, o.params_json as params, o.result_json as result
                from user_positions p join execution_jobs o on o.id = p.job_id
                where p.spot_venue = 'bsc' and p.id >= :since order by p.id""")
    closes = {r.position_id: _json(r.result_json) for r in conn.execute(text(
        "select position_id, result_json from execution_jobs where kind='close' and status='done'"))}
    items = []
    for r in conn.execute(q, {"since": since}):
        token = (_json(r.params).get("token") or {}).get("address", "").lower()
        opened, closed = _json(r.result), closes.get(r.id, {})
        n = float(r.spot_qty or 0)
        for side, res, mids, slip in (
                ("buy", opened, opened.get("ref_mids"), opened.get("entry_slippage")),
                ("sell", closed, (closed.get("exec") or {}).get("exit_ref_mids"), closed.get("exit_slippage"))):
            for tx in [o for o in res.get("order_ids", []) if str(o).startswith("0x")]:
                mid = (mids or {}).get("spot_mid")
                spot = (slip or {}).get("spot")
                items.append({"pos": r.id, "symbol": r.symbol, "side": side, "tx": tx, "token": token,
                              "slip": spot / (n * mid) * 100 if spot is not None and mid and n else None})
    return items


def main():
    sys.stdout.reconfigure(encoding="utf-8")
    ap = argparse.ArgumentParser()
    ap.add_argument("--since-position", type=int, default=47)
    args = ap.parse_args()
    with _engine().connect() as conn:
        items = collect(conn, args.since_position)
    hits = 0
    print("pos symbol side idx/txs slip% sandwich")
    for it in items:
        res = audit_swap(it["side"], it["tx"], it["token"])
        hits += bool(res["sandwich"])
        slip = "-" if it["slip"] is None else f"{it['slip']:.2f}"
        mark = f"SI pool {res['sandwich'][0][:10]} idx {res['sandwich'][1]}/{res['sandwich'][2]}" if res["sandwich"] else "no"
        print(it["pos"], it["symbol"], it["side"], f"{res['idx']}/{res['txs']}", slip, mark)
    print(f"{len(items)} swaps, {hits} con patrón de sándwich")


if __name__ == "__main__":
    main()
