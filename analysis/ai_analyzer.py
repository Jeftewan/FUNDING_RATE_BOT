"""AI-powered opportunity analysis via Groq (Llama 3.3 70B)."""

import json
import logging
import re

log = logging.getLogger(__name__)

GROQ_MODEL = "llama-3.3-70b-versatile"
MAX_OPPS = 10
TIMEOUT = 15

SYSTEM_PROMPT = (
    "Eres un analista experto en arbitraje de funding rates de criptomonedas. "
    "Analiza cada oportunidad y responde SOLO con un objeto JSON valido:\n"
    '{"analyses":[{"id":"_id de la oportunidad","signal":"COMPRAR|MANTENER|EVITAR",'
    '"confidence":1-10,"analysis":"2-3 oraciones en espanol"}]}\n\n'
    "Criterios:\n"
    "- COMPRAR: score alto (>65), momentum positivo/estable, break-even razonable (<8h), "
    "volumen saludable, sin z-score extremo.\n"
    "- EVITAR: z_score extremo (>2.5), momentum negativo, fees altos vs ganancia, "
    "volumen muy bajo, spike terminando (reversion).\n"
    "- MANTENER: condiciones mixtas, requiere vigilancia.\n\n"
    "Se breve y directo. Enfocate en: momento de entrada, riesgo principal, "
    "y tiempo sugerido de hold. No repitas los numeros, interpreta."
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
        "gr": opp.get("stability_grade", "?"),
        "apr": round(opp.get("apr", 0), 1),
        "beh": round(opp.get("break_even_hours", 0), 1),
        "hold": opp.get("estimated_hold_days", "?"),
        "fees": round(opp.get("fees_total", 0) or opp.get("total_fees", 0) or 0, 2),
        "d1k": round(opp.get("daily_income_per_1000", 0), 2),
        "n3d": round(opp.get("net_3d_revenue_per_1000", 0), 2),
        "vol": round((opp.get("volume_24h", 0) or 0) / 1e6, 1),
    }

    if is_cross:
        slim["diff"] = round((opp.get("rate_differential", 0) or 0) * 100, 4)
    else:
        slim["fr"] = round((opp.get("funding_rate", 0) or 0) * 100, 4)

    if ind:
        slim["ind"] = {
            "mom": ind.get("momentum_signal", "flat"),
            "z": round(ind.get("z_score", 0), 1),
            "pct": round(ind.get("percentile", 0)),
            "reg": ind.get("regime", "normal"),
            "spike": ind.get("is_spike_incoming", False),
            "rev": ind.get("is_spike_ending", False),
        }

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


def _parse_response(text: str) -> dict:
    """Parse Groq response into {opp_id: analysis_dict} map."""
    # Try direct JSON parse
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        # Try extracting JSON from markdown code fence
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
        opp_id = a.get("id", "")
        if not opp_id:
            continue
        signal = a.get("signal", "MANTENER").upper()
        if signal not in ("COMPRAR", "MANTENER", "EVITAR"):
            signal = "MANTENER"
        result[opp_id] = {
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
            max_tokens=1500,
            timeout=TIMEOUT,
        )

        raw = resp.choices[0].message.content or ""
        analysis_map = _parse_response(raw)

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
