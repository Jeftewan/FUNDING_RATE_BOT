"""Reglas puras del piloto LLM (analysis/signals.py), usadas por profit_study --signals."""
from analysis import signals as sig


def test_daily_pct_and_floor():
    assert sig.daily_pct(0.0001, 8) == 0.03                  # 0.01 % por 8 h = piso
    assert sig.daily_pct(0.0001, 4) == 0.06
    assert sig.funding_at_floor([0.2, 0.03, 0.01], 2)
    assert not sig.funding_at_floor([0.03, 0.2], 2)
    assert not sig.funding_at_floor([0.01], 3)               # faltan pagos


def test_exit_signal():
    assert sig.exit_signal([0.1, 0.03], 0.35, tp=0.3, n=1)
    assert not sig.exit_signal([0.1, 0.03], 0.25, tp=0.3, n=1)
    assert not sig.exit_signal([0.1, 0.03], None, tp=0.3, n=1)


def test_fill_share_ignores_missing():
    assert sig.fill_share([0.3, None, 0.1, float("nan"), 0.25], 0.25) == 2 / 3
    assert sig.fill_share([None], 0.25) is None
