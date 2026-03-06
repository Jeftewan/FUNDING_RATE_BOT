"""Risk assessment — liquidation, rate reversal alerts."""
import time
import logging

log = logging.getLogger("bot")


def check_rate_reversal(pos: dict, current_rate: float) -> bool:
    """Check if funding rate has reversed direction."""
    return ((pos["entry_fr"] > 0 and current_rate < 0) or
            (pos["entry_fr"] < 0 and current_rate > 0))


def check_stop_loss(pos: dict, current_price: float) -> bool:
    """Check if stop loss has been hit (aggressive positions)."""
    if pos["carry"] != "Reverse" or pos.get("sl_pct", 0) <= 0:
        return False
    ep = pos["entry_price"]
    if ep <= 0:
        return False
    price_drop = ((ep - current_price) / ep) * 100
    return price_drop >= pos["sl_pct"]


def calculate_liquidation_price(entry_price: float, leverage: int,
                                side: str = "long") -> float:
    """Estimate liquidation price for a futures position."""
    if leverage <= 0:
        return 0
    margin_pct = 1.0 / leverage
    if side == "long":
        return entry_price * (1 - margin_pct + 0.006)  # +0.6% maintenance margin
    else:
        return entry_price * (1 + margin_pct - 0.006)


def generate_alerts(positions: list, all_data: list) -> list:
    """Generate alerts for all active positions."""
    alerts = []

    for i, pos in enumerate(positions):
        cur = next(
            (d for d in all_data
             if d["symbol"] == pos["symbol"] and d["exchange"] == pos["exchange"]),
            None,
        )
        if not cur:
            continue

        cfr = cur["fr"]
        cp = cur["price"]

        # Rate reversal
        if check_rate_reversal(pos, cfr):
            alerts.append({
                "type": "RATE_REVERSAL",
                "severity": "CRITICAL",
                "position_idx": i,
                "symbol": pos["symbol"],
                "exchange": pos["exchange"],
                "message": f"Funding rate cambio de signo: {pos['entry_fr']*100:.4f}% → {cfr*100:.4f}%",
            })

        # Stop loss
        if check_stop_loss(pos, cp):
            alerts.append({
                "type": "STOP_LOSS",
                "severity": "CRITICAL",
                "position_idx": i,
                "symbol": pos["symbol"],
                "exchange": pos["exchange"],
                "message": f"Stop loss alcanzado: precio cayo de ${pos['entry_price']:.2f} a ${cp:.2f}",
            })

        # Rate significantly dropped (> 75%)
        if abs(cfr) < abs(pos["entry_fr"]) * 0.25:
            alerts.append({
                "type": "RATE_DROP",
                "severity": "WARNING",
                "position_idx": i,
                "symbol": pos["symbol"],
                "exchange": pos["exchange"],
                "message": f"Rate cayo >75%: {pos['entry_fr']*100:.4f}% → {cfr*100:.4f}%",
            })

    return alerts
