#!/usr/bin/env python3
"""Dataset horario de señales (basis, OI, top long/short, volatilidad) — LOCAL, read-only.

La API /futures/data solo guarda ~30 d. Para tener historia sin esperar a que prod la
acumule se reconstruye desde fuentes públicas con la misma semántica de
market_feature_snapshots (exchanges/market_features.py):
  * OI (contratos y USD) y top trader long/short (posición): data.binance.vision
    futures/um/daily/metrics (cada 5 min). Verificado contra la tabla (2026-11-06):
    sum_open_interest = open_interest (corr 0.995), sum_toptrader_long_short_ratio =
    top_long_short_pos_ratio (corr 0.97–0.998). El taker de vision es un punto de 5 min
    (prod: la hora) → no se usa (IC ~0 en la sonda del 25-oct).
  * basis CEX: cierres 1h perp vs spot (como scripts/market_features_backfill.py).
  * basis Alpha (spot solo on-chain BSC): VWAP 1h on-chain (API pública Binance Alpha)
    vs VWAP 1h del perp (prod: VWAP de 5 velas 1m; los features usan la media de 24 h).
  * volatility_7d: vol_7d() sobre cierres diarios del perp.
Universo: símbolos de cache/binance/fr_binance.csv con algún settlement en la ventana con
tasa diaria ≥ base y volumen ≥ mínimo, con spot CEX o token Alpha BSC.

Salida: cache/binance/signals_1h.csv (symbol, ts_hour, spot_source, basis_pct,
open_interest, open_interest_usd, top_long_short_pos_ratio, volatility_7d).
`--check` compara los features de analysis/signal_features.py offline vs la tabla de prod
(DATABASE_URL, solo lectura) en las horas que se solapan.

    python -u scripts/signals_dataset.py --days 105
    python -u scripts/signals_dataset.py --check
"""
import argparse
import csv
import io
import json
import sys
import time
import zipfile
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from exchanges.market_features import ALPHA_KLINES_URL, basis_pct, vol_7d, vwap, _f  # noqa: E402

FR_CSV = ROOT / "cache" / "binance" / "fr_binance.csv"
ALPHA_JSON = ROOT / "cache" / "binance" / "alpha_tokens.json"
RAW = ROOT / "cache" / "binance" / "raw" / "metrics"
OUT = ROOT / "cache" / "binance" / "signals_1h.csv"
COLS = ["symbol", "ts_hour", "spot_source", "basis_pct", "open_interest", "open_interest_usd",
        "top_long_short_pos_ratio", "volatility_7d"]
VISION = "https://data.binance.vision/data/futures/um/daily/metrics"
FAPI, SAPI = "https://fapi.binance.com", "https://api.binance.com"
H_MS, DAY_MS = 3_600_000, 86_400_000
BASE_DAILY, MIN_VOLUME = 0.0003, 100_000

_s = requests.Session()
_s.headers["User-Agent"] = "basyo-dataset/1.0"


def _get(url, params=None, tries=4):
    for k in range(tries):
        try:
            r = _s.get(url, params=params, timeout=30)
            if r.status_code in (418, 429):
                time.sleep(30 * (k + 1))
                continue
            if r.status_code in (400, 404):
                return None
            r.raise_for_status()
            return r
        except requests.RequestException:
            time.sleep(2 * (k + 1))
    return None


# ── Universo ────────────────────────────────────────────────────────────────
def universe(start_ms: int):
    import pandas as pd
    fr = pd.read_csv(FR_CSV, usecols=["symbol", "rate", "volume_24h", "interval_hours", "funding_ts", "has_spot"])
    fr = fr[fr["funding_ts"] >= start_ms]
    daily = fr["rate"] * 24 / fr["interval_hours"].clip(lower=1)
    hot = fr[(daily >= BASE_DAILY * (1 - 1e-6)) & (fr["volume_24h"] >= MIN_VOLUME)]
    has_spot = fr.groupby("symbol")["has_spot"].max()
    alpha = {}
    for t in json.loads(ALPHA_JSON.read_text(encoding="utf-8")):
        sym = (t.get("symbol") or "").upper()
        if str(t.get("chainId")) == "56" and t.get("alphaId"):
            alpha.setdefault(sym, t["alphaId"])
    out = {}
    for sym in sorted(hot["symbol"].unique()):
        if has_spot.get(sym, 0):
            out[sym] = None
        elif sym in alpha:
            out[sym] = alpha[sym]
    return out


# ── Fuentes ─────────────────────────────────────────────────────────────────
def metrics_day(sym: str, day: str):
    """Ruta del zip del día (caché en disco); '' si no existe en vision."""
    d = RAW / sym
    zp, miss = d / f"{day}.zip", d / f"{day}.missing"
    if zp.exists():
        return zp
    if miss.exists():
        return None
    r = _get(f"{VISION}/{sym}USDT/{sym}USDT-metrics-{day}.zip")
    d.mkdir(parents=True, exist_ok=True)
    if r is None:
        if day < (datetime.now(timezone.utc) - timedelta(days=3)).strftime("%Y-%m-%d"):
            miss.touch()          # los días recientes pueden publicarse más tarde
        return None
    zp.write_bytes(r.content)
    return zp


def metrics_hourly(sym: str, days: list) -> dict:
    """{ts_hour_s: (oi, oi_usd, top_ls)} con el punto de 5 min más cercano (≤ 5 min) a la hora."""
    pts = {}
    for day in days:
        zp = RAW / sym / f"{day}.zip"
        if not zp.exists():
            continue
        try:
            z = zipfile.ZipFile(zp)
            rows = csv.DictReader(io.TextIOWrapper(z.open(z.namelist()[0]), encoding="utf-8"))
            for r in rows:
                ts = int(datetime.strptime(r["create_time"], "%Y-%m-%d %H:%M:%S")
                         .replace(tzinfo=timezone.utc).timestamp())
                pts[ts] = (_f(r.get("sum_open_interest")), _f(r.get("sum_open_interest_value")),
                           _f(r.get("sum_toptrader_long_short_ratio")))
        except (zipfile.BadZipFile, KeyError, ValueError):
            continue
    out = {}
    for ts in pts:
        h = round(ts / 3600) * 3600
        if abs(ts - h) <= 300 and (h not in out or abs(ts - h) < abs(out[h][0] - h)):
            out[h] = (ts, pts[ts])
    return {h: v[1] for h, v in out.items()}


def klines(url, params, start_ms, end_ms, limit, unwrap=False):
    """{open_time_ms: vela} 1h paginadas."""
    out, t = {}, start_ms
    while t < end_ms:
        r = _get(url, {**params, "interval": "1h", "limit": limit, "startTime": t, "endTime": end_ms - 1})
        if r is None:
            break
        rows = (r.json().get("data") or []) if unwrap else r.json()
        if not rows:
            break
        for k in rows:
            out[int(k[0])] = k
        t = int(rows[-1][0]) + H_MS
        if len(rows) < limit:
            break
    return out


def symbol_rows(sym, alpha_id, start_ms, end_ms, days):
    w0 = start_ms - 10 * DAY_MS
    perp = klines(f"{FAPI}/fapi/v1/klines", {"symbol": f"{sym}USDT"}, w0, end_ms, 1500)
    if not perp:
        return []
    if alpha_id:
        spot = klines(ALPHA_KLINES_URL, {"symbol": f"{alpha_id}USDT"}, start_ms - H_MS, end_ms, 500, unwrap=True)
    else:
        spot = klines(f"{SAPI}/api/v3/klines", {"symbol": f"{sym}USDT"}, start_ms - H_MS, end_ms, 1000)
    met = metrics_hourly(sym, days)
    daily = {t // DAY_MS * DAY_MS: _f(k[4]) for t, k in perp.items() if (t + H_MS) % DAY_MS == 0}
    dkeys = sorted(daily)
    rows = []
    for ts in range(start_ms, end_ms, H_MS):
        prev = ts - H_MS                       # vela [ts−1h, ts) → precio en ts
        p, s = perp.get(prev), spot.get(prev)
        if alpha_id:
            b = basis_pct(vwap([s]), vwap([p])) if p and s and _f(s[5]) else None
        else:
            b = basis_pct(s[4], p[4]) if p and s else None
        oi, oi_usd, top = met.get(ts // 1000, (None, None, None))
        day0 = ts // DAY_MS * DAY_MS
        vol = vol_7d([daily[d] for d in dkeys if d < day0])
        if b is None and oi is None:
            continue
        rows.append({"symbol": sym, "ts_hour": ts // 1000, "spot_source": "alpha" if alpha_id else "cex",
                     "basis_pct": b, "open_interest": oi, "open_interest_usd": oi_usd,
                     "top_long_short_pos_ratio": top, "volatility_7d": vol})
    return rows


# ── Paridad vs prod ─────────────────────────────────────────────────────────
def check():
    """Correlación de los features offline vs los de market_feature_snapshots (DB, read-only)."""
    import os
    import re
    import pandas as pd
    import sqlalchemy as sa
    from dotenv import load_dotenv
    from analysis.signal_features import SIGNAL_FEATURES, signal_features
    load_dotenv(ROOT / ".env")
    url = re.sub(r"^postgres(ql)?://", "postgresql+psycopg2://", os.environ["DATABASE_URL"])
    with sa.create_engine(url).connect() as c:
        c.execute(sa.text("SET TRANSACTION READ ONLY"))
        db = pd.read_sql(sa.text(
            "select symbol, ts_hour, spot_source, basis_pct, open_interest, open_interest_usd, "
            "top_long_short_pos_ratio, volatility_7d from market_feature_snapshots "
            "where exchange='Binance'"), c)
    off = pd.read_csv(OUT)
    common = sorted(set(db["symbol"]) & set(off["symbol"]))
    lo = max(db["ts_hour"].min(), off["ts_hour"].min()) + 3 * 86400
    hi = min(db["ts_hour"].max(), off["ts_hour"].max())
    pairs = {k: [] for k in SIGNAL_FEATURES}
    for sym in common:
        a = db[db["symbol"] == sym].sort_values("ts_hour").to_dict("records")
        b = off[off["symbol"] == sym].sort_values("ts_hour").to_dict("records")
        src = a[-1]["spot_source"] if a else "?"
        for t in range(int(lo) // 28800 * 28800, int(hi), 28800):      # cada 8 h
            fa = signal_features([r for r in a if r["ts_hour"] <= t], t)
            fb = signal_features([r for r in b if r["ts_hour"] <= t], t)
            for k in SIGNAL_FEATURES:
                if fa[k] is not None and fb[k] is not None:
                    pairs[k].append((fa[k], fb[k], src))
    print(f"paridad prod vs offline: {len(common)} símbolos, {datetime.fromtimestamp(lo, timezone.utc):%Y-%m-%d} → "
          f"{datetime.fromtimestamp(hi, timezone.utc):%Y-%m-%d}")
    res = {}
    for k, v in pairs.items():
        if len(v) < 30:
            print(f"  {k}: n={len(v)} (insuficiente)")
            continue
        d = pd.DataFrame(v, columns=["prod", "off", "src"])
        res[k] = {s: (round(g["prod"].corr(g["off"], method="spearman"), 3), len(g))
                  for s, g in [("todo", d), *d.groupby("src")]}
        print(f"  {k}: " + ", ".join(f"{s} ρ={r} (n={n})" for s, (r, n) in res[k].items()))
    return res


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=105)
    ap.add_argument("--workers", type=int, default=24)
    ap.add_argument("--symbols", default="")
    ap.add_argument("--check", action="store_true", help="solo comparar contra la tabla de prod")
    a = ap.parse_args()
    sys.stdout.reconfigure(encoding="utf-8")
    if a.check:
        check()
        return
    end_ms = int(time.time() * 1000) // H_MS * H_MS
    start_ms = (end_ms - a.days * DAY_MS) // DAY_MS * DAY_MS
    uni = universe(start_ms)
    if a.symbols:
        want = {s.strip().upper() for s in a.symbols.split(",")}
        uni = {k: v for k, v in uni.items() if k in want}
    days = [(datetime.fromtimestamp(start_ms / 1000, timezone.utc) - timedelta(days=1) + timedelta(days=i))
            .strftime("%Y-%m-%d") for i in range(a.days + 2)]
    print(f"{len(uni)} símbolos ({sum(1 for v in uni.values() if v)} Alpha), {a.days} d, "
          f"{len(uni) * len(days):,} archivos de metrics", flush=True)

    t0 = time.time()
    jobs = [(s, d) for s in uni for d in days]
    with ThreadPoolExecutor(a.workers) as ex:
        for i, _ in enumerate(ex.map(lambda j: metrics_day(*j), jobs), 1):
            if i % 5000 == 0:
                print(f"  metrics {i:,}/{len(jobs):,} ({time.time() - t0:.0f}s)", flush=True)

    rows, done = [], 0
    with ThreadPoolExecutor(8) as ex:
        futs = {s: ex.submit(symbol_rows, s, aid, start_ms, end_ms, days) for s, aid in uni.items()}
        for s, f in futs.items():
            try:
                rows += f.result()
            except Exception as e:  # noqa: BLE001
                print(f"  {s}: ERROR {e}", flush=True)
            done += 1
            if done % 50 == 0:
                print(f"  velas {done}/{len(uni)} ({time.time() - t0:.0f}s)", flush=True)

    with OUT.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=COLS)
        w.writeheader()
        for r in rows:
            w.writerow({c: ("" if r.get(c) is None else r[c]) for c in COLS})
    n_sym = len({r["symbol"] for r in rows})
    cov = {c: sum(r[c] is not None for r in rows) / max(1, len(rows)) for c in COLS[3:]}
    print(f"OK {len(rows):,} filas, {n_sym} símbolos -> {OUT} ({time.time() - t0:.0f}s)")
    print("cobertura: " + ", ".join(f"{c} {v:.0%}" for c, v in cov.items()))


if __name__ == "__main__":
    main()
