"""Análisis IA bajo demanda (Gemini 3.1 Flash-Lite): oportunidades y posiciones."""

import json
import logging
import random
import re

log = logging.getLogger(__name__)

GEMINI_MODEL = "gemini-3.1-flash-lite"
TIMEOUT = 25

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


def _parse_ai_response(text: str, valid_signals: tuple, default_signal: str) -> dict:
    """Parse the LLM JSON response into {id: {signal, confidence, analysis}} map."""
    data = _parse_json(text)
    if not isinstance(data, dict):
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


# Veredicto determinista desde el modelo (el LLM solo explica). Umbrales sobre el
# Net APR predicho por nocional (`model_prediction`): reports/exit_policy_20261022.md
# y reports/attribution_20261025.md muestran que el gate útil es pred > 0; pisos de
# tasa más altos dejan slots vacíos. Las reglas fijas del prompt anterior (z ≥ 2 →
# EVITAR) chocaban con el modelo: los picos Alpha rentables tienen z alto.
ENTER_NAPR = 5.0          # ≥ → ENTRAR (debajo es ruido del modelo)
LEVERAGES = (1, 2, 3)     # tope de leverage del negocio (decisión 2026-10-25)
LIQ_MMR = 0.01

OPP_SYSTEM_PROMPT = (
    "Eres analista cuantitativo de arbitraje de funding rates (long spot + short perp, delta neutral). "
    "Recibes UNA oportunidad con su veredicto YA DECIDIDO por el modelo predictivo (campo 'verdict') "
    "y los numeros calculados. Tu trabajo es EXPLICAR ese veredicto en lenguaje claro y señalar riesgos "
    "concretos; NO cambies el veredicto ni inventes datos.\n\n"
    "Campos: sym, venue (cex=spot en Binance; alpha=spot solo on-chain en Binance Alpha/BSC: mas carry "
    "pero swap mas caro y venta menos liquida), napr (Net APR predicho por el modelo, % anual neto de fees "
    "sobre el nocional, METRICA PRINCIPAL), fr (funding actual %/pago), ih (horas entre pagos), "
    "lev (tabla por apalancamiento: roc = rendimiento anual sobre el capital total, usd_d = USD/dia "
    "esperados para el capital indicado, liq = % de subida que liquida el short), fees_usd, beh (horas "
    "para recuperar fees), con (% de pagos favorables), streak (pagos favorables seguidos), z (z-score "
    "de la tasa actual; alto es normal en picos y el modelo ya lo pondero), mom (momentum), lr (ultimas "
    "tasas %), mins (minutos al proximo pago).\n\n"
    "Responde SOLO JSON: {\"analysis\":\"50-80 palabras: que paga, por que el modelo da ese veredicto, "
    "con numeros\",\"risks\":[\"1-3 riesgos concretos y medibles\"],\"watch\":\"que vigilar y cuando salir "
    "(regla del negocio: cerrar tras dos pagos seguidos <= 0)\",\"leverage\":\"recomendacion entre 1x y 3x "
    "segun liq y volatilidad, una frase\"}"
)


def _lev_table(opp: dict, capital: float) -> list:
    """Rendimiento sobre capital por leverage: con margen C/(L+1) el nocional es C·L/(L+1)."""
    napr = opp.get("model_prediction")
    out = []
    for lev in LEVERAGES:
        frac = lev / (lev + 1)
        roc = napr * frac if napr is not None else None
        out.append({"x": lev, "roc": round(roc, 1) if roc is not None else None,
                    "usd_d": round(capital * frac * napr / 100 / 365, 2) if napr is not None else None,
                    "liq": round(max(0.0, 1 / lev - LIQ_MMR) * 100, 1)})
    return out


def opportunity_verdict(opp: dict) -> dict:
    """ENTRAR / ESPERAR / NO ENTRAR desde el Net APR predicho; SIN MODELO si no predijo."""
    napr = opp.get("model_prediction")
    if napr is None:
        return {"signal": "SIN MODELO",
                "reason": "El modelo no predijo este par (modo cross/DeFi o historial corto): solo heurístico."}
    if napr <= 0:
        return {"signal": "NO ENTRAR", "reason": f"El modelo espera retorno neto ≤ 0 ({napr:.1f} % anual)."}
    if napr < ENTER_NAPR:
        return {"signal": "ESPERAR", "reason": f"Retorno neto esperado bajo ({napr:.1f} % anual < {ENTER_NAPR:.0f} %)."}
    return {"signal": "ENTRAR", "reason": f"Retorno neto esperado {napr:.1f} % anual sobre el nocional."}


def _opp_facts(opp: dict, capital: float) -> dict:
    ind = opp.get("indicators", {}) or {}
    hist = opp.get("history", {}) or {}
    rates = hist.get("_rates") or []
    return {
        "sym": opp.get("symbol", ""),
        "venue": "alpha" if opp.get("spot_venue") == "bsc" else "cex",
        "napr": round(opp["model_prediction"], 1) if opp.get("model_prediction") is not None else None,
        "fr": round((opp.get("funding_rate", 0) or 0) * 100, 4),
        "ih": opp.get("interval_hours", 8),
        "lev": _lev_table(opp, capital),
        "capital": round(capital),
        "fees_usd": round(opp.get("fees_total", 0) or 0, 2),
        "beh": round(opp.get("break_even_hours", 0) or 0, 1),
        "con": round(hist.get("pct", hist.get("favorable_pct", 0)) or 0),
        "streak": hist.get("streak", 0),
        "z": round(ind.get("z_score", 0) or 0, 1),
        "mom": ind.get("momentum_signal", "flat") or "flat",
        "lr": [round(r * 100, 4) for r in rates[-10:]],
        "mins": round(opp.get("mins_to_next", -1) or -1),
    }


def analyze_opportunity(opp: dict, config, capital: float = 1000.0) -> dict:
    """Análisis bajo demanda de UNA oportunidad.

    El veredicto es determinista (`opportunity_verdict`); Gemini solo redacta la
    explicación. Sin API key o si Gemini falla, devuelve el veredicto y los
    números sin texto del LLM (nunca lanza).
    """
    verdict = opportunity_verdict(opp)
    facts = _opp_facts(opp, capital)
    out = {"signal": verdict["signal"], "reason": verdict["reason"], "facts": facts,
           "analysis": verdict["reason"], "risks": [], "watch": "", "leverage": "", "llm": False}
    api_key = _get_gemini_key(config)
    if not api_key:
        return out
    try:
        from google import genai
        from google.genai import types

        client = genai.Client(api_key=api_key, http_options=types.HttpOptions(timeout=TIMEOUT * 1000))
        payload = {"verdict": verdict["signal"], "reason": verdict["reason"], **facts}
        resp = client.models.generate_content(
            model=GEMINI_MODEL,
            contents="Explica esta oportunidad:\n" + json.dumps(payload, separators=(",", ":"), ensure_ascii=False),
            config=types.GenerateContentConfig(system_instruction=OPP_SYSTEM_PROMPT, temperature=0.2,
                                               response_mime_type="application/json"),
        )
        data = _parse_json(resp.text or "")
        if data:
            out.update(analysis=str(data.get("analysis") or out["analysis"])[:700],
                       risks=[str(r)[:200] for r in (data.get("risks") or [])][:3],
                       watch=str(data.get("watch") or "")[:300],
                       leverage=str(data.get("leverage") or "")[:200], llm=True)
    except Exception as e:
        log.warning(f"Opportunity AI failed for {opp.get('_id')}: {e}")
    return out


def _parse_json(text: str):
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        m = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.DOTALL)
        if m:
            try:
                return json.loads(m.group(1))
            except json.JSONDecodeError:
                return None
    return None


# ── Position Analysis ─────────────────────────────────────────

POSITION_SYSTEM_PROMPT = (
    "Eres analista experto en arbitraje de funding rates. Tu rol es dar al usuario "
    "una RUTA DE DECISION CLARA sobre cada posicion: mantener, vigilar o cerrar, "
    "y si hay una alternativa mejor, explicar el trade-off concreto.\n"
    "El campo 'verdict' de cada posicion YA esta decidido por las reglas del negocio: usalo como "
    "'signal', explicalo con numeros y da el plan de accion; no lo contradigas.\n\n"
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
    "PRINCIPIO: los fees de entrada ya se pagaron y los de salida se pagan igual cierres "
    "hoy o despues. Cerrar SOLO conviene si el funding FUTURO esperado deja de ser positivo "
    "o si una alternativa rinde mas tras su propio costo de entrada. Un pago negativo aislado "
    "no justifica cerrar.\n"
    "REGLAS de decision:\n"
    "CERRAR:\n"
    "- rev=true Y el ultimo valor de lr <= 0 — dos settlements seguidos sin cobrar\n"
    "- cfr<=0 y la mayoria de lr <= 0 — la posicion ya no genera\n"
    "- sw.rec=SWITCH y sw.val>0 y sw.beh<24 y sw.alt_napr supera al Net APR actual — alternativa solida y mejor\n\n"
    "VIGILAR:\n"
    "- rev=true con lr mayormente positivo — reversion puntual, esperar el proximo pago\n"
    "- cfr<efr/2 — FR ha caido significativamente\n"
    "- trend=down — tendencia descendente en pagos recientes\n"
    "- sw.rec=CONSIDER y sw.alt_napr atractivo — alternativa potencialmente mejor\n"
    "- fee_recovery_pct<60 y h>48 — recuperacion lenta (informativo: los fees ya estan pagados)\n"
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
    "Ejemplo: '1. Cerrar posicion ahora. 2. Abrir SOLUSDT en Binance (Net APR predicho 45%).'\n"
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


def position_verdict(slim: dict) -> dict:
    """Veredicto determinista con la regla de salida del negocio (reports/exit_policy_20261022.md:
    salir tras dos pagos seguidos ≤ 0 y mantener hasta ~30 d)."""
    lr = slim.get("lr") or []
    if len(lr) >= 2 and lr[-1] <= 0 and lr[-2] <= 0:
        return {"signal": "CERRAR", "reason": "Dos pagos seguidos sin cobrar."}
    sw = slim.get("sw") or {}
    if (sw.get("rec") == "SWITCH" and sw.get("val", 0) > 0 and sw.get("beh", 999) < 24
            and sw.get("alt_napr", 0) > slim.get("apr", 0)):
        return {"signal": "CERRAR", "reason": f"Rotar a {sw.get('alt')}: mejora neta tras el costo del cambio."}
    if lr and lr[-1] <= 0:
        return {"signal": "VIGILAR", "reason": "Último pago ≤ 0: cerrar si el próximo también lo es."}
    if slim.get("h", 0) > 30 * 24:
        return {"signal": "VIGILAR", "reason": "Más de 30 días abierta: revisar si sigue rindiendo sobre su promedio."}
    return {"signal": "MANTENER", "reason": "Sigue cobrando funding."}


def analyze_positions(positions: list, config) -> dict:
    """Análisis de posiciones. Returns {pos_id: {signal, confidence, analysis, action_plan?}}.

    El veredicto es determinista (`position_verdict`); Gemini solo lo explica.
    Sin API key o si Gemini falla, devuelve los veredictos con su razón.
    """
    if not positions:
        return {}
    slim_data = [_slim_position(p) for p in positions]
    verdicts = {s["id"]: position_verdict(s) for s in slim_data}
    for s in slim_data:
        s["verdict"] = verdicts[s["id"]]["signal"]
    base = {pid: {"signal": v["signal"], "confidence": 10, "analysis": v["reason"]}
            for pid, v in verdicts.items()}
    api_key = _get_gemini_key(config)
    if not api_key:
        return base

    try:
        from google import genai
        from google.genai import types

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
        for pid, entry in result.items():        # el LLM explica, no decide
            if pid in verdicts:
                entry["signal"] = verdicts[pid]["signal"]
        log.info(f"Position AI: {len(result)}/{len(positions)} analyzed")
        return {**base, **result}

    except Exception as e:
        log.warning(f"Position AI failed: {e}")
        return base


