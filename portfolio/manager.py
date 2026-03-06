"""Position tracking, earnings accumulation, budget breakdown."""
import time
import logging
from datetime import datetime

log = logging.getLogger("bot")


def get_budget_breakdown(state: dict) -> dict:
    """Calculate budget allocation and usage."""
    t = state["total_capital"]
    sb = t * (state["safe_pct"] / 100)
    ab = t * (state["aggr_pct"] / 100)
    su = sum(p["capital_used"] for p in state["positions"] if p["carry"] == "Positive")
    au = sum(p["capital_used"] for p in state["positions"] if p["carry"] == "Reverse")
    sc = sum(1 for p in state["positions"] if p["carry"] == "Positive")
    ac = sum(1 for p in state["positions"] if p["carry"] == "Reverse")
    return {
        "total": t, "sb": sb, "ab": ab, "su": su, "au": au,
        "sa": max(0, sb - su), "aa": max(0, ab - au), "sc": sc, "ac": ac,
    }


def update_position_earnings(state: dict, all_data: list) -> None:
    """Accumulate real earnings using current rate at each scan."""
    now = time.time()
    for pos in state["positions"]:
        cur = next(
            (d for d in all_data
             if d["symbol"] == pos["symbol"] and d["exchange"] == pos["exchange"]),
            None,
        )
        if not cur:
            continue

        ih = pos.get("ih", 8)
        last_up = pos.get("last_earn_update", pos["entry_time"] / 1000)
        elapsed_h = (now - last_up) / 3600
        full_ivs = int(elapsed_h / ih)
        if full_ivs < 1:
            continue

        cfr = cur["fr"]
        is_pos_carry = pos["carry"] == "Positive"
        if is_pos_carry and cfr > 0:
            fut_size = pos["capital_used"] / 2
            earn_per_iv = fut_size * cfr
        elif not is_pos_carry and cfr < 0:
            fut_size = pos["capital_used"]
            earn_per_iv = fut_size * abs(cfr)
        else:
            earn_per_iv = 0

        earned_now = earn_per_iv * full_ivs
        pos["earned_real"] = pos.get("earned_real", 0) + earned_now
        pos["last_earn_update"] = now
        pos["last_fr_used"] = cfr
        if earned_now > 0:
            log.info(f"  +${earned_now:.4f} {pos['symbol']} ({full_ivs}ivs @ {cfr*100:.4f}%)")


def close_position(state: dict, idx: int) -> tuple:
    """Close position at index, returns (ok, msg)."""
    if idx < 0 or idx >= len(state["positions"]):
        return False, "Posicion invalida"

    pos = state["positions"][idx]
    ih = pos.get("ih", 8)
    el_h = (time.time() - pos["entry_time"] / 1000) / 3600
    ivs = int(el_h / ih)
    est = pos.get("earned_real", 0)

    state["history"].append({
        "symbol": pos["symbol"], "exchange": pos["exchange"],
        "carry": pos["carry"], "hours": el_h, "intervals": ivs,
        "earned": est, "time": datetime.now().isoformat(),
    })
    state["total_earned"] = state.get("total_earned", 0) + est
    sym = pos["symbol"]
    state["positions"].pop(idx)
    log.info(f"Closed: {sym} earned ${est:.4f}")
    return True, f"✅ {sym} cerrada. Ganado: ${est:.2f}"
