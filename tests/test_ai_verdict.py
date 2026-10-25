"""Veredictos deterministas del análisis IA (el LLM explica, no decide)."""
from types import SimpleNamespace

from analysis import ai_analyzer as ai

NO_KEYS = SimpleNamespace(GEMINI_API_KEY_1="", GEMINI_API_KEY_2="", GEMINI_API_KEY_3="")


def test_opportunity_verdict_thresholds():
    assert ai.opportunity_verdict({"model_prediction": None})["signal"] == "SIN MODELO"
    assert ai.opportunity_verdict({"model_prediction": -3})["signal"] == "NO ENTRAR"
    assert ai.opportunity_verdict({"model_prediction": 2})["signal"] == "ESPERAR"
    # z alto ya no fuerza EVITAR: manda el modelo
    opp = {"model_prediction": 40, "indicators": {"z_score": 3.1}}
    assert ai.opportunity_verdict(opp)["signal"] == "ENTRAR"


def test_leverage_table_capital_split():
    rows = {r["x"]: r for r in ai._lev_table({"model_prediction": 20.0}, 1000)}
    assert rows[1]["roc"] == 10.0            # 1x: nocional = C/2
    assert rows[3]["roc"] == 15.0            # 3x: nocional = 0.75·C
    assert rows[3]["usd_d"] == round(1000 * 0.75 * 0.20 / 365, 2)
    assert rows[1]["liq"] > rows[3]["liq"]


def test_analyze_opportunity_without_key_returns_verdict():
    out = ai.analyze_opportunity({"_id": "X", "symbol": "X", "model_prediction": 12, "spot_venue": "bsc"},
                                 NO_KEYS, capital=500)
    assert out["signal"] == "ENTRAR" and out["llm"] is False
    assert out["facts"]["venue"] == "alpha" and out["facts"]["capital"] == 500


def test_position_verdict_exit_rule():
    assert ai.position_verdict({"lr": [0.01, -0.001, 0.0]})["signal"] == "CERRAR"
    assert ai.position_verdict({"lr": [0.01, 0.02, -0.001]})["signal"] == "VIGILAR"
    assert ai.position_verdict({"lr": [0.01, 0.02], "h": 800})["signal"] == "VIGILAR"
    assert ai.position_verdict({"lr": [0.01, 0.02], "h": 50})["signal"] == "MANTENER"


def test_parse_ai_response_fenced_json():
    txt = '```json\n{"analyses":[{"id":"1","signal":"cerrar","confidence":7,"analysis":"x"}]}\n```'
    r = ai._parse_ai_response(txt, ("MANTENER", "CERRAR", "VIGILAR"), "VIGILAR")
    assert r["1"]["signal"] == "CERRAR"
