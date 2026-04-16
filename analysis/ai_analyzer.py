"""AI-powered opportunity analysis via Groq (Llama 3.3 70B)."""

import json
import logging
import random
import re

log = logging.getLogger(__name__)

GROQ_MODEL = "llama-3.3-70b-versatile"
MAX_OPPS = 5
TIMEOUT = 25

SYSTEM_PROMPT = (
    "Eres analista experto en arbitraje de funding rates. Tu trabajo es evaluar "
    "oportunidades y dar una explicacion clara que ayude al usuario a decidir.\n\n"
    "CONTEXTO DEL SCORING (v10.3 — recalibrado con backtest de 90 dias):\n"
    "El score ya incorpora estos hallazgos validados empiricamente:\n"
    "- CONSISTENCIA es el predictor #1 (Spearman 0.55) — mas importante que cualquier otra cosa\n"
    "- ESTABILIDAD es #2 (Spearman 0.31)\n"
    "- YIELD y LIQUIDEZ tuvieron correlacion NEGATIVA con retornos futuros (Pearson -0.35/-0.28)\n"
    "- ACELERACION del momentum es SEÑAL INVERTIDA: accel ON mostro -247% APR vs -25% OFF\n"
    "- z-score >=1.5 predice -264% APR (reversion severa)\n"
    "El score trae hard caps anti-spike: si ves score<=40 probablemente z>2, score<=45 probable "
    "racha inmadura en percentil alto, score<=50 probable current_rate >1.5x media historica.\n\n"
    "Responde SOLO JSON valido:\n"
    '{"analyses":[{"id":"_id","signal":"COMPRAR|MANTENER|EVITAR",'
    '"confidence":1-10,"analysis":"texto explicativo 40-60 palabras"}]}\n\n'
    "CAMPOS que recibiras:\n"
    "sc=score(0-100), apr=retorno anual%, beh=horas para recuperar fees, "
    "d1k=ingreso diario por $1000, n3d=ingreso neto 3 dias por $1000, "
    "vol=volumen 24h en millones USD, fr/diff=funding rate actual%, "
    "mom=momentum(accelerating/decelerating/flat/negative), z=z-score(desviacion vs media), "
    "pct=percentil historico, reg=regimen volatilidad, grade=estabilidad(A/B/C/D), "
    "ehd=dias estimados que se mantendra favorable, con=consistencia%(periodos favorables), "
    "fd=fee drag(fees/ganancia bruta, menor=mejor), spike/rev=flags de spike\n\n"
    "REGLAS de decision:\n"
    "COMPRAR: sc>=60 + con>=70 + z<1.0 + mom!=accelerating + mom!=negative + "
    "beh<10h + vol>5M + grade in (A,B) + yield_diario in zona sana\n"
    "EVITAR: z>=1.5 | mom=accelerating | mom=negative | rev=true | spike=true | "
    "con<50 | grade=D | sc<=45 | beh>15h | yield_diario>0.25%\n"
    "MANTENER: no cumple COMPRAR ni EVITAR (sc 50-60, signales mixtas)\n\n"
    "RIESGOS CLAVE (no negociables):\n"
    "- mom=accelerating: backtest probo que es INVERSO. El momentum acelerando captura "
    "el pico del spike justo antes de la reversion. Tratar como bandera roja.\n"
    "- z>=1.5: tasa muy alejada de la media, reversion probable (-264% APR historico)\n"
    "- z>=2.0: reversion casi garantizada — EVITAR siempre, score ya viene capado a 40\n"
    "- yield diario >0.25%: probable spike, aunque el APR se vea jugoso\n"
    "- yield diario 0.15-0.25%: zona de sospecha, exige con>80 y z<1.0\n"
    "- con<60: tasa inconsistente, puede cambiar de signo\n"
    "- fd>0.5: los fees se comen mas del 50% de la ganancia\n"
    "- grade D: estabilidad muy baja\n"
    "- sc<=45: casi seguro cae en un hard cap anti-spike — no comprar salvo contexto excepcional\n\n"
    "FORTALEZAS CLAVE (las que realmente predicen retornos):\n"
    "- con>=85 + grade A/B: combinacion ganadora confirmada por backtest\n"
    "- yield diario 0.03-0.10% (sweet spot): rendimiento sostenible, no spike\n"
    "- z<1.0 + mom en (flat, stable): tasa en rango normal, sin sobreextension\n"
    "- ehd>=5: persistencia esperada confirmada\n"
    "- fd<0.2: fees bajos, alta eficiencia\n\n"
    "NOTA sobre yield: prioriza el sweet spot 0.03-0.10% sobre yields extremos. "
    "Un yield de 0.08% diario con con=90 es MEJOR apuesta que 0.30% diario con con=65.\n\n"
    "En 'analysis' DEBES incluir estos 3 elementos en 40-60 palabras:\n"
    "1. SITUACION: que esta pasando con esta oportunidad (tasa, z-score, consistencia)\n"
    "2. RAZON: por que recomiendas esa signal (datos concretos, jerarquizando con y grade)\n"
    "3. ACCION: que debe hacer el usuario y que vigilar\n\n"
    "Ejemplo COMPRAR: 'Consistencia 89% con grade A y z-score 0.6, yield diario 0.07% en sweet spot. "
    "Momentum estable (no acelerando), fees recuperables en 5h. Entrar, vigilar si mom pasa a accelerating.'\n"
    "Ejemplo EVITAR (accel): 'Momentum acelerando con z-score 1.3 y yield 0.22%: patron de tip de spike. "
    "Backtest muestra que estas condiciones predicen reversion severa. Esperar a que momentum se aplane.'\n"
    "Ejemplo EVITAR (z alto): 'Z-score 2.1, tasa muy por encima de su media historica. "
    "Score capado por hard cap anti-spike. Reversion casi garantizada, no entrar.'"
)


def _get_groq_key(config) -> str:
    """Pick a random Groq API key from the configured keys."""
    keys = [
        k for k in (
            getattr(config, "GROQ_API_KEY_1", ""),
            getattr(config, "GROQ_API_KEY_2", ""),
            getattr(config, "GROQ_API_KEY_3", ""),
        )
        if k
    ]
    if not keys:
        return ""
    return random.choice(keys)


def _slim_opp(opp: dict) -> dict:
    """Extract relevant fields for AI analysis — rich enough for good analysis."""
    ind = opp.get("indicators", {})
    is_cross = opp.get("mode") == "cross_exchange"
    hist = opp.get("history", {})

    slim = {
        "id": opp.get("_id", ""),
        "sym": opp.get("symbol", ""),
        "mode": "cross" if is_cross else "sp",
        "sc": opp.get("score", 0),
        "apr": round(opp.get("apr", 0)),
        "beh": round(opp.get("break_even_hours", 0), 1),
        "d1k": round(opp.get("daily_income_per_1000", 0), 2),
        "n3d": round(opp.get("net_3d_revenue_per_1000", 0), 2),
        "vol": round((opp.get("volume_24h", 0) or 0) / 1e6, 1),
        "grade": opp.get("stability_grade", "D"),
        "ehd": opp.get("estimated_hold_days", 0),
    }

    if is_cross:
        slim["diff"] = round((opp.get("rate_differential", 0) or 0) * 100, 4)
    else:
        slim["fr"] = round((opp.get("funding_rate", 0) or 0) * 100, 4)

    # Consistency & fee drag from history
    if hist:
        con = hist.get("pct", hist.get("favorable_pct", 0))
        if con:
            slim["con"] = round(con)
        fd = hist.get("fee_drag", 0)
        if fd:
            slim["fd"] = round(fd, 2)

    # Indicadores aplanados
    if ind:
        slim["mom"] = ind.get("momentum_signal", "flat")
        slim["z"] = round(ind.get("z_score", 0), 1)
        slim["pct"] = round(ind.get("percentile", 0))
        slim["reg"] = ind.get("regime", "normal")
        if ind.get("is_spike_incoming"):
            slim["spike"] = True
        if ind.get("is_spike_ending"):
            slim["rev"] = True

    return slim


def _build_messages(opps: list) -> list:
    """Build system + user messages for Groq API."""
    slim_data = [_slim_opp(o) for o in opps]
    user_content = (
        f"Analiza estas {len(slim_data)} oportunidades de arbitraje de funding rates "
        f"(ordenadas por score de mayor a menor). Para cada una, evalua si vale la pena "
        f"entrar, considerando riesgo vs retorno, sostenibilidad de la tasa, y eficiencia de fees. "
        f"Da una explicacion clara y accionable:\n"
        + json.dumps(slim_data, separators=(",", ":"), ensure_ascii=False)
    )
    return [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": user_content},
    ]


def _parse_ai_response(text: str, valid_signals: tuple, default_signal: str) -> dict:
    """Parse Groq JSON response into {id: {signal, confidence, analysis}} map."""
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        m = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.DOTALL)
        if m:
            try:
                data = json.loads(m.group(1))
            except json.JSONDecodeError:
                return {}
        else:
            return {}

    analyses = data.get("analyses", [])
    if not isinstance(analyses, list):
        return {}

    result = {}
    for a in analyses:
        aid = str(a.get("id", ""))
        if not aid:
            continue
        signal = a.get("signal", default_signal).upper()
        if signal not in valid_signals:
            signal = default_signal
        entry = {
            "signal": signal,
            "confidence": max(1, min(10, int(a.get("confidence", 5)))),
            "analysis": str(a.get("analysis", ""))[:500],
        }
        action_plan = a.get("action_plan", "")
        if action_plan:
            entry["action_plan"] = str(action_plan)[:300]
        result[aid] = entry

    return result


def analyze_top_opportunities(opportunities: list, config, top_n: int = MAX_OPPS) -> list:
    """Analyze top N opportunities with Groq AI. Returns opportunities with ai_analysis field.

    Gracefully degrades: if no API key, Groq fails, or parsing fails,
    returns opportunities unchanged without ai_analysis field.
    """
    api_key = _get_groq_key(config)
    if not api_key:
        return opportunities

    # Top N by score (already sorted)
    top_opps = opportunities[:top_n]
    if not top_opps:
        return opportunities

    try:
        from groq import Groq

        client = Groq(api_key=api_key)
        messages = _build_messages(top_opps)

        resp = client.chat.completions.create(
            model=GROQ_MODEL,
            messages=messages,
            response_format={"type": "json_object"},
            temperature=0.3,
            max_tokens=3000,
            timeout=TIMEOUT,
        )

        raw = resp.choices[0].message.content or ""
        analysis_map = _parse_ai_response(
            raw, ("COMPRAR", "MANTENER", "EVITAR"), "MANTENER"
        )

        if analysis_map:
            for opp in opportunities:
                ai = analysis_map.get(opp.get("_id", ""))
                if ai:
                    opp["ai_analysis"] = ai

            log.info(f"AI analysis: {len(analysis_map)}/{len(top_opps)} opportunities analyzed")
        else:
            log.warning("AI analysis: empty response from Groq")

    except Exception as e:
        log.warning(f"AI analysis failed: {e}")

    return opportunities


# ── Position Analysis ─────────────────────────────────────────

POSITION_SYSTEM_PROMPT = (
    "Eres analista experto en arbitraje de funding rates. Tu rol es dar al usuario "
    "una RUTA DE DECISION CLARA sobre cada posicion: mantener, vigilar o cerrar, "
    "y si hay una alternativa mejor, explicar el trade-off concreto.\n\n"
    "CONTEXTO DEL SCORING (v10.3 — recalibrado con backtest 90 dias):\n"
    "- Los scores ahora son mas conservadores: hard caps anti-spike limitan a 40/45/50 "
    "cuando detectan z>2, rachas inmaduras en percentiles altos, o current_rate >1.5x media.\n"
    "- Por eso sw.alt_sc>=70 es señal MUY confiable (la alternativa paso todos los filtros).\n"
    "- sw.alt_sc<=50 casi seguro cae en un hard cap — no justifica switch aunque sw.val>0.\n"
    "- Consistencia del candidato es el predictor #1 (Spearman 0.55 en backtest).\n\n"
    "Responde SOLO JSON valido:\n"
    '{"analyses":[{"id":"id","signal":"MANTENER|CERRAR|VIGILAR",'
    '"confidence":1-10,"analysis":"texto 50-80 palabras",'
    '"action_plan":"1-2 pasos concretos que el usuario debe seguir"}]}\n\n'
    "CAMPOS que recibiras:\n"
    "efr=FR entrada%, cfr=FR actual%, ar=FR promedio%, apr=APR actual%, "
    "h=horas abierta, pc=pagos recibidos, rev=FR revertido, "
    "lr=ultimas 5 tasas%, net=ganancia neta, fees=fees estimados, "
    "cap=capital, exp=exposicion, lev=apalancamiento, ih=intervalo horas, "
    "fee_recovery_pct=% de fees recuperados, trend=tendencia(up/down/stable/unknown)\n\n"
    "CAMPOS SWITCHING (sw) — CRITICO para decision de cambio:\n"
    "- sw.val: beneficio neto del switch (descontando TODOS los fees de salir+entrar)\n"
    "- sw.beh: horas para recuperar los fees del cambio\n"
    "- sw.rec: recomendacion cuantitativa (SWITCH/CONSIDER/HOLD)\n"
    "- sw.alt: simbolo alternativa, sw.alt_ex: exchange alternativa\n"
    "- sw.apr: APR de la alternativa\n"
    "- sw.alt_sc: score de la alternativa\n"
    "- sw.sw_cost: costo total del switch en $\n"
    "- sw.cur_proj: proyeccion ganancia actual 72h, sw.new_proj: proyeccion alternativa\n\n"
    "REGLAS de decision:\n"
    "CERRAR:\n"
    "- rev=true (FR cambio de signo) — SIEMPRE cerrar\n"
    "- cfr~0 o apr<0 — posicion no genera\n"
    "- cfr<efr/3 y h>48 — deterioro severo confirmado\n"
    "- sw.rec=SWITCH y sw.val>0 y sw.beh<24 y sw.alt_sc>=65 — alternativa solida y mejor\n"
    "- fee_recovery_pct<30 y h>72 — no recupera fees, capital atrapado\n\n"
    "VIGILAR:\n"
    "- cfr<efr/2 — FR ha caido significativamente\n"
    "- trend=down — tendencia descendente en pagos recientes\n"
    "- sw.rec=CONSIDER y sw.alt_sc>=60 — alternativa potencialmente mejor\n"
    "- fee_recovery_pct<60 y h>48 — recuperacion lenta de fees\n"
    "- h>144 y cfr<ar — posicion vieja con rendimiento bajo promedio\n\n"
    "MANTENER:\n"
    "- cfr estable/subiendo, apr>0, sin reversion\n"
    "- fee_recovery_pct>=100 (fees ya recuperados)\n"
    "- trend=up o stable con cfr>=ar\n"
    "- sw.rec=HOLD o no hay sw — sin alternativa mejor\n"
    "- sw.alt_sc<=50 aunque sw.rec=SWITCH — el candidato casi seguro cayo en un hard cap\n\n"
    "ANALISIS COMPARATIVO (cuando sw presente):\n"
    "El usuario necesita saber CON NUMEROS si vale la pena cambiar:\n"
    "1. Cuanto gana quedandose (cur_proj en 72h)\n"
    "2. Cuanto ganaria cambiando (new_proj menos sw_cost)\n"
    "3. En cuantas horas recupera el costo del cambio (beh)\n"
    "4. Riesgo: alt_sc CON v10.3 — >=70 muy confiable, 60-69 aceptable, <=50 sospechoso\n"
    "Si la diferencia es marginal (<$0.50 o <10% mejora): recomendar MANTENER\n"
    "Si alt_sc<=50: recomendar MANTENER (el candidato probablemente choca con hard cap anti-spike)\n"
    "Si la alternativa es claramente superior (>25% mejora, beh<24h, alt_sc>=65): recomendar CERRAR\n\n"
    "CONTEXTO TEMPORAL:\n"
    "- h<24: posicion nueva, dar tiempo salvo reversion clara\n"
    "- h 24-72: evaluar cfr vs efr\n"
    "- h 72-144: madura, cfr debe estar cerca de ar\n"
    "- h>144: escrutinio alto, exigir cfr>=ar\n"
    "- h>288: muy vieja, considerar CERRAR salvo apr excelente\n\n"
    "En 'analysis' incluir estos 3 elementos en 50-80 palabras:\n"
    "1. DIAGNOSTICO: salud de la posicion (FR, tendencia, fees recuperados)\n"
    "2. COMPARACION: si hay alternativa, comparar numeros concretos\n"
    "3. VEREDICTO: conclusion clara con razon principal\n\n"
    "En 'action_plan' dar 1-2 pasos CONCRETOS y accionables:\n"
    "Ejemplo: '1. Mantener hasta proximo pago. 2. Si FR baja de 0.005%, cerrar y entrar en ETHUSDT (Binance).'\n"
    "Ejemplo: '1. Cerrar posicion ahora. 2. Abrir SOLUSDT en Bybit (APR 45%, score 78).'\n"
    "Ejemplo: '1. Vigilar proximas 8h. 2. Si FR no recupera 0.01%, cerrar.'\n\n"
    "Ejemplo CERRAR con switch: 'Posicion debilitada: FR cayo a 0.003% (entrada 0.02%), "
    "tendencia bajista, solo 25% fees recuperados en 96h. Alternativa SOLUSDT ofrece APR 52% vs 8% actual, "
    "con costo de switch de $1.20 que se recupera en 6h. Cambiar es claramente mejor.'\n"
    "Ejemplo MANTENER: 'Posicion sana: FR estable en 0.015% (promedio 0.012%), APR 38%, "
    "fees 100% recuperados con $2.30 neto. Tendencia estable. No hay alternativa que justifique "
    "el costo de cambio ($2.40). Mantener.'"
)


def _slim_position(pos: dict) -> dict:
    """Position data for AI — rich enough for quality analysis."""
    payments = pos.get("payments") or []
    # Last 5 rates for better trend detection
    recent = [round(p["rate"] * 100, 4) for p in payments[-5:]] if payments else []

    # Prefer user-entered real fees; otherwise sum entry estimate + exit estimate.
    from portfolio.manager import position_fees as _pf
    _e, _x, est_fees, _is_real = _pf(pos)

    slim = {
        "id": str(pos.get("id", "")),
        "sym": pos.get("symbol", ""),
        "mode": "cross" if pos.get("mode") == "cross_exchange" else "sp",
        "cap": round(pos.get("capital_used", 0) or 0),
        "exp": round(pos.get("exposure", 0) or 0),
        "lev": pos.get("leverage", 1) or 1,
        "efr": round((pos.get("entry_fr", 0) or 0) * 100, 4),
        "cfr": round((pos.get("current_fr", 0) or 0) * 100, 4),
        "ar": round((pos.get("avg_rate", 0) or 0) * 100, 4),
        "pc": pos.get("payment_count", 0) or 0,
        "apr": round(pos.get("current_apr", 0) or 0, 1),
        "net": round(pos.get("net_earned", 0) or 0, 2),
        "fees": round(est_fees, 2),
        "h": round(pos.get("elapsed_h", 0) or 0, 1),
        "ih": pos.get("ih", 8) or 8,
        "rev": bool(pos.get("fr_reversed")),
        "lr": recent,
        "fee_recovery_pct": round(min(100, (pos.get("earned_real", 0) / est_fees * 100) if est_fees > 0 else 100), 1),
    }

    # Trend analysis from recent payments
    if len(recent) >= 2:
        diffs = [recent[i] - recent[i - 1] for i in range(1, len(recent))]
        avg_diff = sum(diffs) / len(diffs)
        if avg_diff > 0.0005:
            slim["trend"] = "up"
        elif avg_diff < -0.0005:
            slim["trend"] = "down"
        else:
            slim["trend"] = "stable"
    else:
        slim["trend"] = "unknown"

    # Include switch analysis context if available
    sa = pos.get("switch_analysis")
    if sa and sa.get("recommendation") != "HOLD":
        best = sa.get("best_switch")
        if best:
            slim["sw"] = {
                "val": round(best.get("adjusted_switch_value", 0), 2),
                "beh": round(best.get("break_even_h", 999), 1),
                "rec": sa["recommendation"],
                "alt": best.get("symbol", ""),
                "alt_ex": best.get("exchange", ""),
                "apr": round(best.get("apr", 0), 1),
                "alt_sc": best.get("score", 0),
                "sw_cost": round(best.get("switch_cost", 0), 2),
                "cur_proj": round(sa.get("current_projected", 0), 2),
                "new_proj": round(best.get("projected_gain_new", 0), 2),
            }

    return slim


def analyze_positions(positions: list, config) -> dict:
    """Analyze active positions with Groq AI. Returns {pos_id: {signal, confidence, analysis}}.

    Gracefully degrades: returns empty dict on any failure.
    """
    api_key = _get_groq_key(config)
    if not api_key or not positions:
        return {}

    try:
        from groq import Groq

        slim_data = [_slim_position(p) for p in positions]
        user_content = (
            f"Evalua estas {len(slim_data)} posiciones abiertas de arbitraje de funding rates. "
            f"Para cada una analiza: rendimiento vs expectativa, tendencia del FR, "
            f"si los fees se han recuperado, y si el tiempo abierto justifica mantenerla. "
            f"Da una recomendacion clara con razonamiento:\n"
            + json.dumps(slim_data, separators=(",", ":"), ensure_ascii=False)
        )
        messages = [
            {"role": "system", "content": POSITION_SYSTEM_PROMPT},
            {"role": "user", "content": user_content},
        ]

        client = Groq(api_key=api_key)
        resp = client.chat.completions.create(
            model=GROQ_MODEL,
            messages=messages,
            response_format={"type": "json_object"},
            temperature=0.3,
            max_tokens=3000,
            timeout=TIMEOUT,
        )

        raw = resp.choices[0].message.content or ""
        result = _parse_ai_response(
            raw, ("MANTENER", "CERRAR", "VIGILAR"), "VIGILAR"
        )
        log.info(f"Position AI: {len(result)}/{len(positions)} analyzed")
        return result

    except Exception as e:
        log.warning(f"Position AI failed: {e}")
        return {}


