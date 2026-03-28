"""Position tracking and earnings — v8.0 unified (no safe/aggr budget split)."""
import time
import logging
import uuid
from datetime import datetime
from analysis.fees import calculate_spot_perp_fees, calculate_cross_exchange_fees

log = logging.getLogger("bot")


def get_capital_summary(state: dict) -> dict:
    """Calculate capital usage summary."""
    total = state["total_capital"]
    used = sum(p["capital_used"] for p in state["positions"])
    available = max(0, total - used)
    count = len(state["positions"])
    return {
        "total": total,
        "used": used,
        "available": available,
        "count": count,
        "max_positions": state.get("max_positions", 5),
    }


def open_position(state: dict, opportunity: dict, capital: float) -> tuple:
    """Open a new position from an opportunity.

    Returns (ok, result_dict_or_error_msg).
    """
    summary = get_capital_summary(state)

    if capital <= 0:
        return False, "Capital debe ser mayor a 0"
    if capital > summary["available"]:
        return False, f"Capital insuficiente. Disponible: ${summary['available']:.2f}"
    if summary["count"] >= summary["max_positions"]:
        return False, f"Maximo de posiciones alcanzado ({summary['max_positions']})"

    mode = opportunity.get("mode", "spot_perp")
    pos_id = str(uuid.uuid4())[:8]
    now_ms = int(time.time() * 1000)

    pos = {
        "id": pos_id,
        "symbol": opportunity["symbol"],
        "exchange": opportunity.get("exchange", ""),
        "mode": mode,
        "entry_fr": opportunity.get("funding_rate", opportunity.get("rate_differential", 0)),
        "entry_price": opportunity.get("price", 0),
        "entry_time": now_ms,
        "capital_used": capital,
        "ih": opportunity.get("interval_hours", 8),
        "earned_real": 0,
        "last_earn_update": time.time(),
        "last_fr_used": 0,
        "payments": [],
        "payment_count": 0,
        "avg_rate": 0,
        "status": "active",
    }

    # Mode-specific fields
    if mode == "cross_exchange":
        pos["long_exchange"] = opportunity.get("long_exchange", "")
        pos["short_exchange"] = opportunity.get("short_exchange", "")
        pos["exchange"] = opportunity.get("short_exchange", "")  # Primary for lookups

    # Calculate fees for reference
    if mode == "spot_perp":
        fees = calculate_spot_perp_fees(
            pos["exchange"], capital, opportunity.get("volume_24h", 1e6)
        )
    else:
        fees = calculate_cross_exchange_fees(
            pos.get("long_exchange", ""), pos.get("short_exchange", ""),
            capital, opportunity.get("volume_24h", 1e6)
        )

    pos["entry_fees"] = fees["total_cost"]

    # Generate execution steps
    steps = _generate_steps(pos, opportunity, capital)

    # Calculate estimates
    fr = abs(pos["entry_fr"])
    ipd = opportunity.get("payments_per_day", 3)
    fut_size = capital / 2
    daily_income = fut_size * fr * ipd
    est_3day = daily_income * 3
    break_even_h = fees["total_cost"] / (daily_income / 24) if daily_income > 0 else 999

    # Don't append to state — caller saves to DB
    # state["positions"].append(pos) is handled by the API route via DBPersistence

    return True, {
        "position": pos,
        "steps": steps,
        "estimated_daily": daily_income,
        "estimated_3day": est_3day,
        "fees_total": fees["total_cost"],
        "break_even_hours": break_even_h,
    }


def _generate_steps(pos: dict, opp: dict, capital: float) -> list:
    """Generate step-by-step execution instructions."""
    mode = pos["mode"]
    symbol = pos["symbol"]

    if mode == "spot_perp":
        spot_size = capital / 2
        fut_size = capital / 2
        exchange = pos["exchange"]
        return [
            f"1. Transferir ${capital:.0f} USDT a {exchange}",
            f"2. Comprar {symbol} en SPOT por ${spot_size:.2f} USDT",
            f"3. Abrir SHORT {symbol}/USDT perpetuo por ${fut_size:.2f} USDT",
            f"4. Configurar Cross Margin, Leverage 1x",
            f"5. El bot monitoreara los pagos cada {pos['ih']}h",
        ]
    else:
        per_side = capital / 2
        long_ex = pos.get("long_exchange", "")
        short_ex = pos.get("short_exchange", "")
        return [
            f"1. Transferir ${per_side:.0f} USDT a {long_ex} (cuenta futures)",
            f"2. Transferir ${per_side:.0f} USDT a {short_ex} (cuenta futures)",
            f"3. Abrir LONG {symbol}/USDT perpetuo en {long_ex} por ${per_side:.2f}",
            f"4. Abrir SHORT {symbol}/USDT perpetuo en {short_ex} por ${per_side:.2f}",
            f"5. Configurar Cross Margin 1x en ambos exchanges",
            f"6. El bot monitoreara el diferencial de funding",
        ]


def close_position(state: dict, position_id: str, reason: str = "manual") -> tuple:
    """Close position by ID, returns (ok, msg)."""
    idx = None
    for i, p in enumerate(state["positions"]):
        if p.get("id") == position_id:
            idx = i
            break

    if idx is None:
        # Fallback: try by index (backward compat)
        try:
            idx = int(position_id)
            if idx < 0 or idx >= len(state["positions"]):
                return False, "Posicion no encontrada"
        except (ValueError, TypeError):
            return False, "Posicion no encontrada"

    pos = state["positions"][idx]
    ih = pos.get("ih", 8)
    el_h = (time.time() - pos["entry_time"] / 1000) / 3600
    ivs = int(el_h / ih)
    earned = pos.get("earned_real", 0)
    fees = pos.get("entry_fees", 0) * 2  # Entry + estimated exit fees
    net_earned = earned - fees

    state["history"].append({
        "id": pos.get("id", ""),
        "symbol": pos["symbol"],
        "exchange": pos["exchange"],
        "mode": pos.get("mode", "spot_perp"),
        "capital_used": pos["capital_used"],
        "hours": el_h,
        "intervals": ivs,
        "payment_count": pos.get("payment_count", ivs),
        "earned": earned,
        "fees": fees,
        "net_earned": net_earned,
        "avg_rate": pos.get("avg_rate", 0),
        "reason": reason,
        "closed_at": datetime.now().isoformat(),
    })
    state["total_earned"] = state.get("total_earned", 0) + earned
    sym = pos["symbol"]
    state["positions"].pop(idx)
    log.info(f"Closed: {sym} earned ${earned:.4f} (net ${net_earned:.4f}) reason={reason}")
    return True, {
        "symbol": sym,
        "earned": earned,
        "fees": fees,
        "net_earned": net_earned,
        "hours": el_h,
        "payments": pos.get("payment_count", ivs),
    }
