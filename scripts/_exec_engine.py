#!/usr/bin/env python3
"""Motor único de simulación: spot on-chain (BSC/Alpha) + short perp Binance — LOCAL, read-only.

Una posición completa por intento: entrada por tramos → hold con seguros (SL del short +
venta límite Kyber) → salida por tramos. Lo usa scripts/exec_study.py.

Datos: velas 1m del perp (last y MARK, data.binance.vision) y on-chain (API pública de Binance
Alpha), funding liquidado (cache/binance/fr_binance.csv) y costo por token (comisión del pool +
pendiente de impacto, cotizaciones Kyber → cache/binance/alpha_cost.csv).

Modelo:
  * Solo cuentan las velas on-chain con trades (la API repite el cierre en las planas).
  * Cada tramo paga comisión + impacto (a + b·USD) + el empuje de los tramos previos, que decae ρ/min.
  * Umbral sobre el basis ejecutable al tamaño del tramo; se llena en la vela siguiente a la lectura.
  * Liquidación del short aislado: PE·(1 + 1/L)/(1 + mmr), mmr 5 % (ARIA #56).
  * PnL = Σ (venta spot / costo spot − recompra perp / venta perp) + funding, en % del nocional objetivo.
"""
import io
import json
import math
import sys
import time
import zipfile
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import pandas as pd
import requests

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
from binance_dataset import OUT_CSV, VISION, _get  # noqa: E402

KL_DIR = ROOT / "cache" / "binance" / "klines1m"
COST_CSV = ROOT / "cache" / "binance" / "alpha_cost.csv"
TOKENS = ROOT / "cache" / "binance" / "alpha_tokens.json"
KINDS = {"perp": "futures/um/monthly/klines", "mark": "futures/um/monthly/markPriceKlines"}
ALPHA_LIST_URL = "https://www.binance.com/bapi/defi/v1/public/wallet-direct/buw/wallet/cex/alpha/all/token/list"
ALPHA_KLINES_URL = "https://www.binance.com/bapi/defi/v1/public/alpha-trade/klines"
KYBER = "https://aggregator-api.kyberswap.com/bsc/api/v1/routes"
USDT = "0x55d398326f99059fF775485246999027B3197955"
COST_SIZES = (400, 1000, 2100, 3000, 4200)

MMR = 0.05            # mantenimiento real de un alt Alpha (ARIA #56)
PERP_COST = 0.05      # taker del perp, % por lado
SL_SLIP = 0.005       # llenado del STOP_MARKET sobre el trigger (ARIA +0.33 %, AIN +1.39 %)
LO_EDGE = 0.005       # la límite Kyber se llena si el on-chain la supera en 0.5 %
RHO = 0.8             # retención del empuje propio por minuto (execution_decomp, job #35)
RUNNER_T, RUNNER_TP = 60, 0.20
QUARTER_MS = 91 * 86400 * 1000
WEEK_MS = 7 * 86400 * 1000


# ── Datos ───────────────────────────────────────────────────────────────
def alpha_tokens() -> dict:
    """símbolo → token BSC de la lista Alpha (incluye offline: su historia sigue disponible)."""
    try:
        data = _get(ALPHA_LIST_URL, timeout=30).json()["data"]
    except Exception:
        data = json.load(open(TOKENS, encoding="utf-8"))
    return {t["symbol"].upper(): t for t in data if str(t.get("chainId")) == "56" and t.get("alphaId")}


def _alpha_month(alpha_id: str, month: str) -> np.ndarray:
    p = pd.Period(month, "M")
    t, stop = int(p.start_time.timestamp() * 1000), int((p + 1).start_time.timestamp() * 1000)
    rows = []
    while t < stop:
        r = _get(ALPHA_KLINES_URL, {"symbol": f"{alpha_id}USDT", "interval": "1m", "limit": 1500,
                                    "startTime": t, "endTime": stop - 1}, timeout=30)
        data = (r.json().get("data") or []) if r is not None else []
        if not data:
            break
        rows += [k[:5] for k in data]
        t = int(data[-1][0]) + 60_000
    return np.array(rows, dtype=float) if rows else np.empty((0, 5))


def month_bars(sym: str, kind: str, month: str, ids: dict = None):
    """Velas 1m [ts, o, h, l, c] de un mes completo, cacheadas en .npz. None si no hay."""
    path = KL_DIR / kind / f"{sym}-{month}.npz"
    if path.exists():
        a = np.load(path)["a"]
        return a if a.size else None
    if ids is None:
        return None
    path.parent.mkdir(parents=True, exist_ok=True)
    if kind == "alpha":
        a = _alpha_month(ids[sym]["alphaId"], month) if sym in ids else np.empty((0, 5))
    else:
        pair = f"{sym}USDT"
        r = _get(f"{VISION}/data/{KINDS[kind]}/{pair}/1m/{pair}-1m-{month}.zip", timeout=120)
        a = np.empty((0, 5))
        if r is not None:
            with zipfile.ZipFile(io.BytesIO(r.content)) as zf:
                raw = zf.read(zf.namelist()[0]).decode()
            a = np.array([ln.split(",")[:5] for ln in raw.splitlines() if ln and ln[0].isdigit()], dtype=float)
    np.savez_compressed(path, a=a)
    return a if a.size else None


def cached_symbols() -> dict:
    out = {}
    for f in (KL_DIR / "alpha").glob("*.npz"):
        sym, month = f.stem.split("-", 1)
        if (KL_DIR / "perp" / f.name).exists():
            out.setdefault(sym, []).append(month)
    return {s: sorted(m) for s, m in out.items()}


def load(sym: str, months: list):
    """Arrays por minuto alineados al perp. Spot = NaN en los minutos sin vela on-chain."""
    parts = {}
    for kind in ("alpha", "perp", "mark"):
        arrs = [a for a in (month_bars(sym, kind, m) for m in months) if a is not None]
        if not arrs:
            if kind == "mark":
                continue
            return None
        a = np.vstack(arrs)
        a = a[np.argsort(a[:, 0], kind="stable")]
        parts[kind] = a[np.r_[True, np.diff(a[:, 0]) > 0]]
    p = parts["perp"]
    ts = p[:, 0].astype(np.int64)

    def align(a):
        a_ts = a[:, 0].astype(np.int64)
        i = np.searchsorted(a_ts, ts)
        ok = i < len(a_ts)
        ok[ok] = a_ts[i[ok]] == ts[ok]
        out = np.full((len(ts), 5), np.nan)
        out[ok] = a[i[ok]]
        return out

    al = align(parts["alpha"])
    mh = align(parts["mark"])[:, 2] if "mark" in parts else p[:, 2]
    return {"ts": ts, "po": p[:, 1], "ph": p[:, 2], "pl": p[:, 3], "pc": p[:, 4],
            "mh": np.where(np.isnan(mh), p[:, 2], mh), "sh": al[:, 2], "sl": al[:, 3], "sc": al[:, 4]}


def funding_table(symbols) -> dict:
    fr = pd.read_csv(OUT_CSV, usecols=["symbol", "rate", "interval_hours", "funding_ts"])
    fr = fr[fr.symbol.isin(list(symbols))].sort_values("funding_ts")
    return {s: (g.funding_ts.to_numpy(np.int64), g.rate.to_numpy(float), g.interval_hours.to_numpy(float))
            for s, g in fr.groupby("symbol")}


# ── Costo por token ─────────────────────────────────────────────────────
def _quote(src, dst, wei):
    for wait in (0, 3, 10):
        time.sleep(wait + 0.6)
        r = requests.get(KYBER, params={"tokenIn": src, "tokenOut": dst, "amountIn": str(int(wei))},
                         headers={"x-client-id": "basyo-study"}, timeout=20)
        if r.status_code == 200 and r.json().get("data"):
            return int(r.json()["data"]["routeSummary"]["amountOut"])
    return None


def refresh_cost(symbols):
    """comisión a (%) = medio spread a 25 USD; pendiente b (% por USD) sobre ese piso, por tamaño."""
    tokens = alpha_tokens()
    rows = []
    for sym in sorted(symbols):
        t = tokens.get(sym.upper())
        if not t:
            print(sym, "sin contrato")
            continue
        addr, dec = t["contractAddress"], int(t["decimals"])
        got = _quote(USDT, addr, 25e18)
        back = _quote(addr, USDT, got) if got else None
        if not back:
            print(sym, "sin cotización")
            continue
        buy, sell = 25 / (got / 10 ** dec), (back / 1e18) / (got / 10 ** dec)
        mid = (buy + sell) / 2
        a = max(0.0, (buy - sell) / (2 * mid) * 100)
        row = {"symbol": sym, "fee_pct": a}
        xs, ys = [], []
        for s in COST_SIZES:
            out = _quote(USDT, addr, s * 1e18)
            if out:
                xs.append(s)
                ys.append(max(0.0, (s / (out / 10 ** dec) / mid - 1) * 100 - a))
                row[f"imp_{s}"] = ys[-1]
        x, y = np.array(xs, float), np.array(ys, float)
        row["slope_pct_per_usd"] = float((x * y).sum() / (x * x).sum()) if len(x) else np.nan
        rows.append(row)
        print(f"{sym}: comisión {a:.3f} %  impacto " + "  ".join(f"{s}:{v:.2f}" for s, v in zip(xs, ys)), flush=True)
    pd.DataFrame(rows).to_csv(COST_CSV, index=False)
    print("Guardado", COST_CSV)


def cost_table():
    df = pd.read_csv(COST_CSV).dropna(subset=["fee_pct", "slope_pct_per_usd"])
    # Cotización rota (ruta sin liquidez el día de la foto; TAC 2026-11-10: 12 % de comisión) → mediana.
    df = df[(df.fee_pct < 3) & (df.slope_pct_per_usd * 2100 < 10)]
    med = (float(df.fee_pct.median()), float(df.slope_pct_per_usd.median()))
    table = {r.symbol: (r.fee_pct, r.slope_pct_per_usd) for r in df.itertuples()}
    return lambda sym: table.get(sym, med)


# ── Política ────────────────────────────────────────────────────────────
@dataclass(frozen=True)
class Pol:
    name: str
    wait: bool = True
    th_e: float = 0.25        # basis ejecutable mínimo de entrada, %
    q_e: float = None         # y además ≥ percentil q del basis del propio token (ventana `look`)
    th_x: float = -0.15
    q_x: float = None
    look: int = 4320
    win_e: int = 360          # ventana de espera, min
    win_x: int = 360
    imp: float = 0.15         # impacto máximo por tramo, %
    smax: int = 6
    gap: int = 1              # min entre tramos
    # seguros
    F: float = 0.80           # SL al F del camino entrada → liquidación
    trig: str = "mark"        # precio que dispara el SL: mark | last
    delta: object = 0.005     # venta límite Kyber a sl·(1−δ); None = sin límite
    a_slices: int = 4         # venta del spot desnudo (caso A)
    a_gap: int = 0            # min entre esos tramos (0 = misma vela)
    b_mode: str = "runner"    # caso B: runner (TP −20 % / 1 h) | now


def _first(mask) -> int:
    if not len(mask):
        return -1
    i = int(np.argmax(mask))
    return i if mask[i] else -1


class Sym:
    def __init__(self, name, d, funding, cost, strict=True):
        self.name, self.ts = name, d["ts"]
        self.po, self.ph, self.pl, self.pc, self.mh = d["po"], d["ph"], d["pl"], d["pc"], d["mh"]
        raw = d["sc"]
        sc = raw.copy()
        if strict:
            sc[(d["sh"] == d["sl"]) & (sc == np.r_[np.nan, sc[:-1]])] = np.nan
        self.sc, self.n = sc, len(sc)
        self.scf = pd.Series(raw).ffill().to_numpy()
        self.sh = np.where(np.isnan(d["sh"]), self.scf, d["sh"])
        self.a, self.b = cost
        ok = ~np.isnan(sc)
        self.idx = np.where(ok)[0]
        self.last = np.maximum.accumulate(np.where(ok, np.arange(self.n), -1))
        f_ts, rate, hours = funding
        self.f_ts, self.daily = f_ts, rate * 100 * 24 / hours
        k = np.clip(np.searchsorted(self.ts, f_ts), 0, self.n - 1)
        inside = (f_ts >= self.ts[0]) & (f_ts <= self.ts[-1])
        self.G = np.r_[0, np.cumsum(np.where(inside, rate * self.pc[k], 0.0))][np.searchsorted(f_ts, self.ts, "right")]
        self._cache = {}

    def price_bar(self, k):
        """Primer print en [k, k+15]; si no, el último en [k−60, k]. None si no hay referencia."""
        p = np.searchsorted(self.idx, k)
        if p < len(self.idx) and self.idx[p] <= k + 15:
            return int(self.idx[p])
        k = min(k, self.n - 1)
        last = int(self.last[k])
        return last if last >= 0 and k - last <= 60 else None

    def ex(self, side, c):
        key = (side, round(c, 5))
        if key not in self._cache:
            with np.errstate(invalid="ignore"):
                self._cache[key] = ((self.pc / (self.sc * (1 + c / 100)) - 1) * 100 if side == "buy"
                                    else (self.sc * (1 - c / 100) / self.pc - 1) * 100)
        return self._cache[key]

    def quant(self, side, q, look):
        key = ("q", side, q, look)
        if key not in self._cache:
            s = pd.Series(self.ex(side, 0.0))
            self._cache[key] = s.rolling(look, min_periods=look // 20).quantile(q).shift(1).to_numpy()
        return self._cache[key]

    def fire(self, side, c, th, q, look):
        key = ("f", side, round(c, 5), th, q, look)
        if key not in self._cache:
            with np.errstate(invalid="ignore"):
                ok = self.ex(side, c) >= th
                if q is not None:
                    ok &= self.ex(side, 0.0) >= self.quant(side, q, look)
            self._cache[key] = ok
        return self._cache[key]

    def attempts(self, hold, step, min_daily, tail):
        for t in range(1440, self.n - hold - tail, step):
            k = np.searchsorted(self.f_ts, self.ts[t], "right")
            if k >= 3 and self.daily[k - 3:k].mean() >= min_daily:
                yield t


def n_slices(S, usd, pol):
    imp = S.a + S.b * usd
    return max(1, min(pol.smax, math.ceil(imp / pol.imp))) if imp > 0 else 1


def leg(S, start, side, usd, pol, rho=RHO, force=False):
    """([(vela, costo spot %)], n) o None si no disparó en la ventana."""
    n = n_slices(S, usd, pol)
    u, c = usd / n, S.a + S.b * usd / n
    buying = side == "buy"
    win = pol.win_e if buying else pol.win_x
    if force or not pol.wait:
        i = start
    else:
        th, q = (pol.th_e, pol.q_e) if buying else (pol.th_x, pol.q_x)
        fired = S.fire(side, c, th, q, pol.look)[start:min(start + win, S.n - 1)]
        if not fired.any():
            return None
        i = start + int(fired.argmax())
    fills, push, k, prev = [], 0.0, i, i
    for s in range(n):
        target = k + 1 if s == 0 else k + pol.gap
        j = S.price_bar(target) if target < S.n else None
        if j is None:
            break
        push = push * rho ** max(j - prev, 1) if s else 0.0
        fills.append((j, c + push))
        push += 2 * S.b * u
        prev, k = j, max(target, j)
    return fills, n


def naked_sell(S, j, usd, pol, rho=RHO):
    """Venta del spot desnudo desde la vela j: (precio medio, última vela)."""
    n = max(1, pol.a_slices)
    u = usd / n
    c, push, px, k = S.a + S.b * u, 0.0, [], j
    for s in range(n):
        k = min(j + s * pol.a_gap, S.n - 1)
        if s and pol.a_gap:
            push *= rho ** pol.a_gap
        px.append(S.scf[k] * (1 - (c + push) / 100))
        push += 2 * S.b * u
    return float(np.mean(px)), k


def protect(S, k0, k1, PE, SE, usd, pol, lev, sl_slip=SL_SLIP, naked_extra=0.0):
    """Seguros en [k0, k1). None si no dispararon; si no (valor por unidad, vela final, caso)."""
    if k1 <= k0:
        return None
    liq = PE * (1 + 1 / lev) / (1 + MMR)
    sl = PE + pol.F * (liq - PE)
    trig = S.mh if pol.trig == "mark" else S.ph
    tp = _first(trig[k0:k1] >= sl)
    lim = sl * (1 - pol.delta) if pol.delta is not None else None
    tl = _first(S.sh[k0:k1] >= lim * (1 + LO_EDGE)) if lim else -1
    if tp < 0 and tl < 0:
        return None
    end = S.n - 1
    taker = 1 + PERP_COST / 100

    def stop_fill(t):
        if S.po[t] >= liq:                       # abre ya sobre la liquidación: se pierde el margen
            return PE * (1 + 1 / lev)
        return max(sl, S.po[t]) * (1 + sl_slip)

    if tl >= 0 and (tp < 0 or tl < tp):          # B: se vendió el spot, el short sigue
        t = k0 + tl
        if pol.b_mode == "now":
            j = min(t + 1, end)
            buy = stop_fill(j) if trig[j] >= sl else S.pc[j] * taker
        else:
            top = min(end, t + 1 + RUNNER_T)
            w_sl = _first(trig[t + 1:top] >= sl)
            w_tp = _first(S.pl[t + 1:top] <= lim * (1 - RUNNER_TP))
            hits = [(w, kind) for w, kind in ((w_sl, "sl"), (w_tp, "tp")) if w >= 0]
            if hits:
                w, kind = min(hits)
                j = t + 1 + w
                buy = stop_fill(j) if kind == "sl" else min(lim * (1 - RUNNER_TP), S.po[j]) * taker
            else:
                j = max(top - 1, t)
                buy = S.pc[j] * taker
        return lim / SE - buy / PE, j, "B"
    t = k0 + tp
    buy = stop_fill(t)
    if tl == tp:
        return lim / SE - buy / PE, t, "AB"
    j = min(t + 1, end)
    if lim and S.sh[j] >= lim * (1 + LO_EDGE):
        return lim / SE - buy / PE, j, "A"
    sell, j = naked_sell(S, j, usd * S.scf[j] / SE, pol)
    return sell * (1 - naked_extra) / SE - buy / PE, j, "A"


def lifecycle(S, t, usd, pol, hold, lev=1.0, rho=RHO, sl_slip=SL_SLIP, naked_extra=0.0, seguros=True):
    """(neto, entrada, salida, funding, fracción, caso) en % del nocional objetivo; None si no hay precio.
    caso: 0 sin disparo, 1 normal, 2 A, 3 B, 4 AB."""
    res = leg(S, t, "buy", usd, pol, rho)
    if res is None or not res[0]:
        return 0.0, 0.0, 0.0, 0.0, 0.0, 0
    fills, n = res
    frac = len(fills) / n
    js = np.array([j for j, _ in fills])
    cs = np.array([c for _, c in fills])
    SE = float(np.mean(S.sc[js] * (1 + cs / 100)))
    PE = float(np.mean(S.pc[js] * (1 - PERP_COST / 100)))
    G0 = float(np.mean(S.G[js]))
    entry = (PE / SE - 1) * 100
    # salida normal: hasta 4 ventanas, la última forzada
    outs, left, start = [], 1.0, t + hold
    for attempt in range(4):
        out = leg(S, start, "sell", left * frac * usd, pol, rho, force=attempt == 3 or not pol.wait)
        if out is None:
            start += pol.win_x
            continue
        xf, xn = out
        outs += [(j, c, left / xn) for j, c in xf]
        left -= len(xf) * left / xn
        if left <= 1e-9:
            break
        start = max(start + pol.win_x, (xf[-1][0] + 1) if xf else 0)
    if not outs:
        return None
    if left > 1e-9:
        outs.append((outs[-1][0], outs[-1][1], left))
    ev = protect(S, int(js[-1]) + 1, outs[0][0], PE, SE, frac * usd, pol, lev, sl_slip, naked_extra) if seguros else None
    if ev:
        val, j, case = ev
        fund = (S.G[j] - G0) / PE
        code = {"A": 2, "B": 3, "AB": 4}[case]
    else:
        val = sum(w * (S.sc[j] * (1 - c / 100) / SE - S.pc[j] * (1 + PERP_COST / 100) / PE) for j, c, w in outs)
        fund = sum(w * (S.G[j] - G0) for j, _, w in outs) / PE
        code = 1
    val, fund = val * 100 * frac, fund * 100 * frac
    return val + fund, entry * frac, val - entry * frac, fund, frac, code
