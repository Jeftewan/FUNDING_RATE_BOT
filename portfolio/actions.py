"""Position calculation helpers — v8.0 (no auto-generated actions)."""
from analysis.fees import (calculate_spot_perp_fees, calculate_cross_exchange_fees,
                           calculate_break_even_hours)

# Maintenance margin rates by exchange (approximate)
MAINTENANCE_MARGIN = {
    "Binance": 0.004,  # 0.4%
    "Bybit": 0.005,    # 0.5%
    "OKX": 0.004,
    "Bitget": 0.005,
}
# Safety buffer: set SL at 80% of the distance to liquidation
SL_SAFETY_PCT = 0.80


def calculate_position_estimate(opportunity: dict, capital: float,
                                leverage: int = 1) -> dict:
    """Calculate estimated returns + SL/TP for a given opportunity.

    Returns detailed breakdown for the frontend calculator.
    """
    mode = opportunity.get("mode", "spot_perp")
    fr = abs(opportunity.get("funding_rate", opportunity.get("rate_differential", 0)))
    ipd = opportunity.get("payments_per_day", 3)
    vol = opportunity.get("volume_24h", 1e6)
    price = opportunity.get("price", 0)

    if mode == "spot_perp":
        fees = calculate_spot_perp_fees(opportunity["exchange"], capital, vol)
        exchange = opportunity["exchange"]
    else:
        fees = calculate_cross_exchange_fees(
            opportunity.get("long_exchange", ""),
            opportunity.get("short_exchange", ""),
            capital, vol,
        )
        exchange = opportunity.get("short_exchange", "")

    # With leverage: position size = capital * leverage / 2 each side
    fut_size = (capital * leverage) / 2

    daily_income = fut_size * fr * ipd
    income_3day = daily_income * 3
    apr = (daily_income * 365 / capital * 100) if capital > 0 else 0
    hourly = daily_income / 24
    break_even_h = calculate_break_even_hours(fees["total_cost"], hourly)

    # Calculate SL/TP based on leverage and liquidation price
    sl_tp = _calculate_sl_tp(price, leverage, exchange, mode, opportunity)

    result = {
        "capital": capital,
        "leverage": leverage,
        "mode": mode,
        "funding_rate": fr,
        "position_size": fut_size * 2,
        "daily_income": daily_income,
        "income_3day": income_3day,
        "income_7day": daily_income * 7,
        "apr": apr,
        "fees_total": fees["total_cost"],
        "fees_detail": {
            "trading_fees": fees["total_fees"],
            "slippage": fees["slip_cost"],
            "slip_pct": fees["slip_pct"],
        },
        "break_even_hours": break_even_h,
        "net_3day": income_3day - fees["total_cost"],
        "net_daily": daily_income - (fees["total_cost"] / 3),
    }
    result.update(sl_tp)
    return result


def _calculate_sl_tp(price: float, leverage: int, exchange: str,
                     mode: str, opp: dict) -> dict:
    """Calculate stop loss and take profit levels.

    For spot-perp (long spot + short futures):
      - Risk is on the SHORT futures side → liquidation if price rises
      - SL on short = before liquidation price (above entry)

    For cross-exchange (long futures A + short futures B):
      - Long side: liquidation if price drops
      - Short side: liquidation if price rises
      - Need SL for both sides

    SL is set at SL_SAFETY_PCT (80%) of the distance to liquidation.
    TP is set symmetrically on the profitable side.
    """
    if price <= 0 or leverage < 1:
        return {"sl_tp": None}

    mm = MAINTENANCE_MARGIN.get(exchange, 0.005)

    if mode == "spot_perp":
        # Spot-perp hedge: Long SPOT + Short PERP
        # SL is on the PERP (short) side — liquidation if price rises
        # TP is on the SPOT side — if price drops, spot loses but short gains
        # (symmetric: closing both at TP locks profit on perp side)
        liq_dist_pct = (1 / leverage) - mm
        liq_price_short = price * (1 + liq_dist_pct)

        # SL on perp: 80% of the way to liquidation (price going UP)
        sl_price = price * (1 + liq_dist_pct * SL_SAFETY_PCT)
        sl_pct = (sl_price / price - 1) * 100

        # TP on spot: symmetric distance below entry (price going DOWN)
        # When price drops this much, close both — perp profits, spot loses
        tp_price = price * (1 - liq_dist_pct * SL_SAFETY_PCT)
        tp_pct = (1 - tp_price / price) * 100

        return {
            "sl_tp": {
                "mode": "spot_perp",
                "entry_price": price,
                "perp_sl_price": round(sl_price, 4),
                "perp_sl_pct": round(sl_pct, 2),
                "perp_liq_price": round(liq_price_short, 4),
                "spot_tp_price": round(tp_price, 4),
                "spot_tp_pct": round(tp_pct, 2),
                "liq_dist_pct": round(liq_dist_pct * 100, 2),
            }
        }
    else:
        # Cross-exchange: Long futures A + Short futures B
        # Each side's TP = mirror of the other side's SL
        # When one side hits SL (loss), the other side profits equivalently
        long_ex = opp.get("long_exchange", "")
        short_ex = opp.get("short_exchange", "")
        mm_long = MAINTENANCE_MARGIN.get(long_ex, 0.005)
        mm_short = MAINTENANCE_MARGIN.get(short_ex, 0.005)

        long_price = opp.get("long_price", price)
        short_price = opp.get("short_price", price)

        # Long side: liq if price drops
        long_liq_dist = (1 / leverage) - mm_long
        long_liq_price = long_price * (1 - long_liq_dist)
        long_sl_price = long_price * (1 - long_liq_dist * SL_SAFETY_PCT)
        long_sl_pct = (1 - long_sl_price / long_price) * 100

        # Short side: liq if price rises
        short_liq_dist = (1 / leverage) - mm_short
        short_liq_price = short_price * (1 + short_liq_dist)
        short_sl_price = short_price * (1 + short_liq_dist * SL_SAFETY_PCT)
        short_sl_pct = (short_sl_price / short_price - 1) * 100

        # TP: one side's TP is when the OTHER side hits its SL
        # Long TP = price goes UP (same % as short's SL distance)
        long_tp_price = long_price * (1 + short_liq_dist * SL_SAFETY_PCT)
        long_tp_pct = (long_tp_price / long_price - 1) * 100

        # Short TP = price goes DOWN (same % as long's SL distance)
        short_tp_price = short_price * (1 - long_liq_dist * SL_SAFETY_PCT)
        short_tp_pct = (1 - short_tp_price / short_price) * 100

        return {
            "sl_tp": {
                "mode": "cross_exchange",
                "long_entry": long_price,
                "long_liq_price": round(long_liq_price, 4),
                "long_sl_price": round(long_sl_price, 4),
                "long_sl_pct": round(long_sl_pct, 2),
                "long_tp_price": round(long_tp_price, 4),
                "long_tp_pct": round(long_tp_pct, 2),
                "short_entry": short_price,
                "short_liq_price": round(short_liq_price, 4),
                "short_sl_price": round(short_sl_price, 4),
                "short_sl_pct": round(short_sl_pct, 2),
                "short_tp_price": round(short_tp_price, 4),
                "short_tp_pct": round(short_tp_pct, 2),
                "liq_dist_pct": round(min(long_liq_dist, short_liq_dist) * 100, 2),
            }
        }

