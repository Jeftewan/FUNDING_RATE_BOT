"""Análisis completo de UNA oportunidad spot_perp Binance (modal "Análisis").

Junta lo que antes se miraba en Coinglass + el panel de analistas:
  * funding liquidado (~1000 pagos) y rachas de cobro anteriores (analysis.analysts);
  * precio perp y spot 1h × 30 d (spot CEX o VWAP on-chain Alpha) y basis horario;
  * OI USD y top traders long/short 1h × 30 d (/futures/data, ~30 d por API);
  * señales diarias con analysis.signal_features (MISMA construcción que el dataset de
    entrenamiento scripts/signals_dataset.py → paridad);
  * analistas, consenso 1–10, calibración y análogos (models/analyst_table.json).
Cada fuente es opcional: si falla, su sección queda en None y el resto sale igual.
"""
import logging
import math
import statistics
import time

from analysis import analysts as an
from analysis.ml_features import LOOKBACK, window_stats
from analysis.signal_features import signal_features
from exchanges.market_features import basis_pct, vol_7d, vwap, _f

log = logging.getLogger("bot")
H_MS, DAY_MS = 3_600_000, 86_400_000
PRICE_HOURS = 720            # 30 d de velas 1h
_cache = {}                  # (opp_id, next_funding_ts) → (ts, result)
CACHE_TTL = 1800


def _safe(fn, *a, **k):
    try:
        return fn(*a, **k)
    except Exception as e:  # noqa: BLE001
        log.warning("opportunity_analysis %s: %s", getattr(fn, "__name__", fn), e)
        return None


# ── Fuentes ────────────────────────────────────────────────────────────────
def funding_history(ex, pid: str):
    rows = ex.fapiPublicGetFundingRate({"symbol": pid, "limit": 1000}) or []
    rows = sorted(rows, key=lambda r: int(r["fundingTime"]))
    return [int(r["fundingTime"]) for r in rows], [float(r["fundingRate"]) for r in rows]


def perp_klines(ex, pid: str):
    return {int(k[0]): k for k in ex.fapiPublicGetKlines({"symbol": pid, "interval": "1h",
                                                         "limit": PRICE_HOURS}) or []}


def spot_klines(ex, pid: str, alpha_id: str = None):
    if alpha_id:
        from exchanges.market_features import _fetch_alpha_klines
        out, end = {}, None
        for _ in range(2):            # la API Alpha pagina de a 500 velas 1h
            p = {"interval": "1h", "limit": 500}
            if end:
                p["endTime"] = end
            rows = _fetch_alpha_klines(alpha_id, p)
            if not rows:
                break
            out.update({int(k[0]): k for k in rows})
            end = int(rows[0][0]) - 1
        return out
    return {int(k[0]): k for k in ex.publicGetKlines({"symbol": pid, "interval": "1h",
                                                     "limit": PRICE_HOURS}) or []}


def futures_data(ex, pid: str):
    """{ts_hour_s: (oi, oi_usd, top_ls)} de /futures/data (1h, 500 puntos ≈ 21 d)."""
    oi = ex.fapiDataGetOpenInterestHist({"symbol": pid, "period": "1h", "limit": 500}) or []
    top = ex.fapiDataGetTopLongShortPositionRatio({"symbol": pid, "period": "1h", "limit": 500}) or []
    out = {}
    for r in oi:
        out.setdefault(int(r["timestamp"]) // 1000, [None, None, None])[:2] = \
            [_f(r.get("sumOpenInterest")), _f(r.get("sumOpenInterestValue"))]
    for r in top:
        out.setdefault(int(r["timestamp"]) // 1000, [None, None, None])[2] = _f(r.get("longShortRatio"))
    return out


def hourly_rows(perp: dict, spot: dict, fdata: dict, alpha: bool):
    """Filas horarias con la semántica de signals_dataset.symbol_rows (precio en ts =
    cierre de la vela [ts−1h, ts))."""
    if not perp:
        return []
    daily = {t // DAY_MS * DAY_MS: _f(k[4]) for t, k in perp.items() if (t + H_MS) % DAY_MS == 0}
    dkeys = sorted(daily)
    rows = []
    for t in sorted(perp):
        ts = t + H_MS
        p, s = perp[t], (spot or {}).get(t)
        if alpha:
            b = basis_pct(vwap([s]), vwap([p])) if s and _f(s[5]) else None
        else:
            b = basis_pct(s[4], p[4]) if s else None
        oi, oi_usd, top = (fdata or {}).get(ts // 1000, (None, None, None))
        day0 = ts // DAY_MS * DAY_MS
        rows.append({"ts_hour": ts // 1000, "perp": _f(p[4]), "spot": _f(s[4]) if s else None,
                     "basis_pct": b, "open_interest": oi, "open_interest_usd": oi_usd,
                     "top_long_short_pos_ratio": top,
                     "volatility_7d": vol_7d([daily[d] for d in dkeys if d < day0])})
    return rows


def model_history(symbol: str, exchange: str):
    from core.db_models import ScoreSnapshot
    snaps = (ScoreSnapshot.query.filter_by(symbol=symbol, exchange=exchange)
             .order_by(ScoreSnapshot.captured_at.asc()).limit(60).all())
    return [{"ts": int(s.captured_at.timestamp() * 1000), "pred": s.model_prediction,
             "score": s.score, "confidence": s.confidence} for s in snaps]


# ── Análisis ───────────────────────────────────────────────────────────────
def regime_median(all_data) -> float:
    vals = [float(d.get("fr") or 0) * float(d.get("ipd") or 3) for d in all_data or []
            if str(d.get("exchange", "")).lower() == "binance"]
    return statistics.median(vals) if vals else None


def analyst_values(opp: dict, rates: list, ts: list, rows: list, all_data, now_ms: int) -> dict:
    """Valores de cada variable de analista con la misma definición que el estudio."""
    ih = float(opp.get("interval_hours") or 8)
    vals = {"p_prod": opp.get("model_prediction"), "relw": opp.get("rel_width"),
            "regime_median_daily": regime_median(all_data)}
    if len(rates) >= LOOKBACK + 1:                       # spec settled_v1 (= dataset)
        ws = window_stats(rates[-(LOOKBACK + 1):-1])
        if ws:
            vals.update(streak=ws["streak"], pct=ws["pct"])
    eps = an.funding_episodes(rates, ts, ih) if rates else []
    st = an.episode_stats(eps, now_ms)
    vals.update(ep_med_days=st["ep_med_days"], ep_age_days=st["ep_age_days"])
    vals.update(signal_features(rows, now_ms // 1000))
    return vals, eps, st


def build(opp: dict, exchange_manager, all_data=None, db_enabled=False, capital=1000.0):
    sym, exch = opp["symbol"], opp.get("exchange", "Binance")
    pid = f"{sym}USDT"
    alpha = opp.get("spot_venue") == "bsc"
    now_ms = int(time.time() * 1000)
    ex = exchange_manager.get_exchange("binance") if exchange_manager else None
    alpha_id = None
    if alpha:
        from exchanges.market_features import _load_alpha_tokens
        alpha_id = ((_safe(_load_alpha_tokens) or {}).get(sym) or {}).get("alpha_id")

    ts, rates = (_safe(funding_history, ex, pid) or ([], [])) if ex else ([], [])
    perp = (_safe(perp_klines, ex, pid) or {}) if ex else {}
    spot = (_safe(spot_klines, ex, pid, alpha_id) or {}) if (ex and (not alpha or alpha_id)) else {}
    fdata = (_safe(futures_data, ex, pid) or {}) if ex else {}
    rows = hourly_rows(perp, spot, fdata, alpha)
    vals, eps, ep_st = analyst_values(opp, rates, ts, rows, all_data, now_ms)

    ev = an.evaluate(vals)
    pred = opp.get("model_prediction")
    analog = an.analog_cell(pred, alpha, opp.get("rel_width")) if pred is not None else None

    ih = float(opp.get("interval_hours") or 8)
    ppd = 24 / max(ih, 1)
    funding = None
    if rates:
        funding = {"ts": ts, "rate_pct": [round(r * 100, 5) for r in rates],
                   "apr": [round(r * ppd * 365 * 100, 1) for r in rates],
                   "base_apr": round(an.BASE_DAILY * 365 * 100, 1), "interval_hours": ih}
    price = None
    if rows:
        price = {"ts": [r["ts_hour"] * 1000 for r in rows], "perp": [r["perp"] for r in rows],
                 "spot": [r["spot"] for r in rows],
                 "basis": [None if r["basis_pct"] is None else round(r["basis_pct"], 4) for r in rows],
                 "spot_source": "alpha" if alpha else "cex"}
    market = None
    mrows = [r for r in rows if r["open_interest_usd"] is not None or r["top_long_short_pos_ratio"] is not None]
    if mrows:
        market = {"ts": [r["ts_hour"] * 1000 for r in mrows],
                  "oi_usd": [r["open_interest_usd"] for r in mrows],
                  "top_ls": [r["top_long_short_pos_ratio"] for r in mrows]}
    mh = _safe(model_history, sym, exch) if db_enabled else None

    recent = [e for e in eps if e["end_ts"] >= now_ms - 365 * DAY_MS]
    size = an.size_mult(ev.get("consensus")) if ev else None
    size = size if size is not None else opp.get("size_mult")
    return {
        "symbol": sym, "exchange": exch, "alpha": alpha, "generated_at": now_ms,
        "model": {"pred": pred, "p10": opp.get("apr_p10"), "p90": opp.get("apr_p90"),
                  "rel_width": opp.get("rel_width")},
        "panel": ev, "analog": analog, "values": {k: (None if v is None or (isinstance(v, float) and math.isnan(v))
                                                       else round(float(v), 4)) for k, v in vals.items()},
        "size_mult": size, "capital_sugerido": round(capital * size, 0) if size else None,
        "episodes": {"list": recent[-25:], **ep_st,
                     "apr_med": (statistics.median([e["apr"] for e in recent if e["ended"]])
                                 if any(e["ended"] for e in recent) else None)},
        "funding": funding, "price": price, "market": market, "model_history": mh,
        "table_version": (an.table() or {}).get("version"),
    }


def get(opp: dict, exchange_manager, all_data=None, db_enabled=False, capital=1000.0):
    key = (opp.get("_id"), opp.get("next_funding_ts"), round(capital))
    hit = _cache.get(key)
    if hit and time.time() - hit[0] < CACHE_TTL:
        return hit[1], True
    res = build(opp, exchange_manager, all_data, db_enabled, capital)
    if len(_cache) > 100:
        _cache.clear()
    _cache[key] = (time.time(), res)
    return res, False
