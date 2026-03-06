"""Action recommendation engine — OPEN/EXIT/ROTATE with step-by-step instructions."""
import logging
from analysis.fees import calculate_returns
from portfolio.manager import get_budget_breakdown

log = logging.getLogger("bot")


def generate_actions(state: dict) -> list:
    """Generate action recommendations based on current state."""
    actions = []
    bd = get_budget_breakdown(state)
    positions = state["positions"]
    all_data = state.get("all_data", [])
    safe_top = state.get("safe_top", [])
    aggr_top = state.get("aggr_top", [])

    # Check existing positions for EXIT/ROTATE
    for i, pos in enumerate(positions):
        cur = next(
            (d for d in all_data
             if d["symbol"] == pos["symbol"] and d["exchange"] == pos["exchange"]),
            None,
        )
        if not cur:
            continue

        cfr = cur["fr"]
        fr_rev = ((pos["entry_fr"] > 0 and cfr < 0) or
                  (pos["entry_fr"] < 0 and cfr > 0))
        fr_drop = abs(cfr) < abs(pos["entry_fr"]) * 0.25 and not fr_rev
        cc = calculate_returns(cur, pos["capital_used"])

        # SL check for aggressive
        if pos["carry"] == "Reverse" and pos.get("sl_pct", 0) > 0:
            cp = cur["price"]
            ep = pos["entry_price"]
            if ep > 0:
                price_drop = ((ep - cp) / ep) * 100
                if price_drop >= pos["sl_pct"]:
                    actions.append({
                        "pri": 0, "type": "EXIT", "idx": i, "critical": True,
                        "title": f"⛔ SL AGRESIVA: {pos['symbol']} — Precio cayo {price_drop:.2f}% (SL: {pos['sl_pct']:.2f}%)",
                        "detail": f"Entrada: ${ep:.4f} → Ahora: ${cp:.4f}",
                        "steps": [], "costs": "", "warning": "", "countdown": "",
                    })
                    continue

        if fr_rev:
            actions.append({
                "pri": 0, "type": "EXIT", "idx": i, "critical": True,
                "title": f"⛔ CERRAR {pos['symbol']} ({pos['exchange']}) — Funding cambio de signo",
                "detail": f"Entrada: {pos['entry_fr']*100:.4f}% → Ahora: {cfr*100:.4f}%",
                "steps": [], "costs": "", "warning": "", "countdown": "",
            })
        elif fr_drop:
            better = None
            pool = safe_top if pos["carry"] == "Positive" else aggr_top
            for opp in pool:
                if opp["token"]["symbol"] != pos["symbol"]:
                    oc = calculate_returns(opp["token"], pos["capital_used"])
                    if oc["apr"] > cc["apr"] * 2:
                        better = opp
                        break
            if better:
                bc = calculate_returns(better["token"], pos["capital_used"])
                actions.append({
                    "pri": 1, "type": "ROTATE", "idx": i, "critical": False,
                    "title": f"🔄 ROTAR: {pos['symbol']} → {better['token']['symbol']} ({better['token']['exchange']})",
                    "detail": f"APR: {cc['apr']:.1f}% → {bc['apr']:.1f}%",
                    "new_sym": better["token"]["symbol"],
                    "new_exch": better["token"]["exchange"],
                    "steps": [], "costs": "", "warning": "", "countdown": "",
                })

    # OPEN actions
    _add_open_actions(actions, safe_top, state, bd, "safe", state["min_apr_safe"], 3)
    _add_open_actions(actions, aggr_top, state, bd, "aggr", state["min_apr_aggr"], 4)

    if not actions and not positions:
        actions.append({
            "pri": 9, "type": "WAIT", "critical": False,
            "title": "⏳ Sin oportunidades — Esperando mejor mercado",
            "detail": f"Min: Safe APR>{state['min_apr_safe']}% | Aggr APR>{state['min_apr_aggr']}%",
            "steps": [], "costs": "", "warning": "", "countdown": "",
        })

    actions.sort(key=lambda x: x["pri"])
    return actions


def _add_open_actions(actions, pool, state, bd, carry_label, min_apr, pri):
    """Add OPEN action recommendations."""
    positions = state["positions"]
    max_key = "max_pos_safe" if carry_label == "safe" else "max_pos_aggr"
    count_key = "sc" if carry_label == "safe" else "ac"
    avail_key = "sa" if carry_label == "safe" else "aa"

    slots = state[max_key] - bd[count_key]
    cap_avail = bd[avail_key]
    if slots <= 0 or cap_avail <= 20:
        return

    cpp = cap_avail / slots
    skipped = state.get("skipped_tokens", [])
    added = 0

    for opp in pool:
        if added >= slots:
            break
        t = opp["token"]
        skip_key = f"{t['symbol']}_{t['exchange']}"
        if skip_key in skipped:
            continue

        c = calculate_returns(t, cpp)
        if not c["worthwhile"] or c["apr"] < min_apr or opp["score"] < state["min_score"]:
            continue
        if any(p["symbol"] == t["symbol"] and p["exchange"] == t["exchange"]
               for p in positions):
            continue

        emoji = "🛡️" if carry_label == "safe" else "⚡"

        if c["carry"] == "Positive":
            steps = [
                f"1. COMPRA {t['symbol']} en SPOT por ${c['spot']:.2f}",
                f"2. Abre SHORT {t['symbol']}USDT PERPETUO por ${c['fut']:.2f}",
                f"   → Leverage: 1x | Cross Margin",
            ]
        else:
            steps = [
                f"1. Abre LONG {t['symbol']}USDT PERPETUO por ${c['fut']:.2f}",
                f"   → Leverage: 1x | Cross Margin",
                f"2. STOP LOSS: -{c['sl_pct']:.2f}% (ganancia max 24h en funding)",
            ]

        rsi_info = ""
        if carry_label == "aggr":
            rsi_val = opp.get("rsi", -1)
            rsi_info = f" | RSI: {rsi_val:.0f}" if rsi_val >= 0 else ""

        actions.append({
            "pri": pri, "type": "OPEN", "carry": carry_label, "critical": False,
            "title": f"{emoji} ABRIR: {t['symbol']}/USDT en {t['exchange']}",
            "detail": f"APR: {c['apr']:.1f}% | ${c['fd']:.2f}/dia | BE: {c['be']:.1f}d | Score: {opp['score']}/100{rsi_info}",
            "steps": steps,
            "costs": f"Fees: ${c['total_fees']:.2f} | Slip: ~${c['slip_cost']:.2f} ({c['slip_pct']:.2f}%) | Total: ${c['total_cost']:.2f}",
            "countdown": f"⏱ Proximo cobro en {int(t['mins_next'])}min" if t.get("mins_next", 0) > 0 else "",
            "warning": f"⚠ SL: -{c['sl_pct']:.2f}% | Compensa caidas ~{c['mdp']:.2f}%/dia" if c["carry"] == "Reverse" else "",
            "symbol": t["symbol"], "exchange": t["exchange"],
            "capital": cpp, "fr": t["fr"], "price": t["price"],
            "ih": c["ih"], "carry_type": c["carry"], "sl_pct": c["sl_pct"],
        })
        added += 1


def generate_spot_perp_instructions(opp, capital: float) -> dict:
    """Generate detailed step-by-step instructions for spot-perp."""
    spot_size = capital / 2
    fut_size = capital / 2

    return {
        "mode": "spot_perp",
        "exchange": opp.exchange,
        "steps": [
            f"1. Transferir ${capital:.0f} USDT a {opp.exchange}",
            f"2. Comprar {opp.symbol} en SPOT por ${spot_size:.2f} USDT",
            f"3. Abrir SHORT {opp.symbol}/USDT perpetuo por ${fut_size:.2f} USDT",
            f"4. Configurar Cross Margin, Leverage 1x",
            f"5. Monitorear funding rate cada {opp.interval_hours}h",
        ],
        "position_size": {"spot": spot_size, "futures": fut_size},
        "expected_daily_income": opp.daily_income_per_1000 * (capital / 1000),
        "expected_3day_income": opp.net_3d_revenue_per_1000 * (capital / 1000),
        "fee_estimate": opp.fees_total * (capital / 1000),
        "break_even_hours": opp.break_even_hours,
        "risks": [
            "Funding rate puede revertir — monitorear cada cobro",
            "Spread spot-futures puede ampliarse al cerrar",
        ],
        "exit_conditions": [
            "Funding rate negativo por 2+ cobros consecutivos",
            "Revenue 3 dias proyectado < fees",
        ],
    }


def generate_cross_exchange_instructions(opp, capital: float) -> dict:
    """Generate detailed instructions for cross-exchange."""
    per_side = capital / 2

    return {
        "mode": "cross_exchange",
        "long_exchange": opp.long_exchange,
        "short_exchange": opp.short_exchange,
        "steps": [
            f"1. Transferir ${per_side:.0f} USDT a {opp.long_exchange} (cuenta futures)",
            f"2. Transferir ${per_side:.0f} USDT a {opp.short_exchange} (cuenta futures)",
            f"3. Abrir LONG {opp.symbol}/USDT perpetuo en {opp.long_exchange} por ${per_side:.2f}",
            f"4. Abrir SHORT {opp.symbol}/USDT perpetuo en {opp.short_exchange} por ${per_side:.2f}",
            f"5. Configurar Cross Margin 1x en ambos exchanges",
            f"6. Monitorear diferencial de funding rates",
        ],
        "position_size": {
            "long": per_side, "short": per_side, "total": capital
        },
        "expected_daily_income": opp.daily_income_per_1000 * (capital / 1000),
        "expected_3day_income": opp.net_3d_revenue_per_1000 * (capital / 1000),
        "fee_estimate": opp.total_fees * (capital / 1000),
        "break_even_hours": opp.break_even_hours,
        "risks": [
            f"Riesgo de liquidacion: {opp.liquidation_risk}",
            "Diferencial puede invertirse",
            "Margen dividido entre 2 exchanges — riesgo alto con capital < $5,000",
        ],
        "exit_conditions": [
            "Diferencial se invierte por 2+ cobros",
            "Una posicion se acerca a liquidacion",
        ],
    }
