#!/usr/bin/env python3
"""Dataset local de funding de Binance (USDⓈ-M) para entrenar el modelo — LOCAL.

Por qué: los snapshots de la DB solo cubren 90 días, tienen huecos (el scanner
event-driven no escribe sin posiciones) y guardan ESTIMACIONES tempranas, no
tasas liquidadas. Binance publica el historial completo:

  * data.binance.vision  futures/um/monthly/fundingRate  (tasa liquidada + intervalo real)
                         futures/um/monthly/klines/1d    (quote volume diario del perp)
                         spot/monthly/klines/1d          (meses con spot → has_spot)
    983 símbolos INCLUIDOS los deslistados (sin sesgo de supervivencia), sin rate limit.
  * fapi /fapi/v1/fundingRate y /fapi/v1/klines para el mes en curso (aún no publicado).

Salida: cache/binance/fr_binance.csv — mismo contrato que
scripts/_scoring_data.py:load_fr_snapshots (symbol, exchange, rate, volume_24h,
interval_hours, funding_ts, captured_at) + has_spot. Una fila = una liquidación;
`rate` = tasa LIQUIDADA; `captured_at` = hora del settlement; `volume_24h` =
quote volume del perp del día UTC ANTERIOR (sin mirar adelante).

Read-only contra APIs públicas, reanudable (los meses ya bajados no se repiten).
Uso:
    python scripts/binance_dataset.py                 # desde 2024-01
    python scripts/binance_dataset.py --start 2023-01 --workers 24
"""
import argparse
import csv
import io
import re
import sys
import time
import zipfile
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parent.parent
OUT_DIR = ROOT / "cache" / "binance"
RAW = OUT_DIR / "raw"
OUT_CSV = OUT_DIR / "fr_binance.csv"

S3 = "https://s3-ap-northeast-1.amazonaws.com/data.binance.vision"
VISION = "https://data.binance.vision"
FAPI = "https://fapi.binance.com"
SAPI = "https://api.binance.com"

_session = requests.Session()
_session.headers["User-Agent"] = "basyo-dataset/1.0"


def _get(url, params=None, timeout=30, tries=4):
    for k in range(tries):
        try:
            r = _session.get(url, params=params, timeout=timeout)
            if r.status_code == 404:
                return None
            if r.status_code in (418, 429):
                time.sleep(30 * (k + 1))
                continue
            r.raise_for_status()
            return r
        except requests.RequestException:
            time.sleep(2 * (k + 1))
    raise RuntimeError(f"GET falló tras {tries} intentos: {url} {params or ''}")


def s3_keys(prefix: str) -> list:
    """Todas las keys .zip bajo `prefix` (pagina con marker)."""
    keys, marker = [], ""
    while True:
        r = _get(S3, params={"prefix": prefix, "marker": marker} if marker else {"prefix": prefix})
        if r is None:
            return keys
        txt = r.text
        page = re.findall(r"<Key>([^<]+\.zip)</Key>", txt)
        keys += page
        if "<IsTruncated>true</IsTruncated>" not in txt or not page:
            return keys
        marker = page[-1]


def s3_symbols() -> list:
    r = _get(S3, params={"delimiter": "/", "prefix": "data/futures/um/monthly/fundingRate/"})
    syms = re.findall(r"fundingRate/([A-Z0-9]+)/</Prefix>", r.text)
    return sorted(s for s in set(syms) if s.endswith("USDT"))


def _month_of(key: str) -> str:
    m = re.search(r"(\d{4}-\d{2})\.zip$", key)
    return m.group(1) if m else ""


def _unzip_csv(content: bytes) -> list:
    z = zipfile.ZipFile(io.BytesIO(content))
    rows = list(csv.reader(io.TextIOWrapper(z.open(z.namelist()[0]), encoding="utf-8")))
    if rows and rows[0] and not rows[0][0].isdigit():
        rows = rows[1:]                                # header (los CSV viejos no lo traen)
    return rows


def _fetch_month(sym: str, kind: str, key: str, start: str) -> bool:
    month = _month_of(key)
    if not month or month < start:
        return False
    dest = RAW / kind / sym / f"{month}.csv"
    if dest.exists():
        return False
    r = _get(f"{VISION}/{key}")
    if r is None:
        return False
    rows = _unzip_csv(r.content)
    dest.parent.mkdir(parents=True, exist_ok=True)
    with open(dest, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        if kind == "fr":           # calc_time, funding_interval_hours, last_funding_rate
            for row in rows:
                w.writerow(row[:3])
        else:                      # klines: open_time, quote_volume
            for row in rows:
                w.writerow([row[0], row[7]])
    return True


def download_vision(symbols: list, start: str, workers: int):
    """fundingRate + klines 1d del perp (meses ≥ start) y meses con spot."""
    spot_months = {}
    jobs = []

    def plan(sym):
        fr = s3_keys(f"data/futures/um/monthly/fundingRate/{sym}/")
        kl = s3_keys(f"data/futures/um/monthly/klines/{sym}/1d/")
        sp = s3_keys(f"data/spot/monthly/klines/{sym}/1d/")
        return sym, fr, kl, sorted({_month_of(k) for k in sp})

    t0 = time.time()
    with ThreadPoolExecutor(max_workers=workers) as pool:
        for fut in as_completed([pool.submit(plan, s) for s in symbols]):
            sym, fr, kl, sp = fut.result()
            spot_months[sym] = sp
            jobs += [(sym, "fr", k) for k in fr] + [(sym, "kl", k) for k in kl]
    print(f"plan: {len(symbols)} símbolos, {len(jobs)} archivos mensuales ({time.time() - t0:.0f}s)",
          flush=True)
    done = 0
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futs = [pool.submit(_fetch_month, s, kind, k, start) for s, kind, k in jobs]
        for i, fut in enumerate(as_completed(futs), 1):
            done += bool(fut.result())
            if i % 2000 == 0:
                print(f"  {i}/{len(futs)} revisados, {done} descargados", flush=True)
    print(f"vision: {done} archivos nuevos ({time.time() - t0:.0f}s)", flush=True)
    return spot_months


def download_current_month(start_ms: int):
    """Mes en curso por fapi (aún no publicado en vision) para los perps activos."""
    info = _get(f"{FAPI}/fapi/v1/exchangeInfo").json()
    live = [s["symbol"] for s in info["symbols"] if s.get("contractType") == "PERPETUAL"
            and s.get("status") == "TRADING" and s.get("quoteAsset") == "USDT"]
    spot = _get(f"{SAPI}/api/v3/exchangeInfo", params={"permissions": "SPOT"}).json()
    spot_live = {s["symbol"] for s in spot["symbols"] if s.get("status") == "TRADING"}
    month = datetime.fromtimestamp(start_ms / 1000, tz=timezone.utc).strftime("%Y-%m")
    for i, sym in enumerate(live, 1):
        fr_rows, cursor = [], start_ms
        while True:   # /fundingRate: 500 req / 5 min / IP → ~0.65 s entre llamadas
            r = _get(f"{FAPI}/fapi/v1/fundingRate",
                     params={"symbol": sym, "startTime": cursor, "limit": 1000})
            time.sleep(0.65)
            data = r.json() if r is not None else []
            fr_rows += [[d["fundingTime"], "", d["fundingRate"]] for d in data]
            if len(data) < 1000:
                break
            cursor = int(data[-1]["fundingTime"]) + 1
        kl = _get(f"{FAPI}/fapi/v1/klines", params={"symbol": sym, "interval": "1d",
                                                    "startTime": start_ms, "limit": 40})
        kl_rows = [[k[0], k[7]] for k in (kl.json() if kl is not None else [])]
        for kind, rows in (("fr", fr_rows), ("kl", kl_rows)):
            dest = RAW / kind / sym / f"{month}.csv"
            dest.parent.mkdir(parents=True, exist_ok=True)
            with open(dest, "w", newline="", encoding="utf-8") as f:
                csv.writer(f).writerows(rows)
        if i % 100 == 0:
            print(f"  fapi {i}/{len(live)}", flush=True)
    return spot_live, month


def _interval_from_diffs(ts: list) -> list:
    out = []
    for i, t in enumerate(ts):
        if i == 0:
            d = (ts[1] - t) if len(ts) > 1 else 8 * 3600_000
        else:
            d = t - ts[i - 1]
        h = d / 3600_000
        out.append(min((1, 2, 4, 8), key=lambda x: abs(x - h)))
    return out


def build_csv(start: str, spot_months: dict, spot_live: set, cur_month: str):
    rows_out = 0
    fr_root = RAW / "fr"
    with open(OUT_CSV, "w", newline="", encoding="utf-8") as fo:
        w = csv.writer(fo)
        w.writerow(["symbol", "exchange", "rate", "volume_24h", "interval_hours",
                    "funding_ts", "captured_at", "has_spot"])
        for sym_dir in sorted(fr_root.iterdir()):
            sym = sym_dir.name
            recs = []
            for f in sorted(sym_dir.glob("*.csv")):
                if f.stem < start:
                    continue
                for row in csv.reader(open(f, encoding="utf-8")):
                    if len(row) >= 3 and row[0]:
                        recs.append((int(float(row[0])), row[1], float(row[2])))
            if not recs:
                continue
            recs.sort()
            recs = [r for i, r in enumerate(recs) if i == 0 or r[0] != recs[i - 1][0]]
            ts = [r[0] for r in recs]
            iv_fallback = _interval_from_diffs(ts)
            vol_by_day = {}
            for f in (RAW / "kl" / sym).glob("*.csv") if (RAW / "kl" / sym).exists() else []:
                for row in csv.reader(open(f, encoding="utf-8")):
                    if len(row) >= 2 and row[0]:
                        vol_by_day[int(float(row[0])) // 86400_000] = float(row[1] or 0)
            sp = set(spot_months.get(sym, []))
            base = sym[:-4]
            for (t, ih_raw, rate), ih_fb in zip(recs, iv_fallback):
                ih = int(float(ih_raw)) if ih_raw not in ("", None) else ih_fb
                day = t // 86400_000
                vol = vol_by_day.get(day - 1, vol_by_day.get(day, 0.0))   # día UTC anterior
                month = datetime.fromtimestamp(t / 1000, tz=timezone.utc).strftime("%Y-%m")
                has_spot = (month in sp) or (month == cur_month and sym in spot_live)
                cap = datetime.fromtimestamp(t / 1000, tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
                w.writerow([base, "Binance", rate, vol, ih, t, cap, int(has_spot)])
                rows_out += 1
    print(f"dataset: {rows_out:,} liquidaciones → {OUT_CSV}", flush=True)


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--start", default="2024-01", help="primer mes (YYYY-MM)")
    ap.add_argument("--workers", type=int, default=24)
    ap.add_argument("--skip-download", action="store_true", help="solo reconstruir el CSV")
    a = ap.parse_args()
    RAW.mkdir(parents=True, exist_ok=True)
    now = datetime.now(timezone.utc)
    cur_month = now.strftime("%Y-%m")
    month_start_ms = int(datetime(now.year, now.month, 1, tzinfo=timezone.utc).timestamp() * 1000)
    spot_cache = OUT_DIR / "spot_months.csv"
    if a.skip_download:
        spot_months = {}
        if spot_cache.exists():
            for row in csv.reader(open(spot_cache, encoding="utf-8")):
                spot_months[row[0]] = row[1:]
        live_f = OUT_DIR / "spot_live.txt"
        spot_live = set(live_f.read_text(encoding="utf-8").split()) if live_f.exists() else set()
    else:
        symbols = s3_symbols()
        print(f"símbolos USDT con historial en vision: {len(symbols)}", flush=True)
        spot_months = download_vision(symbols, a.start, a.workers)
        with open(spot_cache, "w", newline="", encoding="utf-8") as f:
            csv.writer(f).writerows([[s, *m] for s, m in spot_months.items()])
        spot_live, _ = download_current_month(month_start_ms)
        (OUT_DIR / "spot_live.txt").write_text(" ".join(sorted(spot_live)), encoding="utf-8")
    build_csv(a.start, spot_months, spot_live, cur_month)


if __name__ == "__main__":
    sys.exit(main())
