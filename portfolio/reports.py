"""Informe mensual por inversor (cada usuario = un inversor).

Atribución por mes (UTC):
  * funding  → fecha de cada pago en `user_positions.payments_json` (activas y cerradas: la
    fila cerrada se conserva). Si el historial difiere de los pagos (edición manual), la
    diferencia va al mes de cierre.
  * fees     → la de entrada al mes de apertura; la de salida (fees del historial − entrada) al
    mes de cierre.
  * precio   → PnL de precio realizado (basis + slippage de ambas piernas) al mes de cierre.
  * capital  → promedio de `user_capital_daily` del mes; sin filas (meses anteriores al
    registro) → capital promedio desplegado en posiciones (`capital_estimated`).
Rentabilidad del mes = neto / capital promedio. No incluye lo no realizado de las abiertas
(va aparte, solo en el mes en curso).
"""
import calendar
from datetime import datetime, timezone

FUNDING_KINDS = (None, "manual_adjust")
LINK_TOLERANCE_S = 600
DAY = 86400.0


def month_bounds(month: str):
    """'YYYY-MM' → (t0, t1) epoch s UTC, [t0, t1)."""
    y, m = (int(x) for x in month.split("-"))
    if not (2000 <= y <= 2100 and 1 <= m <= 12):
        raise ValueError("mes inválido")
    t0 = datetime(y, m, 1, tzinfo=timezone.utc).timestamp()
    t1 = t0 + calendar.monthrange(y, m)[1] * DAY
    return t0, t1


def _ts(dt):
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.timestamp()


def _day(ts):
    return datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%d")


def _entry_fee(p):
    return float(p.entry_fees_real if p.entry_fees_real is not None else (p.entry_fees or 0))


def _link_history(positions, history):
    """{hist.id: position} por position_id o, en filas viejas, símbolo + closed_at (±10 min)."""
    by_id = {p.id: p for p in positions}
    used, out = set(), {}
    for h in history:
        p = by_id.get(getattr(h, "position_id", None))
        if p is None:
            hc = _ts(h.closed_at)
            best = None
            for c in positions:
                pc = _ts(c.closed_at)
                if (c.id in used or c.status != "closed" or c.symbol != h.symbol or pc is None
                        or hc is None or abs(pc - hc) > LINK_TOLERANCE_S):
                    continue
                if best is None or abs(pc - hc) < abs(_ts(best.closed_at) - hc):
                    best = c
            p = best
        if p is not None:
            used.add(p.id)
            out[h.id] = p
    return out


def _events(positions, history):
    """Eventos de PnL [(ts, kind, usd, key)], key = identificador de posición para la tabla."""
    links = _link_history(positions, history)
    ev = []
    pay_total = {}
    for p in positions:
        key = f"p{p.id}"
        tot = 0.0
        for pay in p.payments_json or []:
            if pay.get("kind") not in FUNDING_KINDS or "earned" not in pay:
                continue
            try:
                ts, e = float(pay.get("ts") or 0), float(pay.get("earned") or 0)
            except (TypeError, ValueError):
                continue
            if ts > 0:
                ev.append((ts, "funding", e, key))
                tot += e
        pay_total[p.id] = tot
        if p.entry_time:
            ev.append((float(p.entry_time) / 1000, "fees", -_entry_fee(p), key))
        if p.status == "active" and p.realized_price_pnl:
            parts = [float(x.get("ts") or 0) for x in (p.payments_json or [])
                     if x.get("kind") == "auto_close_partial"]
            ts = max(parts) if parts else (p.last_earn_update or 0)
            if ts:
                ev.append((ts, "price", float(p.realized_price_pnl), key))
    for h in history:
        hc = _ts(h.closed_at)
        if hc is None:
            continue
        p = links.get(h.id)
        key = f"p{p.id}" if p is not None else f"h{h.id}"
        if p is not None:
            resid = float(h.earned or 0) - pay_total.get(p.id, 0.0)
            if abs(resid) > 1e-9:
                ev.append((hc, "funding", resid, key))
            ev.append((hc, "fees", -(float(h.fees or 0) - _entry_fee(p)), key))
        else:   # sin posición de origen: todo al cierre
            ev.append((hc, "funding", float(h.earned or 0), key))
            ev.append((hc, "fees", -float(h.fees or 0), key))
        if h.price_pnl is not None:
            ev.append((hc, "price", float(h.price_pnl), key))
    return ev, links


def _deployed_avg(positions, history, links, t0, t1):
    """Capital promedio desplegado en [t0, t1): Σ capital × solape / duración."""
    span = max(t1 - t0, DAY / 24)
    tot = 0.0
    for p in positions:
        a = float(p.entry_time or 0) / 1000
        b = _ts(p.closed_at) if p.status == "closed" else t1
        if not a or b is None:
            continue
        tot += float(p.capital_used or 0) * max(0.0, min(b, t1) - max(a, t0))
    for h in history:      # historial sin posición de origen (filas manuales viejas)
        if h.id in links:
            continue
        b = _ts(h.closed_at)
        if b is None:
            continue
        a = b - float(h.hours or 0) * 3600
        tot += float(h.capital_used or 0) * max(0.0, min(b, t1) - max(a, t0))
    return tot / span


def _capital(user_id, positions, history, links, t0, t1):
    """Capital promedio diario. Día sin registro → registro más cercano (antes; si no, después):
    el capital total cambia poco y el desplegado lo subestimaría (inflaría la rentabilidad).
    Sin ningún registro del usuario → capital promedio desplegado. `estimated` si se rellenó."""
    from core.db_models import UserCapitalDaily
    rows = (UserCapitalDaily.query.filter(UserCapitalDaily.user_id == user_id)
            .order_by(UserCapitalDaily.date).all())
    deployed = _deployed_avg(positions, history, links, t0, t1)
    if not rows:
        return {"start": None, "end": None, "avg": round(deployed, 2), "deployed_avg": round(deployed, 2),
                "estimated": True, "source": "deployed"}
    known = {r.date: float(r.total_capital or 0) for r in rows}
    dates = sorted(known)
    days = [_day(t0 + i * DAY) for i in range(max(1, int(-(-(t1 - t0) // DAY))))]
    vals, filled = [], 0
    for d in days:
        if d in known:
            vals.append(known[d])
            continue
        filled += 1
        prev = [x for x in dates if x < d]
        vals.append(known[prev[-1]] if prev else known[dates[0]])
    return {"start": round(vals[0], 2), "end": round(vals[-1], 2),
            "avg": round(sum(vals) / len(vals), 2), "deployed_avg": round(deployed, 2),
            "estimated": filled > 0, "filled_days": filled, "days": len(days), "source": rows[-1].source}


def _sum(ev, t0, t1):
    out = {"funding": 0.0, "fees": 0.0, "price": 0.0}
    for ts, kind, usd, _ in ev:
        if t0 <= ts < t1:
            out[kind] += usd
    out["net"] = out["funding"] + out["price"] + out["fees"]
    return out


def available_months(user_id: int) -> list:
    from core.db_models import UserHistory, UserPosition
    pos = UserPosition.query.filter_by(user_id=user_id).all()
    hist = UserHistory.query.filter_by(user_id=user_id).all()
    ev, _ = _events(pos, hist)
    months = {datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m") for ts, *_ in ev}
    months.add(datetime.now(timezone.utc).strftime("%Y-%m"))
    return sorted(months, reverse=True)


def monthly_report(user_id: int, month: str, now: float = None, outlook: dict = None,
                   unrealized: dict = None) -> dict:
    """Informe del mes. `outlook` = {pos_id: net APR % esperado sobre nocional (modelo)};
    `unrealized` = {pos_id: PnL de precio a mercado} de las abiertas (solo mes en curso)."""
    from core.db_models import User, UserHistory, UserPosition
    now = now or datetime.now(timezone.utc).timestamp()
    t0, t1 = month_bounds(month)
    t_end = min(t1, now)
    if t_end <= t0:
        raise ValueError("mes futuro")
    positions = UserPosition.query.filter_by(user_id=user_id).all()
    history = UserHistory.query.filter_by(user_id=user_id).all()
    ev, links = _events(positions, history)
    hist_by_pos = {p.id: h for h, p in ((h, links.get(h.id)) for h in history) if p is not None}

    s = _sum(ev, t0, t1)
    cap = _capital(user_id, positions, history, links, t0, t_end)
    days = (t_end - t0) / DAY
    ret = s["net"] / cap["avg"] * 100 if cap["avg"] > 0 else None

    # Acumulado del año (enero → fin del mes) sobre el capital promedio del período.
    y0 = datetime(int(month[:4]), 1, 1, tzinfo=timezone.utc).timestamp()
    ys = _sum(ev, y0, t1)
    ycap = _capital(user_id, positions, history, links, y0, t_end)
    ytd = ys["net"] / ycap["avg"] * 100 if ycap["avg"] > 0 else None

    # Serie diaria (neto realizado por día).
    n_days = int(round((t1 - t0) / DAY))
    daily = [{"date": _day(t0 + i * DAY), "net": 0.0} for i in range(n_days)]
    for ts, _k, usd, _ in ev:
        if t0 <= ts < t1:
            daily[int((ts - t0) // DAY)]["net"] += usd

    # Tabla por posición: las que estuvieron abiertas en el mes.
    rows = []
    per_key = {}
    for ts, kind, usd, key in ev:
        if t0 <= ts < t1:
            per_key.setdefault(key, {"funding": 0.0, "fees": 0.0, "price": 0.0})[kind] += usd
    for p in positions:
        a = float(p.entry_time or 0) / 1000
        b = _ts(p.closed_at) if p.status == "closed" else None
        if not a or a >= t1 or (b is not None and b < t0):
            continue
        k = per_key.get(f"p{p.id}", {"funding": 0.0, "fees": 0.0, "price": 0.0})
        net = k["funding"] + k["fees"] + k["price"]
        in_days = (min(b or t_end, t_end) - max(a, t0)) / DAY
        cap_p = float(p.capital_used or 0)
        rows.append({
            "symbol": p.symbol, "exchange": p.exchange, "capital": round(cap_p, 2),
            "opened": _day(a), "closed": _day(b) if b else None,
            "status": "abierta" if p.status == "active" else "cerrada",
            "days_in_month": round(max(in_days, 0.0), 1),
            "funding": round(k["funding"], 2), "price": round(k["price"], 2), "fees": round(k["fees"], 2),
            "net": round(net, 2), "roc_pct": round(net / cap_p * 100, 3) if cap_p > 0 else None,
            "hist_id": hist_by_pos[p.id].id if p.id in hist_by_pos else None,
        })
    for h in history:      # cerradas sin posición de origen
        if h.id in links:
            continue
        hc = _ts(h.closed_at)
        if hc is None or not (t0 <= hc < t1):
            continue
        k = per_key.get(f"h{h.id}", {"funding": 0.0, "fees": 0.0, "price": 0.0})
        net = k["funding"] + k["fees"] + k["price"]
        cap_h = float(h.capital_used or 0)
        rows.append({"symbol": h.symbol, "exchange": h.exchange, "capital": round(cap_h, 2),
                     "opened": _day(hc - float(h.hours or 0) * 3600), "closed": _day(hc),
                     "status": "cerrada", "days_in_month": round(float(h.hours or 0) / 24, 1),
                     "funding": round(k["funding"], 2), "price": round(k["price"], 2),
                     "fees": round(k["fees"], 2), "net": round(net, 2),
                     "roc_pct": round(net / cap_h * 100, 3) if cap_h > 0 else None, "hist_id": h.id})
    rows.sort(key=lambda r: r["net"], reverse=True)

    # Abiertas: no realizado (mes en curso) y expectativa a 30 días.
    active = [p for p in positions if p.status == "active"]
    is_current = t0 <= now < t1
    unreal = None
    if is_current and unrealized:
        unreal = round(sum(float(unrealized.get(str(p.id)) or 0) for p in active), 2)
    exp_usd = 0.0
    exp_n = 0
    for p in active:
        apr = (outlook or {}).get(str(p.id))
        if apr is None:
            continue
        exp_usd += float(apr) / 100 * float(p.exposure or 0) * 30 / 365
        exp_n += 1
    cap_now = cap["end"] or cap["avg"]
    past = []
    for i in range(1, 7):            # rango propio: 6 meses previos con actividad
        y, m = int(month[:4]), int(month[5:]) - i
        while m <= 0:
            y, m = y - 1, m + 12
        a, b = month_bounds(f"{y:04d}-{m:02d}")
        n = _sum(ev, a, b)["net"]
        c = _capital(user_id, positions, history, links, a, b)["avg"]
        if c > 0 and any(a <= ts < b for ts, *_ in ev):
            past.append(n / c * 100)

    closed_in = [r for r in rows if r["closed"] and r["closed"][:7] == month]
    from core.database import db
    user = db.session.get(User, user_id)
    best = rows[0] if rows else None
    worst = rows[-1] if rows else None
    return {
        "month": month, "generated_at": datetime.fromtimestamp(now, timezone.utc).isoformat(),
        "investor": {"id": user_id, "email": getattr(user, "email", "")},
        "partial_month": is_current, "days": round(days, 1),
        "capital": cap,
        "pnl": {k: round(v, 2) for k, v in s.items()},
        "return_pct": round(ret, 3) if ret is not None else None,
        "return_ann_pct": round(ret * 365 / days, 2) if ret is not None and days >= 1 else None,
        "ytd": {"net": round(ys["net"], 2), "return_pct": round(ytd, 3) if ytd is not None else None,
                "capital_avg": ycap["avg"], "estimated": ycap["estimated"]},
        "utilization_pct": (round(cap["deployed_avg"] / cap["avg"] * 100, 1)
                            if cap["avg"] and cap["source"] != "deployed" else None),
        "positions": rows,
        "n_positions": len(rows), "n_closed": len(closed_in),
        "win_rate_pct": (round(sum(r["net"] > 0 for r in closed_in) / len(closed_in) * 100, 0)
                         if closed_in else None),
        "best": best, "worst": worst if worst is not best else None,
        "daily": [{"date": d["date"], "net": round(d["net"], 2)} for d in daily],
        "open_now": {"count": len(active), "capital": round(sum(float(p.capital_used or 0) for p in active), 2),
                     "unrealized_price_pnl": unreal},
        "expectation": {
            "next_30d_usd": round(exp_usd, 2) if exp_n else None,
            "next_30d_pct": round(exp_usd / cap_now * 100, 2) if exp_n and cap_now else None,
            "positions_with_outlook": exp_n,
            "own_range_pct": ({"min": round(min(past), 2), "median": round(sorted(past)[len(past) // 2], 2),
                               "max": round(max(past), 2), "months": len(past)} if past else None),
            "reference": "Backtest 12 m (1x, cartera 5 cupos): 1.7–6.7 % mensual, típico ~3 %; "
                         "mercado 2026 más comprimido.",
        },
    }
