"""Features diarios de las señales horarias (market_feature_snapshots) — stdlib.

Una sola función para entrenamiento (scripts/signals_dataset.py, scripts/ml_v3_signals.py,
scripts/analyst_study.py) e inferencia (analysis/opportunity_analysis.py): paridad por
construcción. Se agregan 24 h para que la diferencia de fuente (prod: mids del book a
~:04 / VWAP 5×1m on-chain; offline: cierres 1h / VWAP 1h on-chain) no pese.

Entrada: filas horarias de UN par ordenadas por ts_hour ascendente, ya recortadas a
ts_hour ≤ t (sin mirar el futuro). Cada fila es un dict con las columnas de la tabla
(`ts_hour` en epoch s, `basis_pct`, `open_interest`, `open_interest_usd`,
`top_long_short_pos_ratio`, `volatility_7d`).
"""
import math

SIGNAL_FEATURES = ["basis_24h", "basis_chg_24h", "log_oi_usd", "oi_chg_24h_pct",
                   "top_ls_24h", "volatility_7d"]
H = 3600
MIN_HOURS = 12          # mínimo de horas con dato en una ventana de 24 h


def _f(v):
    try:
        x = float(v)
    except (TypeError, ValueError):
        return None
    return x if math.isfinite(x) else None


def _mean(vals):
    vals = [v for v in vals if v is not None]
    return sum(vals) / len(vals) if len(vals) >= MIN_HOURS else None


def _window(rows, lo, hi, col):
    """Valores de `col` con lo < ts_hour ≤ hi."""
    return [_f(r.get(col)) for r in rows if lo < int(r["ts_hour"]) <= hi]


def _last(rows, col, lo):
    for r in reversed(rows):
        if int(r["ts_hour"]) <= lo:
            break
        v = _f(r.get(col))
        if v is not None:
            return v, int(r["ts_hour"])
    return None, None


def signal_features(rows, t_s: int) -> dict:
    """Features en el instante t_s (epoch s) con las filas horarias de un par.
    Cualquier feature sin datos suficientes → None (HistGB lo trata como faltante)."""
    out = {k: None for k in SIGNAL_FEATURES}
    if not rows:
        return out
    end = int(t_s) // H * H
    rows = [r for r in rows if end - 80 * H < int(r["ts_hour"]) <= end]
    if not rows:
        return out
    b24 = _mean(_window(rows, end - 24 * H, end, "basis_pct"))
    b_prev = _mean(_window(rows, end - 72 * H, end - 24 * H, "basis_pct"))
    out["basis_24h"] = b24
    out["basis_chg_24h"] = b24 - b_prev if b24 is not None and b_prev is not None else None
    oi_usd, _ = _last(rows, "open_interest_usd", end - 3 * H)
    out["log_oi_usd"] = math.log10(oi_usd) if oi_usd and oi_usd > 0 else None
    oi, ts_oi = _last(rows, "open_interest", end - 3 * H)
    if oi is not None:
        prev = [(_f(r.get("open_interest")), int(r["ts_hour"])) for r in rows
                if ts_oi - 26 * H <= int(r["ts_hour"]) <= ts_oi - 22 * H]
        prev = [p for p in prev if p[0]]
        if prev:
            ref = min(prev, key=lambda p: abs(p[1] - (ts_oi - 24 * H)))[0]
            out["oi_chg_24h_pct"] = (oi / ref - 1) * 100
    out["top_ls_24h"] = _mean(_window(rows, end - 24 * H, end, "top_long_short_pos_ratio"))
    out["volatility_7d"], _ = _last(rows, "volatility_7d", end - 48 * H)
    return out
