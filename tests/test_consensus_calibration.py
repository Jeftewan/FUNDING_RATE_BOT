"""Funciones de scripts/consensus_study.py: label con k pagos ≤ 0, Venn-Abers y tramos."""
import os
import sys

import numpy as np
import pandas as pd
import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "scripts"))

cst = pytest.importorskip("consensus_study")


def test_ivap_bounds_and_monotone():
    rng = np.random.default_rng(1)
    s = rng.normal(size=800)
    y = (rng.random(800) < 1 / (1 + np.exp(-2 * s))).astype(float)
    g = cst.ivap_grid(s, y, n_grid=41)
    p0, p1, p = map(np.asarray, (g["p0"], g["p1"], g["p"]))
    assert np.all(p0 <= p1 + 1e-12) and np.all(p0 <= p + 1e-12) and np.all(p <= p1 + 1e-12)
    assert np.all(np.diff(p) >= -1e-9)


def test_k_labels_k2_matches_h30():
    import ml_v2
    rng = np.random.default_rng(2)
    n = 300
    fr = pd.DataFrame({"symbol": "X", "rate": rng.normal(0.0002, 0.0004, n), "interval_hours": 8,
                       "funding_ts": np.arange(n, dtype=np.int64) * 8 * 3_600_000})
    a = cst.k_labels(fr, k=2).set_index("funding_ts")
    b = ml_v2.h30_labels(fr).set_index("funding_ts")
    assert np.allclose(a["fund_K"], b["fund_H30"], equal_nan=True)
    assert np.allclose(a["hold_K"], b["hold_H30"], equal_nan=True)
    k3 = cst.k_labels(fr, k=3).set_index("funding_ts")
    both = k3["hold_K"].notna() & a["hold_K"].notna()
    assert both.sum() > 100
    assert (k3.loc[both, "hold_K"] >= a.loc[both, "hold_K"]).all()   # 3 pagos ≤ 0 nunca sale antes


def test_tramo_table_flags_miscalibration():
    p = np.full(200, 0.85)
    y = np.r_[np.ones(100), np.zeros(100)]
    r = cst.tramo_table(p, y, np.zeros(200))
    assert r[0]["tramo"] == 9 and not r[0]["ok"]


def test_apr_map_monotone_and_flat_outside():
    acs = pytest.importorskip("apr_calibration_study")
    assert list(acs.pava([3, 1, 2, 5], [1, 1, 1, 1])) == [2, 2, 2, 5]
    rng = np.random.default_rng(3)
    x = rng.uniform(0, 60, 5000)
    y = 0.6 * x + 4 + rng.normal(0, 10, 5000)          # sobre-disperso: pendiente < 1
    k = acs.fit_map(x, y)
    ky = np.array(k)[:, 1]
    assert np.all(np.diff(ky) >= -1e-9)
    out = acs.apply_map(k, [-10, 30, 1000])
    assert out[0] == ky[0] and out[2] == ky[-1] and abs(out[1] - 22) < 3
