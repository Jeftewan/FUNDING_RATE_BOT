"""AI-powered opportunity analysis via Groq (Llama 3.3 70B)."""

import json
import logging
import re

log = logging.getLogger(__name__)

GROQ_MODEL = "llama-3.3-70b-versatile"
MAX_OPPS = 10
TIMEOUT = 15

SYSTEM_PROMPT = (
    "Analista de arbitraje de funding rates. Responde SOLO JSON:\n"
    '{"analyses":[{"id":"_id","signal":"COMPRAR|MANTENER|EVITAR",'
    '"confidence":1-10,"analysis":"max 15 palabras"}]}\n\n'
    "Reglas de decision:\n"
    "COMPRAR: sc>60 + mom!=negative + beh<10h + vol>5M + z<2.0\n"
    "EVITAR: z>2.5 | mom=negative | rev=true | vol<2M | beh>15h\n"
    "MANTENER: no cumple COMPRAR ni EVITAR\n\n"
    "En analysis: di QUE HACER y POR QUE en max 15 palabras. "
    "Ej: 'Entrar ahora, momentum fuerte y fees se recuperan en 3h'"
)


def _slim_opp(opp: dict) -> dict:
    """Extract only relevant fields for AI analysis, keeping tokens minimal."""
    ind = opp.get("indicators", {})
    is_cross = opp.get("mode") == "cross_exchange"

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
    }

    if is_cross:
        slim["diff"] = round((opp.get("rate_differential", 0) or 0) * 100, 4)
    else:
        slim["fr"] = round((opp.get("funding_rate", 0) or 0) * 100, 4)

    # Indicadores aplanados — sin dict anidado para ahorrar tokens
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
        f"Analiza estas {len(slim_data)} oportunidades (ordenadas por score):\n"
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
            "analysis": str(a.get("analysis", ""))[:200],
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
            temperature=0.2,
            max_tokens=1200,
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
    "Evalua posiciones de arbitraje de funding rates. Responde SOLO JSON:\n"
    '{"analyses":[{"id":"id","signal":"MANTENER|CERRAR|VIGILAR",'
    '"confidence":1-10,"analysis":"max 15 palabras"}]}\n\n'
    "Reglas:\n"
    "CERRAR: rev=true | cfr~0 | apr<0 | (cfr<efr/3 y h>48)\n"
    "VIGILAR: cfr<efr/2 | apr cayendo | lr muestra tendencia a baja\n"
    "MANTENER: cfr estable o subiendo, apr>0, sin reversion\n\n"
    "En analysis: ACCION CONCRETA y razon. "
    "Ej: 'Cerrar ya, FR revertido hace 2 pagos' o 'Mantener, FR estable y APR 45%'"
)


def _slim_position(pos: dict) -> dict:
    """Ultra-compact position data for AI — minimal tokens."""
    payments = pos.get("payments") or []
    # Last 3 rates for better trend detection
    recent = [round(p["rate"] * 100, 4) for p in payments[-3:]] if payments else []

    return {
        "id": str(pos.get("id", "")),
        "sym": pos.get("symbol", ""),
        "mode": "cross" if pos.get("mode") == "cross_exchange" else "sp",
        "cap": round(pos.get("capital_used", 0) or 0),
        "efr": round((pos.get("entry_fr", 0) or 0) * 100, 4),
        "cfr": round((pos.get("current_fr", 0) or 0) * 100, 4),
        "apr": round(pos.get("current_apr", 0) or 0, 1),
        "net": round(pos.get("net_earned", 0) or 0, 2),
        "h": round(pos.get("elapsed_h", 0) or 0, 1),
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
            f"Evalua estas {len(slim_data)} posiciones activas:\n"
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
            temperature=0.2,
            max_tokens=800,
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


