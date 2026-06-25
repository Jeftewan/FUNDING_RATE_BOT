"""AI-powered opportunity analysis via Gemini 3.1 Flash-Lite."""

import json
import logging
import random
import re

log = logging.getLogger(__name__)

GEMINI_MODEL = "gemini-3.1-flash-lite"
MAX_OPPS = 5
TIMEOUT = 25

SYSTEM_PROMPT = (
    "Eres analista experto en arbitraje de funding rates. Tu trabajo es evaluar "
    "oportunidades y dar una explicacion clara que ayude al usuario a decidir.\n\n"
    "CONTEXTO DEL RANKING (modelo predictivo ML):\n"
    "Un modelo de machine learning predice el NET APR (retorno anual neto de fees, en %) "
    "de cada oportunidad y las oportunidades te llegan YA RANKEADAS de mayor a menor por "
    "ese Net APR predicho. El numero clave que evaluas es 'napr' (Net APR predicho), NO un "
    "score 0-100.\n"
    "El modelo ya internaliza consistencia, estabilidad, yield, fee drag, z-score, momentum "
    "y percentil como variables de entrada. Es decir, el napr predicho YA descuenta esos "
    "riesgos. Tu rol es dar contexto humano y accionable, NO re-penalizar lo que el modelo "
    "ya considero. No bajes una señal solo porque ves un riesgo menor que el modelo ya peso.\n"
    "IMPORTANTE — campo 'mdl': napr es la prediccion NETA del modelo SOLO si mdl=1. Si mdl=0 "
    "el modelo no predijo esta oportunidad y napr es un APR BRUTO estimado (no descuenta fees ni "
    "riesgo, y el lote se ordeno por heuristico, no por napr): trata ese napr con mas cautela, no "
    "asumas que el riesgo ya esta descontado y apoyate mas en con, fd, z y mom.\n\n"
    "Responde SOLO JSON valido:\n"
    '{"analyses":[{"id":"_id","signal":"COMPRAR|MANTENER|EVITAR",'
    '"confidence":1-10,"analysis":"texto explicativo 40-60 palabras"}]}\n\n'
    "CAMPOS que recibiras (SIEMPRE presentes, sin excepciones):\n"
    "- sym: simbolo del par (ej BTC, ETH)\n"
    "- mode: 'sp'=spot-perp, 'cross'=cross-exchange\n"
    "- napr: Net APR predicho por el modelo ML en % anual neto de fees — METRICA PRINCIPAL "
    "(solo fiable como neto si mdl=1)\n"
    "- mdl: 1=napr viene del modelo ML (neto, descuenta riesgo); 0=modelo no predijo, napr es APR bruto\n"
    "- apr: retorno anual bruto estimado en %\n"
    "- beh: horas para recuperar fees (break-even)\n"
    "- d1k: ingreso diario USD por cada $1000 invertidos\n"
    "- n3d: ingreso neto USD en 3 dias por cada $1000 invertidos\n"
    "- vol: volumen 24h en millones USD\n"
    "- fr: funding rate actual en % (solo mode sp)\n"
    "- diff: diferencial de funding rate en % (solo mode cross)\n"
    "- grade: grado segun Net APR predicho (A=muy alto, B=alto, C=moderado, D=bajo)\n"
    "- ehd: dias estimados que la tasa se mantendra favorable\n"
    "- con: consistencia en % — porcentaje de periodos historicos que fueron favorables (0-100)\n"
    "- fd: fee drag — ratio fees/ganancia bruta (0.0-1.0, menor=mejor, 0.2=20% se va en fees)\n"
    "- mom: momentum (contexto de riesgo) — 'flat'=estable, 'accelerating'=acelerando, "
    "'decelerating'=desacelerando, 'negative'=negativo\n"
    "- z: z-score (contexto de riesgo) — desviaciones estandar de la tasa actual vs su media "
    "historica (0=normal, >1.5=sobreextendido, >2.0=critico)\n\n"
    "REGLAS de decision (sobre el napr ya rankeado por el modelo):\n"
    "COMPRAR: napr alto (entre los mejores del lote) + con>=60 + sin riesgo claro e inminente\n"
    "EVITAR: SOLO ante riesgo fuerte y concreto: z>=2.0 | (mom=accelerating AND z>1.0) | "
    "con<40 | beh>20h | napr<=0\n"
    "MANTENER: todo lo demas — señales mixtas, merecen vigilancia pero no descarte\n\n"
    "NOTA CRITICA: como el lote llega rankeado por el modelo, las primeras oportunidades ya "
    "fueron validadas como las de mayor retorno neto esperado. En estos casos la señal por "
    "defecto es COMPRAR; solo emite EVITAR si hay una razon FUERTE y CONCRETA (z>=2.0, "
    "momentum acelerando con z alto, consistencia muy baja, break-even excesivo).\n\n"
    "FORTALEZAS que refuerzan COMPRAR:\n"
    "- napr alto con con>=70: retorno neto esperado solido y consistente\n"
    "- z<1.0 + mom flat: tasa en rango normal, sin sobreextension\n"
    "- ehd>=5: persistencia esperada confirmada\n"
    "- fd<0.2: fees bajos, alta eficiencia\n\n"
    "RIESGOS (solo estos justifican EVITAR):\n"
    "- z>=2.0: tasa sobreextendida, reversion probable\n"
    "- mom=accelerating + z>1.0: pico de spike, reversion inminente\n"
    "- con<40: tasa demasiado impredecible\n"
    "- beh>20h: fees excesivos para el retorno\n"
    "- napr<=0: el modelo no espera retorno neto positivo\n\n"
    "En 'analysis' DEBES incluir estos 3 elementos en 40-60 palabras:\n"
    "1. SITUACION: que esta pasando con esta oportunidad\n"
    "2. RAZON: por que recomiendas esa signal (datos concretos)\n"
    "3. ACCION: que debe hacer el usuario y que vigilar\n\n"
    "Ejemplo COMPRAR (napr=48): 'Net APR predicho 48% con consistencia 89% grade A y z-score 0.6. "
    "El modelo la rankea entre las mejores; momentum estable y fees recuperables en 5h. Entrar con "
    "confianza, vigilar si z sube de 1.0.'\n"
    "Ejemplo MANTENER (napr=15): 'Net APR predicho 15% con consistencia 63% grade C. Retorno aceptable "
    "pero z-score 1.2 indica sobreextension moderada. Esperar a que z baje de 1.0 o entrar con tamaño reducido.'\n"
    "Ejemplo EVITAR (z alto): 'Z-score 2.3, tasa sobreextendida y reversion probable; el modelo aun la rankea "
    "pero el riesgo es concreto. No entrar hasta que z baje de 1.5.'"
)


def _get_gemini_key(config) -> str:
    """Pick a random Gemini API key from the configured keys."""
    keys = [
        k for k in (
            getattr(config, "GEMINI_API_KEY_1", ""),
            getattr(config, "GEMINI_API_KEY_2", ""),
            getattr(config, "GEMINI_API_KEY_3", ""),
        )
        if k
    ]
    if not keys:
        return ""
    return random.choice(keys)


def _slim_opp(opp: dict) -> dict:
    """Extract relevant fields for AI analysis.

    Every key is ALWAYS present so the AI never has to guess about missing
    data.  The key names exactly match what SYSTEM_PROMPT documents.
    """
    ind = opp.get("indicators", {}) or {}
    is_cross = opp.get("mode") == "cross_exchange"
    hist = opp.get("history", {}) or {}

    # Net APR predicho por el modelo ML — métrica principal. Fallback al APR
    # bruto estimado cuando el modelo no predijo (model_prediction None). El flag
    # mdl le dice al LLM si napr es la predicción neta del modelo (1) o un APR
    # bruto sustituto (0), para que no lo trate como neto-descontado-de-riesgo.
    model_pred = opp.get("model_prediction")
    is_model = model_pred is not None
    napr = model_pred if is_model else opp.get("apr", 0)

    slim = {
        "id": opp.get("_id", ""),
        "sym": opp.get("symbol", ""),
        "mode": "cross" if is_cross else "sp",
        "napr": round(napr, 1),
        "mdl": 1 if is_model else 0,
        "apr": round(opp.get("apr", 0)),
        "beh": round(opp.get("break_even_hours", 0), 1),
        "d1k": round(opp.get("daily_income_per_1000", 0), 2),
        "n3d": round(opp.get("net_3d_revenue_per_1000", 0), 2),
        "vol": round((opp.get("volume_24h", 0) or 0) / 1e6, 1),
        "grade": opp.get("stability_grade", "D"),
        "ehd": opp.get("estimated_hold_days", 0),
        # Always include consistency and fee drag (0 is a valid value)
        "con": round(hist.get("pct", hist.get("favorable_pct", 0)) or 0),
        "fd": round(hist.get("fee_drag", 0) or 0, 2),
    }

    if is_cross:
        slim["diff"] = round((opp.get("rate_differential", 0) or 0) * 100, 4)
    else:
        slim["fr"] = round((opp.get("funding_rate", 0) or 0) * 100, 4)

    # Indicadores de riesgo (contexto para la narrativa; el modelo ya los ingiere)
    slim["mom"] = ind.get("momentum_signal", "flat") or "flat"
    slim["z"] = round(ind.get("z_score", 0) or 0, 1)

    return slim


def _build_messages(opps: list) -> tuple:
    """Build (system_prompt, user_content) for the Gemini API."""
    slim_data = [_slim_opp(o) for o in opps]
    user_content = (
        f"Analiza estas {len(slim_data)} oportunidades de arbitraje de funding rates "
        f"(ordenadas por Net APR predicho de mayor a menor). Para cada una, evalua si vale la pena "
        f"entrar, considerando riesgo vs retorno, sostenibilidad de la tasa, y eficiencia de fees. "
        f"Da una explicacion clara y accionable:\n"
        + json.dumps(slim_data, separators=(",", ":"), ensure_ascii=False)
    )
    return SYSTEM_PROMPT, user_content


def _parse_ai_response(text: str, valid_signals: tuple, default_signal: str) -> dict:
    """Parse the LLM JSON response into {id: {signal, confidence, analysis}} map."""
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
    """Analyze top N opportunities with Gemini AI. Returns opportunities with ai_analysis field.

    Gracefully degrades: if no API key, Gemini fails, or parsing fails,
    returns opportunities unchanged without ai_analysis field.
    """
    api_key = _get_gemini_key(config)
    if not api_key:
        return opportunities

    # Top N por Net APR predicho por el modelo ML (model_prediction). Fallback a
    # `score` cuando el modelo no predijo. Se excluyen las que el modelo predice
    # con retorno neto <= 0 (no vale la pena analizarlas).
    def _napr_key(o):
        mp = o.get("model_prediction")
        return mp if mp is not None else o.get("score", 0)

    ranked = sorted(opportunities, key=_napr_key, reverse=True)
    ranked = [
        o for o in ranked
        if not (o.get("model_prediction") is not None and o.get("model_prediction") <= 0)
    ]
    top_opps = ranked[:top_n]
    if not top_opps:
        return opportunities

    try:
        from google import genai
        from google.genai import types

        client = genai.Client(
            api_key=api_key,
            http_options=types.HttpOptions(timeout=TIMEOUT * 1000),
        )
        system_prompt, user_content = _build_messages(top_opps)

        resp = client.models.generate_content(
            model=GEMINI_MODEL,
            contents=user_content,
            config=types.GenerateContentConfig(
                system_instruction=system_prompt,
                temperature=0.3,
                response_mime_type="application/json",
            ),
        )

        raw = resp.text or ""
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
            log.warning("AI analysis: empty response from Gemini")

    except Exception as e:
        log.warning(f"AI analysis failed: {e}")

    return opportunities


# ── Position Analysis ─────────────────────────────────────────

POSITION_SYSTEM_PROMPT = (
    "Eres analista experto en arbitraje de funding rates. Tu rol es dar al usuario "
    "una RUTA DE DECISION CLARA sobre cada posicion: mantener, vigilar o cerrar, "
    "y si hay una alternativa mejor, explicar el trade-off concreto.\n\n"
    "CONTEXTO DEL RANKING (modelo predictivo ML):\n"
    "- Un modelo ML predice el Net APR (retorno anual neto de fees) de cada candidato; "
    "es la métrica de calidad de las alternativas (sw.alt_napr), ya descuenta consistencia, "
    "estabilidad, yield, fees, z-score, momentum y percentil.\n"
    "- sw.alt_napr alto = alternativa de mayor retorno neto esperado; sw.alt_napr<=0 = el modelo "
    "no espera retorno neto positivo, no justifica switch.\n"
    "- Compara sw.alt_napr contra el Net APR del que ya tienes: el switch solo vale si la "
    "alternativa supera al actual por un margen claro Y cubre el costo del cambio.\n\n"
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
    "- sw.apr: APR bruto de la alternativa\n"
    "- sw.alt_napr: Net APR predicho de la alternativa (% anual neto, metrica de calidad)\n"
    "- sw.sw_cost: costo total del switch en $\n"
    "- sw.cur_proj: proyeccion ganancia actual 72h, sw.new_proj: proyeccion alternativa\n\n"
    "REGLAS de decision:\n"
    "CERRAR:\n"
    "- rev=true (FR cambio de signo) — SIEMPRE cerrar\n"
    "- cfr~0 o apr<0 — posicion no genera\n"
    "- cfr<efr/3 y h>48 — deterioro severo confirmado\n"
    "- sw.rec=SWITCH y sw.val>0 y sw.beh<24 y sw.alt_napr supera al Net APR actual — alternativa solida y mejor\n"
    "- fee_recovery_pct<30 y h>72 — no recupera fees, capital atrapado\n\n"
    "VIGILAR:\n"
    "- cfr<efr/2 — FR ha caido significativamente\n"
    "- trend=down — tendencia descendente en pagos recientes\n"
    "- sw.rec=CONSIDER y sw.alt_napr atractivo — alternativa potencialmente mejor\n"
    "- fee_recovery_pct<60 y h>48 — recuperacion lenta de fees\n"
    "- h>144 y cfr<ar — posicion vieja con rendimiento bajo promedio\n\n"
    "MANTENER:\n"
    "- cfr estable/subiendo, apr>0, sin reversion\n"
    "- fee_recovery_pct>=100 (fees ya recuperados)\n"
    "- trend=up o stable con cfr>=ar\n"
    "- sw.rec=HOLD o no hay sw — sin alternativa mejor\n"
    "- sw.alt_napr<=0 o no supera al Net APR actual aunque sw.rec=SWITCH — el cambio no agrega retorno neto\n\n"
    "ANALISIS COMPARATIVO (cuando sw presente):\n"
    "El usuario necesita saber CON NUMEROS si vale la pena cambiar:\n"
    "1. Cuanto gana quedandose (cur_proj en 72h)\n"
    "2. Cuanto ganaria cambiando (new_proj menos sw_cost)\n"
    "3. En cuantas horas recupera el costo del cambio (beh)\n"
    "4. Calidad: comparar sw.alt_napr (Net APR predicho de la alternativa) vs el Net APR del actual\n"
    "Si la diferencia es marginal (<$0.50 o <10% mejora, o alt_napr no supera al actual): recomendar MANTENER\n"
    "Si sw.alt_napr<=0: recomendar MANTENER (el modelo no espera retorno neto positivo del candidato)\n"
    "Si la alternativa es claramente superior (>25% mejora, beh<24h, alt_napr bien por encima del actual): recomendar CERRAR\n\n"
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
    "Ejemplo: '1. Cerrar posicion ahora. 2. Abrir SOLUSDT en Bybit (Net APR predicho 45%).'\n"
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
            # alt_napr: Net APR predicho del candidato (model_prediction). Fallback
            # al APR estimado si el modelo no predijo.
            alt_napr = best.get("net_apr")
            if alt_napr is None:
                alt_napr = best.get("apr", 0)
            slim["sw"] = {
                "val": round(best.get("adjusted_switch_value", 0), 2),
                "beh": round(best.get("break_even_h", 999), 1),
                "rec": sa["recommendation"],
                "alt": best.get("symbol", ""),
                "alt_ex": best.get("exchange", ""),
                "apr": round(best.get("apr", 0), 1),
                "alt_napr": round(alt_napr, 1),
                "sw_cost": round(best.get("switch_cost", 0), 2),
                "cur_proj": round(sa.get("current_projected", 0), 2),
                "new_proj": round(best.get("projected_gain_new", 0), 2),
            }

    return slim


def analyze_positions(positions: list, config) -> dict:
    """Analyze active positions with Gemini AI. Returns {pos_id: {signal, confidence, analysis}}.

    Gracefully degrades: returns empty dict on any failure.
    """
    api_key = _get_gemini_key(config)
    if not api_key or not positions:
        return {}

    try:
        from google import genai
        from google.genai import types

        slim_data = [_slim_position(p) for p in positions]
        user_content = (
            f"Evalua estas {len(slim_data)} posiciones abiertas de arbitraje de funding rates. "
            f"Para cada una analiza: rendimiento vs expectativa, tendencia del FR, "
            f"si los fees se han recuperado, y si el tiempo abierto justifica mantenerla. "
            f"Da una recomendacion clara con razonamiento:\n"
            + json.dumps(slim_data, separators=(",", ":"), ensure_ascii=False)
        )

        client = genai.Client(
            api_key=api_key,
            http_options=types.HttpOptions(timeout=TIMEOUT * 1000),
        )
        resp = client.models.generate_content(
            model=GEMINI_MODEL,
            contents=user_content,
            config=types.GenerateContentConfig(
                system_instruction=POSITION_SYSTEM_PROMPT,
                temperature=0.3,
                response_mime_type="application/json",
            ),
        )

        raw = resp.text or ""
        result = _parse_ai_response(
            raw, ("MANTENER", "CERRAR", "VIGILAR"), "VIGILAR"
        )
        log.info(f"Position AI: {len(result)}/{len(positions)} analyzed")
        return result

    except Exception as e:
        log.warning(f"Position AI failed: {e}")
        return {}


