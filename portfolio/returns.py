"""Rentabilidad real de una posición sobre el CAPITAL TOTAL (spot + margen del perp).

`capital_used` es todo el capital comprometido: en spot_perp a 1x la mitad compra el spot
y la otra mitad es margen del short; solo el nocional (`exposure`) cobra funding. Por eso
la rentabilidad real se mide contra `capital_used`, no contra el nocional.
"""
import time

MIN_DAYS = 1 / 24          # menos de 1 h: el anualizado no significa nada
BASIS_MAX_AGE_S = 3 * 3600


def position_return(net, capital, days) -> dict:
    """{roc_pct, apr_pct}: neto / capital total y su anualizado simple (× 365 / días)."""
    try:
        net, capital, days = float(net), float(capital), float(days)
    except (TypeError, ValueError):
        return {"roc_pct": None, "apr_pct": None}
    if capital <= 0:
        return {"roc_pct": None, "apr_pct": None}
    roc = net / capital * 100
    apr = roc * 365 / days if days >= MIN_DAYS else None
    return {"roc_pct": round(roc, 4), "apr_pct": round(apr, 2) if apr is not None else None}


def spot_from_basis(perp_now, basis_pct):
    """basis_pct = (perp − spot)/spot × 100  →  spot = perp / (1 + basis/100)."""
    if not perp_now or basis_pct is None:
        return None
    return float(perp_now) / (1 + float(basis_pct) / 100)


def unrealized_price_pnl(pos: dict, perp_now, spot_now):
    """PnL de precio a mercado de lo que sigue abierto: spot comprado + short vendido.
    None si faltan cantidades o precios de entrada (posición manual)."""
    sq, pq = pos.get("spot_qty"), pos.get("perp_qty")
    se, pe = pos.get("spot_entry_price"), pos.get("entry_price")
    if not (sq is not None and pq is not None and se and pe and perp_now and spot_now):
        return None
    return float(sq) * (float(spot_now) - float(se)) + float(pq) * (float(pe) - float(perp_now))


def latest_basis(symbol: str, exchange: str, now: float = None):
    """Último basis_pct de market_feature_snapshots (≤ 3 h). None si no hay o sin DB."""
    try:
        from core.db_models import MarketFeatureSnapshot as M
        now = now or time.time()
        row = (M.query.filter(M.symbol == symbol, M.exchange == exchange,
                              M.ts_hour >= int(now - BASIS_MAX_AGE_S), M.basis_pct.isnot(None))
               .order_by(M.ts_hour.desc()).first())
        return float(row.basis_pct) if row else None
    except Exception:  # noqa: BLE001 — sin tabla/DB: se aproxima con basis 0
        return None


def active_mark_to_market(pos: dict, perp_now, net_realized: float, days: float, basis_pct=None) -> dict:
    """Neto a mercado y rentabilidad real de una posición activa.

    net_realized = funding − fees (entrada + salida estimada) + PnL de precio de cierres parciales.
    Se suma el PnL de precio no realizado con el spot inferido del último basis; sin basis se
    asume spot = perp (basis 0) y se marca `mtm_approx`.
    """
    approx = basis_pct is None
    spot_now = spot_from_basis(perp_now, 0.0 if approx else basis_pct)
    unreal = unrealized_price_pnl(pos, perp_now, spot_now)
    net = float(net_realized) + (unreal or 0.0)
    out = position_return(net, pos.get("capital_used"), days)
    return {
        "unrealized_price_pnl": round(unreal, 6) if unreal is not None else None,
        "net_mtm": round(net, 6),
        "mtm_approx": bool(approx and unreal is not None),
        "real_roc_pct": out["roc_pct"],
        "real_apr_pct": out["apr_pct"],
        "days_held": round(days, 3),
    }


def closed_return(h: dict) -> dict:
    """Rentabilidad real de una posición cerrada: net_earned / capital_used en sus horas."""
    net = h.get("net_earned")
    if net is None:
        net = h.get("earned") or 0
    out = position_return(net, h.get("capital_used"), (h.get("hours") or 0) / 24)
    return {"real_roc_pct": out["roc_pct"], "real_apr_pct": out["apr_pct"]}
