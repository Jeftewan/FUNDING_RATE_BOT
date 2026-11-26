"""Fórmulas de scripts/ops_review_report.py: cobertura por USDT igual vs cantidad igual."""
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "scripts"))
from ops_review_report import decompose, hedge_pnl  # noqa: E402


@pytest.mark.parametrize("S0,P0,S1,P1", [(1.0, 1.003, 1.2, 1.2), (1.0, 0.997, 0.7, 0.701), (2.0, 2.01, 2.5, 2.49)])
def test_closed_forms(S0, P0, S1, P1):
    X = 1000.0
    b0, b1 = P0 / S0 - 1, P1 / S1 - 1
    usd = hedge_pnl(X, S0, P0, S1, P1, "usd")
    qty = hedge_pnl(X, S0, P0, S1, P1, "qty")
    assert usd == pytest.approx(X * (S1 / S0) * (b0 - b1) / (1 + b0))
    assert qty == pytest.approx(X * (b0 - (S1 / S0) * b1))
    assert qty - usd == pytest.approx(-X * b0 * (P1 / P0 - 1))


def test_decompose_sums_to_price_pnl():
    qs, qp, S0, P0, S1, P1 = 1000.0, 997.0, 1.0, 1.003, 1.2, 1.205
    e, x, d = decompose(qs, qp, S0, P0, S1, P1)
    assert e + x + d == pytest.approx(qs * (S1 - S0) + qp * (P0 - P1))
    assert decompose(qs, qs, S0, P0, S1, P1)[2] == 0
