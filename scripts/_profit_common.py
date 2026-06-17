"""Capa común para el análisis de profit (Etapa 1).

Scripts de investigación LOCAL, read-only sobre el feature CSV que genera
`scoring_backtest.py`. No tocan la DB, ni producción, ni `analysis/scoring.py`.

Convenciones de unidades (verificadas contra el CSV):
  * `current_rate`, `fwd_{h}_total` están en PORCENTAJE (ya ×100).
    `fwd_1_total` de la fila i == `current_rate` de la fila i+1.
  * El fee round-trip de producción es 0.003 (fracción) = 0.30 en % → `DEFAULT_FEE_PCT`.
    (`scripts/_scoring_data.py:estimate_fee_drag`, "spot 0.10%×2 + perp 0.05%×2").
  * `fee_drag` del CSV es un RATIO normalizado (cap 1.0). NUNCA se resta como costo.

Retorno realizado direccion-ajustado:
  La posición se abre del lado que COBRA funding según el signo del rate actual.
  Para tasas negativas el retorno realizado tiene signo opuesto a la suma cruda:
      realizado_h = sign(current_rate) * fwd_{h}_total
  net_h = realizado_h − fee_pct
  (Pasar `raw=True` usa la suma cruda con signo, la convención del backtest viejo.)
"""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pandas as pd

# Pesos v10.6 reales (analysis/scoring.py). Base 99 → score clamp [0,100].
V106_WEIGHTS = {
    "consistency": 46,
    "stability":   21,
    "yield":       17,
    "fee":          8,
    "liquidity":    6,
    "trend":        1,
}
V106_BASE = sum(V106_WEIGHTS.values())  # 99

# Drivers crudos (independientes de la versión del scoring) por dimensión v10.6.
# Cada tupla: (columna, dirección esperada respecto a la ganancia).
#   +1 = mayor driver debería implicar más ganancia; −1 = inverso.
DIMENSION_DRIVERS = {
    "consistency": [("streak", +1), ("pct_positive", +1)],
    "stability":   [("cv", -1), ("min_ratio", +1)],
    "yield":       [("settlement_avg", +1), ("abs_current_rate", +1)],
    "fee":         [("fee_drag", -1)],
    "liquidity":   [("volume", +1)],
    "trend":       [("mom_roc", +1), ("percentile", +1)],
}

# Horizontes disponibles en el CSV → días aprox (intervalos de 8h).
HORIZON_DAYS = {8: 2.7, 24: 8.0, 72: 24.0}
PRIMARY_HORIZON = 24          # ~8 días → cae dentro de la ventana objetivo 7–15d
DEFAULT_FEE_PCT = 0.30        # = 0.003 round-trip × 100
DEFAULT_FEES = [0.05, 0.10, 0.20, 0.30]

DEFAULT_FEATURES = "reports/backtest_20260615_features.csv"


def load_features(path: str, universe: str = "positive") -> pd.DataFrame:
    """Carga el feature CSV, deriva auxiliares y aplica el universo.

    universe:
      * "positive" (default) — solo `current_rate > 0`. Es el caso enterable
        SIN ambigüedad de dirección: shorteás el perp y cobrás funding, válido
        para CUALQUIER modo (spot_perp/cross/defi). ~81% de las filas.
      * "all" — todas las filas, con retorno direccion-ajustado
        (`sign(rate)×fwd`). Asume que SIEMPRE podés tomar el lado que cobra
        (cierto para cross/defi, FALSO para spot_perp). Útil como contraste,
        pero sobre-acredita las oportunidades de funding negativo.
    """
    p = Path(path)
    if not p.exists():
        sys.exit(f"No existe el feature CSV: {p}\n"
                 f"Genera uno con: python scripts/scoring_backtest.py")
    df = pd.read_csv(p)
    if universe == "positive":
        df = df[df["current_rate"] > 0].copy()
    elif universe != "all":
        sys.exit(f"universe inválido: {universe} (usar 'positive' o 'all')")
    df["abs_current_rate"] = df["current_rate"].abs()
    df["_sign"] = np.sign(df["current_rate"])
    return df


def realized(df: pd.DataFrame, h: int, raw: bool = False) -> pd.Series:
    """Retorno realizado (%) a horizonte h. Direccion-ajustado salvo raw=True."""
    col = f"fwd_{h}_total"
    s = df[col]
    return s if raw else df["_sign"] * s


def net(df: pd.DataFrame, h: int, fee_pct: float, raw: bool = False) -> pd.Series:
    """Retorno neto (%) = realizado − fee."""
    return realized(df, h, raw=raw) - fee_pct


def spearman(a: pd.Series, b: pd.Series) -> float:
    """Spearman ρ sin scipy: Pearson sobre los rangos. NaN-safe."""
    m = a.notna() & b.notna()
    if m.sum() < 3:
        return float("nan")
    return a[m].rank().corr(b[m].rank())


def pearson(a: pd.Series, b: pd.Series) -> float:
    m = a.notna() & b.notna()
    if m.sum() < 3:
        return float("nan")
    return a[m].corr(b[m])


def sharpe_like(net_series: pd.Series) -> float:
    """mean/std del neto — ganancia ajustada a riesgo en un número."""
    s = net_series.dropna()
    sd = s.std()
    if sd is None or sd == 0 or np.isnan(sd):
        return float("nan")
    return s.mean() / sd


def decile_table(df: pd.DataFrame, value_col: str, fee_pct: float,
                 h: int, raw: bool = False, q: int = 10) -> pd.DataFrame:
    """Tabla por decil de `value_col` (típicamente 'score') con net a horizonte h.

    Devuelve N, mean/median net, % rentable y Sharpe-like por decil.
    """
    d = df[[value_col]].copy()
    d["net"] = net(df, h, fee_pct, raw=raw)
    d = d.dropna(subset=[value_col, "net"])
    try:
        d["bucket"] = pd.qcut(d[value_col].rank(method="first"), q,
                              labels=list(range(1, q + 1)))
    except ValueError:
        d["bucket"] = pd.qcut(d[value_col], q, duplicates="drop")
    rows = []
    for b, g in d.groupby("bucket", observed=True):
        rows.append({
            "decile": int(b) if str(b).isdigit() else b,
            "n": len(g),
            "score_lo": round(g[value_col].min(), 1),
            "score_hi": round(g[value_col].max(), 1),
            "mean_net": round(g["net"].mean(), 4),
            "median_net": round(g["net"].median(), 4),
            "pct_profit": round(100 * (g["net"] > 0).mean(), 1),
            "sharpe": round(sharpe_like(g["net"]), 3),
        })
    return pd.DataFrame(rows)


def md_table(df: pd.DataFrame, cols: list[str] | None = None) -> str:
    """DataFrame → tabla markdown simple."""
    cols = cols or list(df.columns)
    head = "| " + " | ".join(cols) + " |"
    sep = "| " + " | ".join("---" for _ in cols) + " |"
    body = "\n".join(
        "| " + " | ".join(str(r[c]) for c in cols) + " |"
        for _, r in df.iterrows()
    )
    return "\n".join([head, sep, body])
