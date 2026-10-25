#!/usr/bin/env python3
"""Dataset local de funding CROSS (Binance, Bybit, OKX, Bitget) — LOCAL, read-only.

Para el backtest cross perp (scripts/cross_backtest.py). Universo = perps lineales
USDT listados en ≥ 2 de los 4 venues (por base canónica: `1000PEPE` y `PEPE` son el
mismo par; la tasa no depende del multiplicador del contrato).

Fuentes públicas (sin API key):
  * Binance  cache/binance/fr_binance.csv (scripts/binance_dataset.py, incluye deslistados)
             + ccxt para la cola que falte. Sin ese CSV → ccxt completo.
  * Bybit    ccxt fetch_funding_rate_history paginado (historial completo, 200 por llamada).
  * OKX      la API solo guarda ~3 meses → archivos mensuales públicos
             (/api/v5/public/market-data-history, module=3) + ccxt para lo reciente.
  * Bitget   la API v2 solo guarda ~90 días (≈270 liquidaciones) y no publica archivo
             histórico → solo esa ventana (el backtest la evalúa aparte).
  * Klines 1d (OHLC + quote volume) del perp: Binance fapi, si no hay, Bybit → liquidación
    por pierna y filtro de liquidez.

Salida (mismo esquema que cache/binance/fr_binance.csv, `symbol` = base canónica + USDT):
  cache/cross/fr_<exchange>.csv   symbol,exchange,rate,volume_24h,interval_hours,funding_ts,captured_at,has_spot
  cache/cross/klines_1d.csv       symbol,day_ts,open,high,low,close,quote_volume,src
Incremental: por (exchange, símbolo) solo baja lo posterior al último funding_ts guardado.
Salida a exchanges por EXCHANGE_PROXY_URL (exchanges/net.py) si está definida.

Uso:
    python scripts/cross_dataset.py                       # desde 2025-01
    python scripts/cross_dataset.py --start 2025-01 --only bybit,okx
"""
import argparse
import csv
import io
import re
import sys
import threading
import time
import zipfile
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd
import requests

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from exchanges.net import ccxt_proxy_params, requests_proxies  # noqa: E402

OUT = ROOT / "cache" / "cross"
BINANCE_CSV = ROOT / "cache" / "binance" / "fr_binance.csv"
KLINES = OUT / "klines_1d.csv"
VENUES = ("binance", "bybit", "okx", "bitget")
COLS = ["symbol", "exchange", "rate", "volume_24h", "interval_hours", "funding_ts", "captured_at", "has_spot"]
H = 3_600_000
_MULT = re.compile(r"^(1000000|100000|10000|1000)(?=[A-Z])")
_lock = threading.Lock()


def canon(base: str) -> str:
    return _MULT.sub("", base.upper()) + "USDT"


_local = threading.local()
_markets = {}


def _client(ex_id):
    """Un cliente ccxt por hilo y venue; los mercados se cargan una sola vez."""
    import ccxt
    cache = _local.__dict__.setdefault("clients", {})
    if ex_id not in cache:
        ex = getattr(ccxt, ex_id)({"enableRateLimit": True, "timeout": 30000, **ccxt_proxy_params()})
        with _lock:
            if ex_id not in _markets:
                ex.load_markets()
                _markets[ex_id] = (ex.markets, ex.currencies)
        ex.set_markets(*_markets[ex_id])
        cache[ex_id] = ex
    return cache[ex_id]


def _get(url, params=None, tries=4):
    for k in range(tries):
        try:
            r = requests.get(url, params=params, timeout=30, proxies=requests_proxies())
            if r.status_code in (400, 404):         # símbolo inválido / deslistado
                return None
            if r.status_code in (418, 429):
                time.sleep(20 * (k + 1))
                continue
            r.raise_for_status()
            return r
        except requests.RequestException:
            time.sleep(2 * (k + 1))
    raise RuntimeError(f"GET falló: {url} {params or ''}")


def _retry(fn, tries=4):
    for k in range(tries):
        try:
            return fn()
        except Exception as e:                       # noqa: BLE001 — red/rate limit
            if k == tries - 1:
                raise
            time.sleep((10 if "429" in str(e) or "Too many" in str(e) else 2) * (k + 1))


# ── Universo ─────────────────────────────────────────────────────────────────
def load_universe():
    """({exchange: {canon: ccxt_symbol}}, {exchange: {canon: id nativo}}, spot Binance)
    de swaps lineales USDT activos."""
    markets, ids, spot = {}, {}, set()
    for ex_id in VENUES:
        m = _client(ex_id).markets
        sw = {canon(v["base"]): (s, v["id"]) for s, v in m.items()
              if v.get("swap") and v.get("linear") and v.get("settle") == "USDT"
              and v.get("active") is not False}
        markets[ex_id] = {c: s for c, (s, _) in sw.items()}
        ids[ex_id] = {c: i for c, (_, i) in sw.items()}
        if ex_id == "binance":
            spot = {canon(v["base"]) for v in m.values() if v.get("spot") and v.get("quote") == "USDT"
                    and v.get("active") is not False}
    count = {}
    for ex_id in VENUES:
        for c in markets[ex_id]:
            count[c] = count.get(c, 0) + 1
    common = {c for c, n in count.items() if n >= 2}
    return ({ex: {c: s for c, s in mk.items() if c in common} for ex, mk in markets.items()},
            ids, spot)


# ── Persistencia incremental ─────────────────────────────────────────────────
def _path(ex_id):
    return OUT / f"fr_{ex_id}.csv"


def last_ts(ex_id) -> dict:
    p = _path(ex_id)
    if not p.exists():
        return {}
    d = pd.read_csv(p, usecols=["symbol", "funding_ts"])
    return d.groupby("symbol")["funding_ts"].max().to_dict()


def append_rows(ex_id, rows):
    if not rows:
        return
    with _lock:
        p = _path(ex_id)
        new = not p.exists()
        with open(p, "a", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            if new:
                w.writerow(COLS)
            w.writerows(rows)


def finalize(ex_id):
    """Ordena, deduplica y recalcula interval_hours = Δ al pago anterior (1–8 h)."""
    p = _path(ex_id)
    if not p.exists():
        return 0
    d = pd.read_csv(p).drop_duplicates(["symbol", "funding_ts"], keep="last")
    d = d.sort_values(["symbol", "funding_ts"])
    diff = d.groupby("symbol")["funding_ts"].diff() / H
    nxt = d.groupby("symbol")["funding_ts"].diff(-1).abs() / H
    ih = diff.fillna(nxt).round().clip(1, 8).fillna(8)
    d["interval_hours"] = ih.astype(int)
    d.to_csv(p, index=False)
    return len(d)


def _row(sym, ex_id, rate, ts, has_spot, vol=0.0):
    ts = int(ts)
    return [sym, ex_id, float(rate), vol, 8, ts,
            datetime.fromtimestamp(ts / 1000, timezone.utc).isoformat(), int(has_spot)]


# ── Descargas por venue ──────────────────────────────────────────────────────
def ccxt_forward(ex_id, canon_sym, ccxt_sym, since, has_spot, limit):
    """Pagina hacia adelante desde `since` (Binance, Bybit, OKX reciente)."""
    ex = _client(ex_id)
    rows, cur = [], since
    # Bybit devuelve vacío si startTime es anterior al listado → arrancar en launchTime.
    launch = int((ex.market(ccxt_sym).get("info") or {}).get("launchTime") or 0)
    cur = max(cur, launch)
    now = int(time.time() * 1000)
    while True:
        page = _retry(lambda: ex.fetch_funding_rate_history(ccxt_sym, since=cur, limit=limit))
        page = [x for x in page if x["timestamp"] and x["timestamp"] >= cur]
        if not page:
            if cur < now - 2 * 86_400_000:          # hueco (deslistado/pausa) → saltar 15 d
                cur += 15 * 86_400_000
                continue
            break
        rows += [_row(canon_sym, ex_id, x["fundingRate"], x["timestamp"], has_spot) for x in page
                 if x.get("fundingRate") is not None]
        nxt = page[-1]["timestamp"] + 1
        if nxt <= cur or len(page) < 2:
            break
        cur = nxt
    return rows


def fetch_bitget(canon_sym, ccxt_sym, since, has_spot):
    """API v2 por páginas (más reciente primero); el historial público es ~90 d."""
    ex = _client("bitget")
    rows = []
    for page_no in range(1, 50):
        page = _retry(lambda: ex.fetch_funding_rate_history(ccxt_sym, limit=100, params={"pageNo": page_no}))
        if not page:
            break
        rows += [_row(canon_sym, "bitget", x["fundingRate"], x["timestamp"], has_spot)
                 for x in page if x["timestamp"] > since]
        if min(x["timestamp"] for x in page) <= since or len(page) < 100:
            break
    return rows


def okx_months(inst_family, start_ms, end_ms):
    """URLs de los archivos mensuales de funding de OKX entre start y end."""
    r = _get("https://www.okx.com/api/v5/public/market-data-history",
             {"module": "3", "instType": "SWAP", "instFamilyList": inst_family,
              "dateAggrType": "monthly", "begin": str(start_ms), "end": str(end_ms)})
    urls = []
    for blk in (r.json().get("data") or []) if r is not None else []:
        for det in blk.get("details") or []:
            urls += [g["url"] for g in det.get("groupDetails") or []]
    return urls


def fetch_okx(canon_sym, ccxt_sym, inst_id, since, has_spot):
    rows = []
    fam = inst_id.replace("-SWAP", "")
    now = int(time.time() * 1000)
    # Archivos mensuales (el endpoint acepta rangos de pocos meses → bloques de 3).
    blk = since
    while blk < now - 80 * 86_400_000:
        end = min(blk + 92 * 86_400_000, now)
        for url in okx_months(fam, blk, end):
            r = _get(url)
            if r is None:
                continue
            z = zipfile.ZipFile(io.BytesIO(r.content))
            for line in z.open(z.namelist()[0]).read().decode("utf-8", "replace").splitlines()[1:]:
                parts = line.split(",")
                if (len(parts) >= 3 and parts[2].strip().isdigit() and int(parts[2]) > since
                        and re.fullmatch(r"-?[0-9.eE+-]+", parts[1].strip())):
                    rows.append(_row(canon_sym, "okx", parts[1], parts[2], has_spot))
        blk = end
    tail_since = max([since] + [r[5] for r in rows]) + 1
    rows += ccxt_forward("okx", canon_sym, ccxt_sym, max(tail_since, now - 85 * 86_400_000), has_spot, 100)
    return rows


def binance_local(start_ms, spot):
    """Filas de Binance desde el CSV de binance_dataset.py (con deslistados)."""
    if not BINANCE_CSV.exists():
        return None
    d = pd.read_csv(BINANCE_CSV, usecols=["symbol", "rate", "volume_24h", "interval_hours", "funding_ts",
                                          "captured_at", "has_spot"])
    d = d[d["funding_ts"] >= start_ms].copy()
    d["symbol"] = d["symbol"].str.replace("USDT$", "", regex=True).map(canon)
    d["exchange"] = "binance"
    return d[COLS]


# ── Klines 1d ────────────────────────────────────────────────────────────────
def fetch_klines(canon_sym, src, native_id, since):
    rows, cur = [], since
    while True:
        if src == "binance":
            r = _get("https://fapi.binance.com/fapi/v1/klines",
                     {"symbol": native_id, "interval": "1d", "startTime": cur, "limit": 1000})
            data = r.json() if r is not None else []
            page = [(int(k[0]), *map(float, k[1:5]), float(k[7])) for k in data]
        else:
            r = _get("https://api.bybit.com/v5/market/kline",
                     {"category": "linear", "symbol": native_id, "interval": "D", "start": cur, "limit": 1000})
            data = ((r.json().get("result") or {}).get("list") or []) if r is not None else []
            page = sorted((int(k[0]), *map(float, k[1:5]), float(k[6])) for k in data)
        page = [p for p in page if p[0] >= cur]
        if not page:
            break
        rows += [[canon_sym, *p, src] for p in page]
        if len(page) < 999:
            break
        cur = page[-1][0] + 1
    return rows


def update_klines(universe, ids, start_ms, workers):
    done = {}
    if KLINES.exists():
        k = pd.read_csv(KLINES)
        done = k.groupby("symbol")["day_ts"].max().to_dict()
    syms = sorted(set().union(*[set(m) for m in universe.values()]))
    jobs = []
    for c in syms:
        if c in universe["binance"]:
            src, nid = "binance", ids["binance"][c]
        elif c in universe["bybit"]:
            src, nid = "bybit", ids["bybit"][c]
        else:
            continue
        jobs.append((c, src, nid, int(done.get(c, start_ms - 1)) + 1))
    # Universo spot_perp: todos los perps Binance del dataset local (fapi responde 400
    # para los deslistados → sin klines; el backtest los cuenta como "sin precio").
    if BINANCE_CSV.exists():
        listed = {j[0] for j in jobs}
        loc = pd.read_csv(BINANCE_CSV, usecols=["symbol", "funding_ts"])
        for base in sorted(loc.loc[loc["funding_ts"] >= start_ms, "symbol"].unique()):
            c = canon(base)                          # el CSV guarda la base sin "USDT"
            if c not in listed:
                listed.add(c)
                jobs.append((c, "binance", f"{base}USDT", int(done.get(c, start_ms - 1)) + 1))

    def safe(j):
        try:
            return _retry(lambda: fetch_klines(*j))
        except Exception as e:                       # noqa: BLE001
            print(f"  klines {j[0]}: {str(e)[:100]}", flush=True)
            return []

    out = []
    with ThreadPoolExecutor(workers) as pool:
        for rows in pool.map(safe, jobs):
            out += rows
    new = pd.DataFrame(out, columns=["symbol", "day_ts", "open", "high", "low", "close", "quote_volume", "src"])
    if KLINES.exists():
        new = pd.concat([pd.read_csv(KLINES), new])
    new = new.drop_duplicates(["symbol", "day_ts"], keep="last").sort_values(["symbol", "day_ts"])
    new.to_csv(KLINES, index=False)
    return len(new)


# ── Main ─────────────────────────────────────────────────────────────────────
def run_venue(ex_id, universe, ids, spot, start_ms, workers):
    t0 = time.time()
    mk = universe[ex_id]
    prev = last_ts(ex_id)
    if ex_id == "binance" and not prev:
        local = binance_local(start_ms, spot)
        if local is not None:
            local = local[local["symbol"].isin(set().union(*[set(m) for m in universe.values()]))]
            append_rows(ex_id, local.values.tolist())
            prev = last_ts(ex_id)

    def one(c):
        since = int(prev.get(c, start_ms - 1)) + 1
        hs = c in spot
        if ex_id == "bybit":
            return ccxt_forward("bybit", c, mk[c], since, hs, 200)
        if ex_id == "binance":
            return ccxt_forward("binance", c, mk[c], since, hs, 1000)
        if ex_id == "okx":
            return fetch_okx(c, mk[c], ids["okx"][c], since, hs)
        return fetch_bitget(c, mk[c], since, hs)

    n, errs = 0, 0
    with ThreadPoolExecutor(workers) as pool:
        futs = {pool.submit(one, c): c for c in sorted(mk)}
        for k, f in enumerate(futs, 1):
            try:
                rows = f.result()
                append_rows(ex_id, rows)
                n += len(rows)
            except Exception as e:                   # noqa: BLE001
                errs += 1
                print(f"  {ex_id} {futs[f]}: {str(e)[:120]}", flush=True)
            if k % 100 == 0:
                print(f"  {ex_id}: {k}/{len(futs)} símbolos, +{n:,} filas ({time.time() - t0:.0f}s)", flush=True)
    total = finalize(ex_id)
    print(f"{ex_id}: +{n:,} filas nuevas, total {total:,}, errores {errs} ({time.time() - t0:.0f}s)", flush=True)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--start", default="2025-01")
    ap.add_argument("--only", default=",".join(VENUES))
    ap.add_argument("--workers", type=int, default=4)
    ap.add_argument("--no-klines", action="store_true")
    ap.add_argument("--klines-only", action="store_true")
    ap.add_argument("--symbols", default="", help="CSV de símbolos canónicos (prueba)")
    a = ap.parse_args()
    start_ms = int(datetime.strptime(a.start, "%Y-%m").replace(tzinfo=timezone.utc).timestamp() * 1000)
    OUT.mkdir(parents=True, exist_ok=True)

    universe, ids, spot = load_universe()
    if a.symbols:
        keep = set(a.symbols.split(","))
        universe = {ex: {c: s for c, s in mk.items() if c in keep} for ex, mk in universe.items()}
    print("universo (≥2 venues): " + ", ".join(f"{ex}={len(universe[ex])}" for ex in VENUES), flush=True)

    venues = [] if a.klines_only else [v for v in a.only.split(",") if v in VENUES]
    with ThreadPoolExecutor(max(1, len(venues))) as pool:   # un hilo por venue, rate limit propio
        list(pool.map(lambda v: run_venue(v, universe, ids, spot, start_ms, a.workers), venues))
    if not a.no_klines:
        print(f"klines 1d: {update_klines(universe, ids, start_ms, a.workers):,} filas", flush=True)


if __name__ == "__main__":
    main()
