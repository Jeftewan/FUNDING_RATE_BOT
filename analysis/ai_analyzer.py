"""AI-powered opportunity analysis via Groq (Llama 3.3 70B)."""

import json
import logging
import re

log = logging.getLogger(__name__)

GROQ_MODEL = "llama-3.3-70b-versatile"
MAX_OPPS = 10
TIMEOUT = 25

SYSTEM_PROMPT = (
    "Eres analista experto en arbitraje de funding rates. Tu trabajo es evaluar "
    "oportunidades y dar una explicacion clara que ayude al usuario a decidir.\n\n"
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
    "COMPRAR: sc>60 + mom!=negative + beh<10h + vol>5M + z<1.5 + con>70\n"
    "EVITAR: z>2.0 | mom=negative | rev=true | vol<2M | beh>15h | con<50 | grade=D\n"
    "MANTENER: no cumple COMPRAR ni EVITAR\n\n"
    "RIESGOS CLAVE:\n"
    "- z>2.5: tasa MUY alejada de la media, reversion inminente — EVITAR siempre\n"
    "- z>2.0: riesgo alto de reversion — solo MANTENER si todo lo demas es fuerte\n"
    "- con<60: la tasa ha sido inconsistente, puede cambiar de signo\n"
    "- fd>0.5: los fees se comen mas del 50% de la ganancia\n"
    "- grade D: estabilidad muy baja, alto riesgo\n\n"
    "FORTALEZAS CLAVE:\n"
    "- con>85 + grade A/B: tasa historicamente muy confiable\n"
    "- ehd>5: se espera que la tasa se mantenga varios dias mas\n"
    "- mom=accelerating + z<1.5: momentum fuerte sin estar sobreextendida\n"
    "- fd<0.2: fees bajos, alta eficiencia\n\n"
    "En 'analysis' DEBES incluir estos 3 elementos en 40-60 palabras:\n"
    "1. SITUACION: que esta pasando con esta oportunidad (tasa, tendencia, riesgo)\n"
    "2. RAZON: por que recomiendas esa signal (datos concretos)\n"
    "3. ACCION: que debe hacer el usuario y que vigilar\n\n"
    "Ejemplo COMPRAR: 'Tasa estable en percentil 75 con momentum acelerando y z-score bajo (0.8). "
    "Fees se recuperan en 4h y consistencia del 88%. Entrar ahora, colocar SL si FR cae bajo 0.01%.'\n"
    "Ejemplo EVITAR: 'Tasa en z-score 2.3, muy por encima de su media historica. "
    "Momentum desacelerando y consistencia solo 55%. Alto riesgo de reversion, esperar correccion.'"
)


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
        result[aid] = {
            "signal": signal,
            "confidence": max(1, min(10, int(a.get("confidence", 5)))),
            "analysis": str(a.get("analysis", ""))[:500],
        }

    return result


def analyze_top_opportunities(opportunities: list, config, top_n: int = MAX_OPPS) -> list:
    """Analyze top N opportunities with Groq AI. Returns opportunities with ai_analysis field.

    Gracefully degrades: if no API key, Groq fails, or parsing fails,
    returns opportunities unchanged without ai_analysis field.
    """
    api_key = getattr(config, "GROQ_API_KEY", "")
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
    "Eres analista experto en arbitraje de funding rates. Evaluas posiciones abiertas "
    "y das recomendaciones claras con explicacion detallada.\n\n"
    "Responde SOLO JSON valido:\n"
    '{"analyses":[{"id":"id","signal":"MANTENER|CERRAR|VIGILAR",'
    '"confidence":1-10,"analysis":"texto explicativo 40-60 palabras"}]}\n\n'
    "CAMPOS que recibiras:\n"
    "efr=FR de entrada%, cfr=FR actual%, ar=FR promedio historico de la posicion%, "
    "apr=APR actual%, h=horas abierta, pc=pagos recibidos, "
    "rev=FR revertido(cambio de signo), lr=ultimas 3 tasas%, "
    "net=ganancia neta(despues de fees), cap=capital, exp=exposicion, lev=apalancamiento, "
    "fees=fees estimados(entry+exit), ih=intervalo de pago en horas\n\n"
    "REGLAS de decision:\n"
    "CERRAR: rev=true | cfr~0 | apr<0 | (cfr<efr/3 y h>48) | (h>144 y cfr<ar/2) | net muy negativo\n"
    "VIGILAR: cfr<efr/2 | apr cayendo | lr tendencia baja | (cfr<ar y h>72) | (h>144 y cfr<ar)\n"
    "MANTENER: cfr estable o subiendo, apr>0, sin reversion, cfr>=ar\n\n"
    "CONTEXTO TEMPORAL — muy importante:\n"
    "- pc<3: pocos datos, ar no confiable, basarse en cfr vs efr y tendencia lr\n"
    "- h<24 (1d): posicion nueva, dar tiempo salvo reversion clara\n"
    "- h 24-72: evaluar si cfr se mantiene vs efr\n"
    "- h 72-144: posicion madura, cfr debe estar cerca de ar para MANTENER\n"
    "- h>144 (6d): escrutinio alto, exigir cfr>=ar para MANTENER\n"
    "- h>288 (12d): posicion muy vieja, considerar CERRAR salvo apr excelente\n\n"
    "CONTEXTO FR PROMEDIO (ar) — dato clave:\n"
    "- cfr >= ar: posicion sana, FR actual igual o mejor que su promedio\n"
    "- cfr < ar*0.5: deterioro claro, la tasa esta cayendo significativamente\n"
    "- ar < efr/2: la posicion nunca rindio lo esperado, debilidad estructural\n"
    "- ar >= efr: la posicion ha rendido igual o mejor que al entrar\n\n"
    "CONTEXTO FEES Y RENTABILIDAD:\n"
    "- net > 0: fees ya recuperados, posicion en ganancia\n"
    "- net < 0 y h>48: no ha recuperado fees en 2 dias, mal signo\n"
    "- Con apalancamiento alto (lev>3): mas sensible a movimientos de precio\n\n"
    "En 'analysis' DEBES incluir estos 3 elementos en 40-60 palabras:\n"
    "1. ESTADO: como va la posicion (rendimiento, tendencia del FR, tiempo)\n"
    "2. RAZON: por que recomiendas esa signal (datos concretos del analisis)\n"
    "3. ACCION: que debe hacer el usuario ahora (mantener/cerrar/ajustar SL-TP)\n\n"
    "Ejemplo MANTENER: 'Posicion sana con 72h abierta. FR actual (0.015%) por encima del promedio "
    "(0.012%) y APR de 38%. Fees recuperados con $2.30 neto. Mantener, ajustar SL si FR baja de 0.008%.'\n"
    "Ejemplo CERRAR: 'FR cayo de 0.02% a 0.003% en ultimos 3 pagos, muy por debajo del promedio (0.015%). "
    "Con 168h abierta el deterioro es claro. Cerrar y reubicar capital en mejor oportunidad.'\n"
    "Ejemplo VIGILAR: 'FR actual (0.01%) esta por debajo del promedio (0.018%) tras 96h. "
    "Tendencia descendente en lr. Vigilar proximo pago, cerrar si cae bajo 0.005%.'"
)


def _slim_position(pos: dict) -> dict:
    """Position data for AI — rich enough for quality analysis."""
    payments = pos.get("payments") or []
    # Last 5 rates for better trend detection
    recent = [round(p["rate"] * 100, 4) for p in payments[-5:]] if payments else []

    entry_fees = pos.get("entry_fees", 0) or 0
    est_fees = entry_fees * 2  # entry + exit estimate

    return {
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
    }


def analyze_positions(positions: list, config) -> dict:
    """Analyze active positions with Groq AI. Returns {pos_id: {signal, confidence, analysis}}.

    Gracefully degrades: returns empty dict on any failure.
    """
    api_key = getattr(config, "GROQ_API_KEY", "")
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
            max_tokens=2000,
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


