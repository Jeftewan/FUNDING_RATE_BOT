#!/usr/bin/env python3
"""Revisión de la operación real — LOCAL, solo lectura (DB de prod, Binance GET, RPC BSC).

Preguntas (2026-11-24):
  Q1. ¿Cubrir con el mismo USDT por pierna (era manual) en vez de la misma cantidad del activo (API) reduce el
      efecto de la basis?
  Q2. ¿Por qué un usuario nuevo rinde ~0.4 %/mes? ¿Funciona la espera de basis al entrar/salir?

Fórmulas (X USD por pierna, b = P/S − 1, r = P₁/P₀):
  USDT igual     PnL/X = S₁/S₀ − P₁/P₀ = (S₁/S₀)·(b₀ − b₁)/(1 + b₀)
  cantidad igual PnL/X = (S₁ − S₀ − P₁ + P₀)/S₀
  cantidad − USDT = −b₀·(r − 1)   → solo un delta residual de b₀ del nocional; ambas pagan la basis igual.

    python -u scripts/ops_review.py --fetch db|binance|chain|all [--railway]
    python -u scripts/ops_review.py                       # → reports/ops_review_YYYYMMDD.md
"""
import argparse
import json
import os
import pickle
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))
CACHE = ROOT / "cache" / "ops_review"
USERS = (2, 4, 5)
# Wallets on-chain de la era manual: OPS_MANUAL_WALLETS="2:0x…,4:0x…" en .env (no se versionan).
MANUAL_WALLETS = {}


def _wallets():
    for part in os.environ.get("OPS_MANUAL_WALLETS", "").split(","):
        if ":" in part:
            uid, addr = part.split(":", 1)
            MANUAL_WALLETS[int(uid)] = addr.strip()
RAILWAY_PROJECT = "7dc7767e-983c-45a6-afc0-be5210dee632"
RPC = "https://bsc-mainnet.nodereal.io/v1/64a9df0874fb4a93b9d0a3849de012d3"   # chainlist: getLogs histórico
TRANSFER = "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"
STABLES = {"0x55d398326f99059ff775485246999027b3197955": "USDT",
           "0x8ac76a51cc950d9822d68b83fe1ad97b32cd580d": "USDC",
           "0xe9e7cea3dedca5984780bafc599bd69add087d56": "BUSD"}
WBNB = "0xbb4cdb9cbd36b01bd1cbaebf2de08d9173bc095c"
DAY = 86400


# ───────────────────────── utilidades ─────────────────────────
def _load_env():
    from dotenv import load_dotenv
    load_dotenv(ROOT / ".env")
    _wallets()


def _railway_env():
    out = subprocess.run(["railway", "variables", "--json", "-p", RAILWAY_PROJECT, "-e", "production",
                          "-s", "FUNDING_RATE_BOT"], capture_output=True, text=True, shell=os.name == "nt")
    data = json.loads(out.stdout)
    for k in ("FERNET_KEY", "EXCHANGE_PROXY_URL"):
        if data.get(k):
            os.environ[k] = data[k]


def _save(name, obj):
    CACHE.mkdir(parents=True, exist_ok=True)
    with open(CACHE / f"{name}.pkl", "wb") as f:
        pickle.dump(obj, f)


def _load(name):
    p = CACHE / f"{name}.pkl"
    return pickle.load(open(p, "rb")) if p.exists() else None


def _ms(v):
    import pandas as pd
    if v is None or (not isinstance(v, (int, float)) and pd.isna(v)) or (isinstance(v, float) and v != v):
        return None
    if isinstance(v, (int, float)):
        return int(v if v > 1e11 else v * 1000)
    if v.tzinfo is None:
        v = v.replace(tzinfo=timezone.utc)
    return int(v.timestamp() * 1000)


# ───────────────────────── extracción: DB ─────────────────────────
def fetch_db():
    import pandas as pd
    from sqlalchemy import text
    from _scoring_data import get_engine
    ids = ",".join(map(str, USERS))
    with get_engine().connect() as c:
        out = {
            "positions": pd.read_sql(text(f"SELECT * FROM user_positions WHERE user_id IN ({ids})"), c),
            "history": pd.read_sql(text(f"SELECT * FROM user_history WHERE user_id IN ({ids})"), c),
            "jobs": pd.read_sql(text(f"SELECT * FROM execution_jobs WHERE user_id IN ({ids})"), c),
            "capital": pd.read_sql(text(f"SELECT * FROM user_capital_daily WHERE user_id IN ({ids})"), c),
            "configs": pd.read_sql(text(f"SELECT user_id, total_capital, max_positions FROM user_configs "
                                        f"WHERE user_id IN ({ids})"), c),
        }
        syms = sorted(set(out["positions"].symbol))
        out["mfs"] = pd.read_sql(text("SELECT symbol, ts_hour, basis_pct, spot_source, funding_rate_est, spot_mid, perp_mid, next_funding_ts "
                                      "FROM market_feature_snapshots WHERE lower(exchange)='binance' AND symbol = ANY(:s)"),
                                 c, params={"s": syms}) if syms else None
        # todas las series Alpha (para el valor de esperar y el reloj del settlement)
        out["mfs_alpha"] = pd.read_sql(text("SELECT symbol, ts_hour, basis_pct, funding_rate_est, next_funding_ts, "
                                            "funding_interval_hours, alpha_liquidity_usd FROM market_feature_snapshots "
                                            "WHERE lower(exchange)='binance' AND spot_source='alpha'"), c)
    _save("db", out)
    print(f"DB: {len(out['positions'])} posiciones, {len(out['history'])} historial, {len(out['jobs'])} jobs")


# ───────────────────────── extracción: Binance (GET) ─────────────────────────
def _app():
    from flask import Flask
    from core.database import db
    url = os.environ["DATABASE_URL"]
    for pre in ("postgres://", "postgresql://"):
        if url.startswith(pre):
            url = "postgresql+psycopg2://" + url[len(pre):]
    app = Flask(__name__)
    app.config.update(SQLALCHEMY_DATABASE_URI=url, SQLALCHEMY_TRACK_MODIFICATIONS=False)
    db.init_app(app)
    return app


def fetch_binance():
    from core.db_persistence import DBPersistence
    from exchanges import trade_executor as te
    db = _load("db")
    pos = db["positions"]
    app = _app()
    out = _load("binance") or {}
    now = int(time.time() * 1000)
    for uid in USERS:
        with app.app_context():
            creds = DBPersistence().load_user_exchange_keys(uid, "Binance")
        if not creds or str(creds.get("api_key", "")).startswith("gAAAA"):
            print(f"user {uid}: sin keys descifrables (¿--railway?)")
            continue
        cli = te._build_client("binance", creds, default_type="swap")
        cli.load_time_difference()
        te._ensure_markets(cli)
        trades, income = out.get(uid, {}).get("trades", {}), out.get(uid, {}).get("income", [])
        p = pos[pos.user_id == uid]
        for sym in sorted(set(p.symbol)):
            rows = p[p.symbol == sym]
            t0 = int(rows.entry_time.min()) - 2 * DAY * 1000
            t1 = max(_ms(x) or now for x in rows.closed_at.tolist()) + 2 * DAY * 1000
            t1 = min(t1, now)
            got, t, err = [], max(t0, now - 180 * DAY * 1000 + 3600_000), None
            while t < t1:
                te_ = min(t + 7 * DAY * 1000 - 1, t1)
                try:
                    r = cli.fapiPrivateGetUserTrades({"symbol": f"{sym}USDT", "startTime": t, "endTime": te_,
                                                      "limit": 1000})
                    got += r
                except Exception as e:
                    err = str(e)[:120]
                t = te_ + 1
                time.sleep(0.25)
            trades[sym] = got
            print(f"user {uid} {sym}: {len(got)} fills{' (err ' + err + ')' if err else ''}"
                  f"{' (parcial: >180 d)' if t0 < now - 180 * DAY * 1000 else ''}")
        # income de todo el periodo (FUNDING_FEE, COMMISSION, REALIZED_PNL), ventanas de 30 d
        t = int(p.entry_time.min()) - DAY * 1000 if len(p) else now
        t = max(t, now - 365 * DAY * 1000)
        income = []
        while t < now:
            te_ = min(t + 30 * DAY * 1000, now)
            start = t
            for _ in range(50):
                try:
                    r = cli.fapiPrivateGetIncome({"startTime": start, "endTime": te_, "limit": 1000})
                except Exception as e:
                    print(f"user {uid} income err {str(e)[:100]}")
                    r = []
                income += r
                if len(r) < 1000:
                    break
                start = int(r[-1]["time"]) + 1
                time.sleep(0.3)
            t = te_ + 1
        out[uid] = {"trades": trades, "income": income}
        print(f"user {uid}: income {len(income)} filas")
    _save("binance", out)


# ───────────────────────── extracción: BSC on-chain ─────────────────────────
def _rpc(method, params):
    last = None
    for i in range(6):
        try:
            r = requests.post(RPC, json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params},
                              timeout=60).json()
            if "result" in r:
                return r["result"]
            last = r.get("error")
        except Exception as e:
            last = e
        time.sleep(2 + 2 * i)
    raise RuntimeError(f"RPC {method}: {last}")


_BTS = {}


def _block_ts(b):
    if b not in _BTS:
        _BTS[b] = int(_rpc("eth_getBlockByNumber", [hex(b), False])["timestamp"], 16)
    return _BTS[b]


def _block_at(ts, head):
    lo, hi = max(1, head - 60_000_000), head
    while hi - lo > 1:
        m = (lo + hi) // 2
        if _block_ts(m) < ts:
            lo = m
        else:
            hi = m
    return hi


def _erc20_meta(addr, cache):
    if addr in cache:
        return cache[addr]
    meta = {"symbol": "?", "decimals": 18}
    try:
        meta["decimals"] = int(_rpc("eth_call", [{"to": addr, "data": "0x313ce567"}, "latest"]), 16)
        raw = bytes.fromhex(_rpc("eth_call", [{"to": addr, "data": "0x95d89b41"}, "latest"])[2:])
        if len(raw) >= 96:
            n = int.from_bytes(raw[32:64], "big")
            meta["symbol"] = raw[64:64 + n].decode(errors="ignore")
        else:
            meta["symbol"] = raw.rstrip(b"\0").decode(errors="ignore")
    except Exception:
        pass
    cache[addr] = meta
    return meta


def manual_events(db, binance):
    """[(uid, pos_id, sym, lado, ts_ms, qty, usd)] — entrada (SELL) y salida (BUY) del perp de cada manual."""
    import pandas as pd
    pos = db["positions"]
    ev = []
    for uid in MANUAL_WALLETS:
        p = pos[(pos.user_id == uid) & (pos.source == "manual")]
        for _, r in p.iterrows():
            t0 = int(r.entry_time)
            t1 = _ms(r.closed_at) or t0
            tr = (binance.get(uid) or {}).get("trades", {}).get(r.symbol, [])
            sel = [x for x in tr if t0 - 3 * DAY * 1000 <= int(x["time"]) <= t1 + 3 * DAY * 1000]
            for side in ("SELL", "BUY"):
                xs = [x for x in sel if x["side"] == side]
                if xs:
                    q = sum(float(x["qty"]) for x in xs)
                    v = sum(float(x["quoteQty"]) for x in xs)
                    ev.append((uid, int(r.id), r.symbol, side, min(int(x["time"]) for x in xs), q, v))
    return ev


def _block_near(ts, head, head_ts):
    """Bloque con timestamp ≈ ts: estimación lineal (0.45 s/bloque) + 3 correcciones."""
    b = head - int((head_ts - ts) / 0.45)
    for _ in range(4):
        d = ts - _block_ts(b)
        if abs(d) < 30:
            break
        b += int(d / 0.45)
    return b


def fetch_chain(half_h=3, span=24_000):
    """Transferencias BEP-20 de las wallets manuales en ±half_h alrededor de cada fill del perp."""
    db, binance = _load("db"), _load("binance")
    out = _load("chain") or {"windows": {}, "meta": {}, "blocks": {}}
    out.setdefault("windows", {})
    head = int(_rpc("eth_blockNumber", []), 16)
    head_ts = _block_ts(head)
    for ev in manual_events(db, binance):
        uid, pid, sym, side, ts, q, usd = ev
        key = (pid, side)
        if key in out["windows"]:
            continue
        addr = MANUAL_WALLETS[uid].lower()
        pad = "0x" + "0" * 24 + addr[2:]
        b0 = _block_near(ts / 1000 - half_h * 3600, head, head_ts)
        b1 = _block_near(ts / 1000 + half_h * 3600, head, head_ts)
        logs, b = [], b0
        while b <= b1:
            e = min(b + span - 1, b1)
            for topics in ([TRANSFER, pad], [TRANSFER, None, pad]):
                logs += _rpc("eth_getLogs", [{"fromBlock": hex(b), "toBlock": hex(e), "topics": topics}])
                time.sleep(1.0)
            b = e + 1
        for lg in logs:
            _erc20_meta(lg["address"].lower(), out["meta"])
            bn = int(lg["blockNumber"], 16)
            if bn not in out["blocks"]:
                out["blocks"][bn] = _block_ts(bn)
        out["windows"][key] = {"uid": uid, "sym": sym, "side": side, "ts": ts, "qty": q, "usd": usd,
                               "addr": addr, "b0": b0, "b1": b1, "logs": logs}
        print(f"user {uid} #{pid} {sym} {side}: {len(logs)} transferencias")
        _save("chain", out)
    _save("chain", out)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--fetch", choices=["db", "binance", "chain", "all"])
    ap.add_argument("--railway", action="store_true", help="FERNET_KEY/proxy de Railway (no se imprimen)")
    args = ap.parse_args()
    _load_env()
    _wallets()
    if args.railway:
        _railway_env()
    if args.fetch in ("db", "all"):
        fetch_db()
    if args.fetch in ("binance", "all"):
        fetch_binance()
    if args.fetch in ("chain", "all"):
        fetch_chain()
    if not args.fetch:
        from ops_review_report import run
        run()


if __name__ == "__main__":
    main()
