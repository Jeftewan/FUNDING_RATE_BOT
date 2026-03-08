"""Risk assessment — rate reversal and pre-payment alerts."""
import logging

log = logging.getLogger("bot")


def check_rate_reversal(pos: dict, current_rate: float) -> bool:
    """Check if funding rate has reversed direction."""
    return ((pos["entry_fr"] > 0 and current_rate < 0) or
            (pos["entry_fr"] < 0 and current_rate > 0))


def calculate_liquidation_price(entry_price: float, leverage: int,
                                side: str = "long") -> float:
    """Estimate liquidation price for a futures position."""
    if leverage <= 0:
        return 0
    margin_pct = 1.0 / leverage
    if side == "long":
        return entry_price * (1 - margin_pct + 0.006)
    else:
        return entry_price * (1 + margin_pct - 0.006)
