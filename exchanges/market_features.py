"""Señales de mercado horarias para pares Binance spot_perp (tabla market_feature_snapshots).

Binance guarda solo ~30 d de OI, taker buy/sell y top long/short: se recolectan
cada hora para los candidatos del scan y alimentan el modelo futuro (Fase 3).

Costo por corrida (cliente ccxt de exchanges/manager.py, mismo proxy):
- todos-los-símbolos: premiumIndex, fundingInfo, bookTicker spot y perp (4 llamadas);
- por símbolo: openInterestHist, takerlongshortRatio, topLongShortPositionRatio
  (/futures/data: 1000 req/5 min por IP → se pacean a DATA_MIN_INTERVAL);
- klines 1d del perp para la volatilidad 7 d, una vez por símbolo y día UTC.
Un fallo en una fuente deja esas columnas en NULL; nunca tumba el scan.

Las funciones puras (basis_pct, pct_change, vol_7d) las usa también
scripts/market_features_backfill.py → misma definición online y offline.
"""
import logging
import math
import time
from datetime import datetime, timezone

from exchanges.net import is_geoblock_error, mark_geoblocked

log = logging.getLogger("bot")

EXCHANGE = "Binance"
RETENTION_DAYS = 120
# /futures/data/*: 1000 req / 5 min por IP → ≤ 3.3 req/s.
DATA_MIN_INTERVAL = 0.32
# Errores seguidos en /futures/data antes de abandonar el resto de símbolos
# (429/418 → no insistir y arriesgar un ban de IP).
MAX_CONSECUTIVE_ERRORS = 5

FEATURE_COLUMNS = [
    "funding_rate_est", "mark_price", "index_price", "next_funding_ts",
    "spot_mid", "perp_mid", "basis_pct",
    "open_interest", "open_interest_usd", "oi_change_1h_pct",
    "taker_buy_sell_ratio", "top_long_short_pos_ratio",
    "funding_interval_hours", "funding_cap", "funding_floor", "volatility_7d",
]


# ── Funciones puras (paridad prod / backfill) ──────────────────

def _f(v):
    try:
        x = float(v)
    except (TypeError, ValueError):
        return None
    return x if math.isfinite(x) else None


def hour_bucket(ts: float) -> int:
    """Inicio de la hora UTC (epoch s)."""
    return int(ts // 3600) * 3600


def mid(bid, ask):
    b, a = _f(bid), _f(ask)
    if not b or not a or b <= 0 or a <= 0:
        return None
    return (b + a) / 2


def basis_pct(spot_mid, perp_mid):
    """(perp − spot) / spot × 100. Positivo = perp con prima (contango)."""
    s, p = _f(spot_mid), _f(perp_mid)
    if not s or not p or s <= 0:
        return None
    return (p - s) / s * 100


def pct_change(new, old):
    n, o = _f(new), _f(old)
    if n is None or not o:
        return None
    return (n / o - 1) * 100


def vol_7d(daily_closes):
    """Desvío estándar muestral de los 7 log-retornos de los últimos 8 cierres diarios COMPLETOS."""
    closes = [c for c in (_f(x) for x in daily_closes[-8:]) if c and c > 0]
    if len(closes) < 8:
        return None
    rets = [math.log(closes[i] / closes[i - 1]) for i in range(1, len(closes))]
    m = sum(rets) / len(rets)
    return math.sqrt(sum((r - m) ** 2 for r in rets) / (len(rets) - 1))


# ── Colector ────────────────────────────────────────────────────

class MarketFeatureCollector:
    def __init__(self, exchange_manager, sleep=time.sleep):
        self.exchange_manager = exchange_manager
        self._sleep = sleep
        self._vol_cache = {}  # {symbol: (utc_day, vol)}

    def _bulk(self, name, fn, params=None):
        try:
            return fn(params or {})
        except Exception as e:
            if is_geoblock_error(e):
                mark_geoblocked("binance")
            log.warning(f"Market features: {name} falló: {e}")
            return None

    def collect(self, symbols, now: float = None) -> list:
        """Filas (dict) para cada símbolo base; columnas sin dato = None."""
        ex = self.exchange_manager.get_exchange("binance")
        symbols = sorted({s for s in symbols if s})
        if not ex or not symbols:
            return []
        now = now or time.time()

        premium = {d.get("symbol"): d for d in
                   self._bulk("premiumIndex", ex.fapiPublicGetPremiumIndex) or []}
        finfo = {d.get("symbol"): d for d in
                 self._bulk("fundingInfo", ex.fapiPublicGetFundingInfo) or []}
        spot_bt = {d.get("symbol"): d for d in
                   self._bulk("spot bookTicker", ex.publicGetTickerBookTicker) or []}
        perp_bt = {d.get("symbol"): d for d in
                   self._bulk("perp bookTicker", ex.fapiPublicGetTickerBookTicker) or []}

        rows = []
        data_ok, errors = True, 0
        for sym in symbols:
            pid = f"{sym}USDT"
            row = {"symbol": sym, "exchange": EXCHANGE, "ts_hour": hour_bucket(now)}
            row.update({c: None for c in FEATURE_COLUMNS})

            p = premium.get(pid) or {}
            row["funding_rate_est"] = _f(p.get("lastFundingRate"))
            row["mark_price"] = _f(p.get("markPrice"))
            row["index_price"] = _f(p.get("indexPrice"))
            nft = _f(p.get("nextFundingTime"))
            row["next_funding_ts"] = int(nft) if nft else None

            fi = finfo.get(pid)
            if fi:
                iv = _f(fi.get("fundingIntervalHours"))
                row["funding_interval_hours"] = int(iv) if iv else None
                row["funding_cap"] = _f(fi.get("adjustedFundingRateCap"))
                row["funding_floor"] = _f(fi.get("adjustedFundingRateFloor"))
            elif finfo:
                # fundingInfo solo lista símbolos con parámetros ajustados; el resto es 8 h.
                row["funding_interval_hours"] = 8

            s, pb = spot_bt.get(pid) or {}, perp_bt.get(pid) or {}
            row["spot_mid"] = mid(s.get("bidPrice"), s.get("askPrice"))
            row["perp_mid"] = mid(pb.get("bidPrice"), pb.get("askPrice"))
            row["basis_pct"] = basis_pct(row["spot_mid"], row["perp_mid"])

            if data_ok:
                ok = self._per_symbol_data(ex, pid, row)
                errors = 0 if ok else errors + 1
                if errors >= MAX_CONSECUTIVE_ERRORS:
                    log.warning(f"Market features: {errors} errores seguidos en /futures/data, "
                                "se omite OI/ratios del resto de símbolos")
                    data_ok = False

            row["volatility_7d"] = self._vol(ex, pid, sym, now)

            if any(row[c] is not None for c in FEATURE_COLUMNS):
                rows.append(row)
        return rows

    def _data_call(self, fn, params):
        self._sleep(DATA_MIN_INTERVAL)
        return fn(params)

    def _per_symbol_data(self, ex, pid, row) -> bool:
        """OI + Δ1h, taker buy/sell y top long/short. False si alguna llamada falló."""
        ok = True
        try:
            oi = self._data_call(ex.fapiDataGetOpenInterestHist,
                                 {"symbol": pid, "period": "1h", "limit": 2}) or []
            if oi:
                row["open_interest"] = _f(oi[-1].get("sumOpenInterest"))
                row["open_interest_usd"] = _f(oi[-1].get("sumOpenInterestValue"))
                if len(oi) >= 2:
                    row["oi_change_1h_pct"] = pct_change(oi[-1].get("sumOpenInterest"),
                                                         oi[-2].get("sumOpenInterest"))
        except Exception as e:
            ok = self._data_error(pid, "openInterestHist", e)
        try:
            tk = self._data_call(ex.fapiDataGetTakerlongshortRatio,
                                 {"symbol": pid, "period": "1h", "limit": 1}) or []
            if tk:
                row["taker_buy_sell_ratio"] = _f(tk[-1].get("buySellRatio"))
        except Exception as e:
            ok = self._data_error(pid, "takerlongshortRatio", e)
        try:
            tp = self._data_call(ex.fapiDataGetTopLongShortPositionRatio,
                                 {"symbol": pid, "period": "1h", "limit": 1}) or []
            if tp:
                row["top_long_short_pos_ratio"] = _f(tp[-1].get("longShortRatio"))
        except Exception as e:
            ok = self._data_error(pid, "topLongShortPositionRatio", e)
        return ok

    @staticmethod
    def _data_error(pid, name, e) -> bool:
        if is_geoblock_error(e):
            mark_geoblocked("binance")
        log.debug(f"Market features {pid} {name}: {e}")
        return False

    def _vol(self, ex, pid, sym, now):
        day = int(now // 86400)
        hit = self._vol_cache.get(sym)
        if hit and hit[0] == day:
            return hit[1]
        try:
            kl = ex.fapiPublicGetKlines({"symbol": pid, "interval": "1d", "limit": 9}) or []
            # La última vela es el día en curso (incompleta) → se descarta.
            closes = [k[4] for k in kl if int(k[0]) // 86400000 < day]
            v = vol_7d(closes)
        except Exception as e:
            log.debug(f"Market features {pid} klines: {e}")
            return None
        if len(self._vol_cache) > 2000:
            self._vol_cache.clear()
        self._vol_cache[sym] = (day, v)
        return v


# ── Persistencia ────────────────────────────────────────────────

def upsert_rows(db, rows) -> int:
    """INSERT … ON CONFLICT (symbol, exchange, ts_hour) DO UPDATE (Postgres y SQLite).

    Un valor nuevo NULL no pisa uno ya guardado en la misma hora (re-scan con
    una fuente caída)."""
    if not rows:
        return 0
    from core.db_models import MarketFeatureSnapshot
    table = MarketFeatureSnapshot.__table__
    if db.engine.dialect.name == "postgresql":
        from sqlalchemy.dialects.postgresql import insert
    else:
        from sqlalchemy.dialects.sqlite import insert
    now = datetime.now(timezone.utc)
    # Dedup por clave: ON CONFLICT rechaza el lote si trae dos veces la misma.
    by_key = {(r["symbol"], r["exchange"], r["ts_hour"]): {**r, "captured_at": now} for r in rows}
    stmt = insert(table).values(list(by_key.values()))
    stmt = stmt.on_conflict_do_update(
        index_elements=["symbol", "exchange", "ts_hour"],
        set_={**{c: db.func.coalesce(stmt.excluded[c], table.c[c]) for c in FEATURE_COLUMNS},
              "captured_at": stmt.excluded.captured_at},
    )
    db.session.execute(stmt)
    db.session.commit()
    return len(by_key)


def purge_old(db, now: float = None, days: int = RETENTION_DAYS) -> int:
    from core.db_models import MarketFeatureSnapshot
    cutoff = hour_bucket((now or time.time()) - days * 86400)
    n = MarketFeatureSnapshot.query.filter(MarketFeatureSnapshot.ts_hour < cutoff).delete()
    db.session.commit()
    return n
