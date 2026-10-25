#!/usr/bin/env python3
"""Backfill de market_feature_snapshots desde la API pública de Binance — LOCAL.

Baja los ~30 d que Binance expone por API (/futures/data solo guarda el último
mes) para los perps USDT con spot USDT en Binance, con la misma resolución
horaria y las mismas funciones que el colector de prod
(exchanges/market_features.py: basis_pct, pct_change, vol_7d).

Salida: cache/binance/market_features.csv — columnas de la tabla
(symbol, exchange, ts_hour, funding_rate_est, …, volatility_7d). Se FUSIONA con
el CSV previo (clave symbol+exchange+ts_hour; lo nuevo pisa), así que correrlo
cada <30 d acumula historia más allá de la ventana de la API.

Diferencias con prod (documentadas para el entrenamiento):
  * spot_mid / perp_mid = cierre de la vela 1h que termina en ts_hour (prod: mid
    del book a ~:04) → basis_pct aproximado.
  * mark_price / index_price = cierre de las velas 1h de mark/index.
  * taker_buy_sell_ratio = periodo [ts_hour−1h, ts_hour) (igual que prod, que lee
    la última hora cerrada); OI y top long/short = punto en ts_hour.
  * funding_rate_est, next_funding_ts, intervalo y cap/floor = vacíos (no hay
    historia por API; las tasas liquidadas están en cache/binance/fr_binance.csv).

Uso:
    python scripts/market_features_backfill.py               # todos los perps con spot
    python scripts/market_features_backfill.py --symbols BTC,ETH --days 7
"""
import argparse
import csv
import sys
import time
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from exchanges.market_features import (  # noqa: E402
    DATA_MIN_INTERVAL, EXCHANGE, FEATURE_COLUMNS, basis_pct, pct_change, vol_7d, _f,
)
from exchanges.net import requests_proxies  # noqa: E402

OUT_CSV = ROOT / "cache" / "binance" / "market_features.csv"
COLUMNS = ["symbol", "exchange", "ts_hour"] + FEATURE_COLUMNS
FAPI = "https://fapi.binance.com"
SAPI = "https://api.binance.com"
H_MS = 3_600_000
DAY_MS = 86_400_000

_session = requests.Session()
_session.headers["User-Agent"] = "basyo-dataset/1.0"
_session.proxies = requests_proxies() or {}


def _get(url, params=None, tries=4, pace=0.0):
    for k in range(tries):
        if pace:
            time.sleep(pace)
        try:
            r = _session.get(url, params=params, timeout=30)
            if r.status_code in (418, 429):
                time.sleep(30 * (k + 1))
                continue
            if r.status_code == 400:   # símbolo sin datos en /futures/data
                return []
            r.raise_for_status()
            return r.json()
        except requests.RequestException:
            time.sleep(2 * (k + 1))
    raise RuntimeError(f"GET falló tras {tries} intentos: {url} {params or ''}")


def universe() -> list:
    """Bases con perp USDT PERPETUAL en TRADING y spot USDT en TRADING."""
    fut = _get(f"{FAPI}/fapi/v1/exchangeInfo")["symbols"]
    spot = _get(f"{SAPI}/api/v3/exchangeInfo")["symbols"]
    spot_ids = {s["symbol"] for s in spot if s.get("status") == "TRADING" and s.get("quoteAsset") == "USDT"}
    return sorted(s["baseAsset"] for s in fut
                  if s.get("contractType") == "PERPETUAL" and s.get("quoteAsset") == "USDT"
                  and s.get("status") == "TRADING" and s["symbol"] in spot_ids)


def _data_series(path, pid, start_ms, end_ms):
    """/futures/data/* a 1h, paginado por 500 → {timestamp: entrada}."""
    out, t = {}, start_ms
    while t < end_ms:
        rows = _get(f"{FAPI}{path}", {"symbol": pid, "period": "1h", "limit": 500,
                                       "startTime": t, "endTime": end_ms}, pace=DATA_MIN_INTERVAL)
        if not rows:
            break
        for r in rows:
            out[int(r["timestamp"])] = r
        last = max(int(r["timestamp"]) for r in rows)
        if last + H_MS <= t:
            break
        t = last + H_MS
    return out


def _closes(url, pid, start_ms, end_ms, interval="1h", key="symbol"):
    """{open_time: close} de klines (paginado por 1000)."""
    out, t = {}, start_ms
    step = H_MS if interval == "1h" else DAY_MS
    while t < end_ms:
        rows = _get(url, {key: pid, "interval": interval, "limit": 1000,
                          "startTime": t, "endTime": end_ms})
        if not rows:
            break
        for k in rows:
            out[int(k[0])] = k[4]
        t = int(rows[-1][0]) + step
        if len(rows) < 1000:
            break
    return out


def symbol_rows(sym: str, start_ms: int, end_ms: int) -> list:
    pid = f"{sym}USDT"
    oi = _data_series("/futures/data/openInterestHist", pid, start_ms - H_MS, end_ms)
    taker = _data_series("/futures/data/takerlongshortRatio", pid, start_ms - H_MS, end_ms)
    top = _data_series("/futures/data/topLongShortPositionRatio", pid, start_ms, end_ms)
    perp = _closes(f"{FAPI}/fapi/v1/klines", pid, start_ms - H_MS, end_ms)
    spot = _closes(f"{SAPI}/api/v3/klines", pid, start_ms - H_MS, end_ms)
    mark = _closes(f"{FAPI}/fapi/v1/markPriceKlines", pid, start_ms - H_MS, end_ms)
    index = _closes(f"{FAPI}/fapi/v1/indexPriceKlines", pid, start_ms - H_MS, end_ms, key="pair")
    daily = _closes(f"{FAPI}/fapi/v1/klines", pid, start_ms - 10 * DAY_MS, end_ms, "1d")
    days = sorted(daily)

    rows = []
    for ts in range(start_ms, end_ms, H_MS):
        prev = ts - H_MS   # vela 1h [ts−1h, ts) → cierre ≈ precio en ts
        o, o_prev = oi.get(ts) or {}, oi.get(prev) or {}
        row = {"symbol": sym, "exchange": EXCHANGE, "ts_hour": ts // 1000}
        row.update({c: None for c in FEATURE_COLUMNS})
        row["mark_price"] = _f(mark.get(prev))
        row["index_price"] = _f(index.get(prev))
        row["spot_mid"] = _f(spot.get(prev))
        row["perp_mid"] = _f(perp.get(prev))
        row["basis_pct"] = basis_pct(row["spot_mid"], row["perp_mid"])
        row["open_interest"] = _f(o.get("sumOpenInterest"))
        row["open_interest_usd"] = _f(o.get("sumOpenInterestValue"))
        row["oi_change_1h_pct"] = pct_change(o.get("sumOpenInterest"), o_prev.get("sumOpenInterest"))
        row["taker_buy_sell_ratio"] = _f((taker.get(prev) or {}).get("buySellRatio"))
        row["top_long_short_pos_ratio"] = _f((top.get(ts) or {}).get("longShortRatio"))
        day_start = ts // DAY_MS * DAY_MS
        row["volatility_7d"] = vol_7d([daily[d] for d in days if d < day_start])
        if any(row[c] is not None for c in FEATURE_COLUMNS):
            rows.append(row)
    return rows


def load_existing() -> dict:
    if not OUT_CSV.exists():
        return {}
    with OUT_CSV.open(newline="") as f:
        return {(r["symbol"], r["exchange"], int(r["ts_hour"])): r for r in csv.DictReader(f)}


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--days", type=int, default=30, help="ventana hacia atrás (máx. ~30 por API)")
    ap.add_argument("--symbols", default="", help="CSV de bases (default: todos los perps con spot)")
    args = ap.parse_args()

    end_ms = int(time.time() * 1000) // H_MS * H_MS          # hora en curso excluida
    start_ms = end_ms - args.days * DAY_MS + H_MS
    syms = [s.strip().upper() for s in args.symbols.split(",") if s.strip()] or universe()
    print(f"{len(syms)} símbolos, {args.days} d -> {OUT_CSV}", flush=True)

    merged = load_existing()
    n_before, n_new, fails = len(merged), 0, []
    t0 = time.time()
    for i, sym in enumerate(syms, 1):
        try:
            rows = symbol_rows(sym, start_ms, end_ms)
        except Exception as e:
            fails.append(sym)
            print(f"  {sym}: ERROR {e}", flush=True)
            continue
        for r in rows:
            merged[(r["symbol"], r["exchange"], r["ts_hour"])] = r
        n_new += len(rows)
        if i % 25 == 0 or i == len(syms):
            print(f"  {i}/{len(syms)} ({time.time() - t0:.0f}s) filas bajadas={n_new}", flush=True)

    OUT_CSV.parent.mkdir(parents=True, exist_ok=True)
    tmp = OUT_CSV.with_suffix(".tmp")
    with tmp.open("w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=COLUMNS, extrasaction="ignore")
        w.writeheader()
        for k in sorted(merged):
            w.writerow({c: ("" if merged[k].get(c) is None else merged[k][c]) for c in COLUMNS})
    tmp.replace(OUT_CSV)
    print(f"OK: {len(merged)} filas en el CSV ({len(merged) - n_before} nuevas), "
          f"{len(syms) - len(fails)}/{len(syms)} símbolos; fallidos: {fails[:10]}")


if __name__ == "__main__":
    main()
