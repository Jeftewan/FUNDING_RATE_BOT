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

    Delta-neutral sizing with leverage:
      Spot-Perp: spot_size = capital * lev / (lev+1), fut_margin = capital / (lev+1)
                 exposure = spot_size = fut_margin * lev (both sides equal)
      Cross-Ex:  margin_per_side = capital / 2, exposure = margin * lev
    """
    mode = opportunity.get("mode", "spot_perp")
    fr = abs(opportunity.get("funding_rate", opportunity.get("rate_differential", 0)))
    vol = opportunity.get("volume_24h", 1e6)
    price = opportunity.get("price", 0)

    if mode == "cross_exchange":
        ipd = min(opportunity.get("long_ppd", 3), opportunity.get("short_ppd", 3))
    else:
        ipd = opportunity.get("payments_per_day", 3)

    if mode == "spot_perp":
        fees = calculate_spot_perp_fees(opportunity["exchange"], capital, vol)
        exchange = opportunity["exchange"]
        # Delta-neutral: spot_size = fut_margin * leverage
        # spot_size + fut_margin = capital
        # → fut_margin = capital / (lev + 1)
        # → spot_size = capital * lev / (lev + 1)
        fut_margin = capital / (leverage + 1)
        spot_size = capital - fut_margin  # = capital * lev / (lev + 1)
        exposure = spot_size  # both sides have equal exposure
    else:
        fees = calculate_cross_exchange_fees(
            opportunity.get("long_exchange", ""),
            opportunity.get("short_exchange", ""),
            capital, vol,
        )
        exchange = opportunity.get("short_exchange", "")
        # Cross-exchange: both sides use futures with leverage
        # margin_per_side = capital / 2, exposure = margin * leverage
        fut_margin = capital / 2
        spot_size = 0
        exposure = fut_margin * leverage

    # Funding is paid on the EXPOSURE (notional), not the margin
    daily_income = exposure * fr * ipd
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
        "exposure": exposure,
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

    # Add sizing details for frontend
    if mode == "spot_perp":
        result["spot_size"] = spot_size
        result["fut_margin"] = fut_margin
        result["fut_exposure"] = exposure
    else:
        result["margin_per_side"] = fut_margin
        result["exposure_per_side"] = exposure

    result.update(sl_tp)
    result["entry_strategy"] = build_entry_strategy(opportunity, capital, mode, fees)
    return result


def build_entry_strategy(opp: dict, capital: float, mode: str, fees: dict) -> dict:
    """Compute entry-execution metrics and order-type recommendation."""
    mins_to_next = float(opp.get("mins_to_next") or 999)
    volume_24h = float(opp.get("volume_24h") or 1e6) or 1e6

    slip_pct = fees.get("slip_pct", 0)
    slip_source = fees.get("source", "table")

    per_leg = capital / 2
    book_impact_pct = per_leg / volume_24h * 100

    book_impact_level = (
        "high" if book_impact_pct >= 0.1
        else "medium" if book_impact_pct >= 0.05
        else "low"
    )

    window_status = (
        "red" if mins_to_next <= 10
        else "yellow" if mins_to_next <= 30
        else "green"
    )

    basis_pct = None
    if mode == "spot_perp":
        try:
            from analysis.slippage import fetch_spot_price
            perp_price = float(opp.get("price") or 0)
            spot_price = fetch_spot_price(opp.get("exchange", ""), opp.get("symbol", ""))
            if spot_price and spot_price > 0 and perp_price > 0:
                basis_pct = (perp_price - spot_price) / spot_price * 100
        except Exception:
            pass
    elif mode == "cross_exchange":
        long_price = float(opp.get("long_price") or 0)
        short_price = float(opp.get("short_price") or 0)
        if long_price > 0 and short_price > 0:
            mid = (long_price + short_price) / 2
            basis_pct = (short_price - long_price) / mid * 100

    if mode == "spot_perp":
        rec = "Limit SPOT (mid, 60s) → Market PERP al llenar"
    else:
        rec = "Limit simultáneo ambos exchanges (90s) → abortar si solo 1 llena"

    modifiers = []
    if book_impact_level == "high":
        modifiers.append("dividir en 2–3 chunks")
    if window_status == "red":
        modifiers.append("⚠ muy cerca del pago")
    if slip_pct > 0.3:
        modifiers.append("⚠ slippage alto, reduce capital")
    if basis_pct is not None and mode == "spot_perp" and basis_pct < -0.1:
        modifiers.append("⚠ basis negativo")
    if modifiers:
        rec += " · " + " · ".join(modifiers)

    return {
        "slippage_pct": round(slip_pct, 3),
        "slippage_source": slip_source,
        "book_impact_pct": round(book_impact_pct, 4),
        "book_impact_level": book_impact_level,
        "window_status": window_status,
        "mins_to_next": round(mins_to_next, 1),
        "basis_pct": round(basis_pct, 3) if basis_pct is not None else None,
        "recommendation": rec,
    }


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
        # Risk: price rises → short perp loses → liquidation
        # Exit plan: when price hits SL level, close BOTH sides:
        #   - Close short perp at a LOSS (SL)
        #   - Sell spot at a PROFIT (TP) — bought lower, sell higher
        # So SL perp price = TP spot price (same level, above entry)
        liq_dist_pct = (1 / leverage) - mm
        liq_price_short = price * (1 + liq_dist_pct)

        # SL on perp: 80% of the way to liquidation (price going UP)
        sl_price = price * (1 + liq_dist_pct * SL_SAFETY_PCT)
        sl_pct = (sl_price / price - 1) * 100

        # TP on spot: SAME price as SL perp (price went UP = spot profit)
        tp_price = sl_price
        tp_pct = sl_pct

        return {
            "sl_tp": {
                "mode": "spot_perp",
                "entry_price": round(price, 4),
                "perp_sl_price": round(sl_price, 4),
                "perp_sl_pct": round(sl_pct, 2),
                "perp_liq_price": round(liq_price_short, 4),
                "spot_tp_price": round(tp_price, 4),
                "spot_tp_pct": round(tp_pct, 2),
                "liq_dist_pct": round(liq_dist_pct * 100, 2),
            }
        }
    else:
        # Cross-exchange: Long futures A + Short futures B (hedged)
        # Long: profits when price rises, SL when price drops
        # Short: profits when price drops, SL when price rises
        # TP of one side = SL price of the other side (close both together)
        #   When short hits SL (price UP) → long profits → long TP
        #   When long hits SL (price DOWN) → short profits → short TP
        long_ex = opp.get("long_exchange", "")
        short_ex = opp.get("short_exchange", "")
        mm_long = MAINTENANCE_MARGIN.get(long_ex, 0.005)
        mm_short = MAINTENANCE_MARGIN.get(short_ex, 0.005)

        long_price = opp.get("long_price", price)
        short_price = opp.get("short_price", price)

        # Long side: liquidation if price drops
        long_liq_dist = (1 / leverage) - mm_long
        long_liq_price = long_price * (1 - long_liq_dist)
        long_sl_price = long_price * (1 - long_liq_dist * SL_SAFETY_PCT)
        long_sl_pct = (1 - long_sl_price / long_price) * 100

        # Short side: liquidation if price rises
        short_liq_dist = (1 / leverage) - mm_short
        short_liq_price = short_price * (1 + short_liq_dist)
        short_sl_price = short_price * (1 + short_liq_dist * SL_SAFETY_PCT)
        short_sl_pct = (short_sl_price / short_price - 1) * 100

        # TP: mirror of the other side's SL
        # Long TP: price goes UP to short's SL level → close long at profit
        # Use short's SL % move applied to long's entry
        long_tp_price = long_price * (1 + short_liq_dist * SL_SAFETY_PCT)
        long_tp_pct = (long_tp_price / long_price - 1) * 100

        # Short TP: price goes DOWN to long's SL level → close short at profit
        # Use long's SL % move applied to short's entry
        short_tp_price = short_price * (1 - long_liq_dist * SL_SAFETY_PCT)
        short_tp_pct = (1 - short_tp_price / short_price) * 100

        return {
            "sl_tp": {
                "mode": "cross_exchange",
                "long_entry": round(long_price, 4),
                "long_liq_price": round(long_liq_price, 4),
                "long_sl_price": round(long_sl_price, 4),
                "long_sl_pct": round(long_sl_pct, 2),
                "long_tp_price": round(long_tp_price, 4),
                "long_tp_pct": round(long_tp_pct, 2),
                "short_entry": round(short_price, 4),
                "short_liq_price": round(short_liq_price, 4),
                "short_sl_price": round(short_sl_price, 4),
                "short_sl_pct": round(short_sl_pct, 2),
                "short_tp_price": round(short_tp_price, 4),
                "short_tp_pct": round(short_tp_pct, 2),
                "liq_dist_pct": round(min(long_liq_dist, short_liq_dist) * 100, 2),
            }
        }

