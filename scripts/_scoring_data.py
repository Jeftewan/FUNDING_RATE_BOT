#!/usr/bin/env python3
"""Capa compartida de datos para los scripts de scoring (backtest + optimizer).

Centraliza lo que ambos scripts necesitan idéntico:
  - conexión a la DB (desde DATABASE_URL),
  - carga + caché de funding_rate_snapshots,
  - estimación de fee_drag,
  - cómputo de los features crudos por ventana (cv, streak, pct, etc.).

Las capas que DIFIEREN entre scripts (forward returns, decay, durabilidad,
scoring real vs paramétrico) viven en cada script, no aquí.

Requiere: pip install -r requirements-dev.txt
"""

import os
import statistics
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path

import pandas as pd
from sqlalchemy import create_engine, text

# ── project root: scripts/ → repo root ──────────────────────────────────────
ROOT = Path(__file__).resolve().parent.parent
CACHE_DIR = ROOT / "cache"


# ── Database ──────────────────────────────────────────────────────────────────

def get_engine():
    """SQLAlchemy engine desde DATABASE_URL (solo lectura en la práctica)."""
    url = os.environ.get("DATABASE_URL", "")
    if not url:
        sys.exit("ERROR: DATABASE_URL no está configurada.")
    if url.startswith("postgres://"):
        url = "postgresql://" + url[len("postgres://"):]
    return create_engine(url, pool_pre_ping=True)


def load_fr_snapshots(force_reload: bool = False, days: int = 90) -> pd.DataFrame:
    """Carga funding_rate_snapshots de los últimos `days`, con caché CSV.

    El caché se invalida si tiene más de 1h. Devuelve un DataFrame ordenado
    por (symbol, exchange, captured_at).
    """
    CACHE_DIR.mkdir(exist_ok=True)
    cache_fr = CACHE_DIR / "fr_snapshots.csv"

    if not force_reload and cache_fr.exists():
        age_h = (datetime.now().timestamp() - cache_fr.stat().st_mtime) / 3600
        if age_h < 1.0:
            print(f"  Usando caché ({age_h:.1f}h de antigüedad)")
            return pd.read_csv(cache_fr, parse_dates=["captured_at"])

    print("  Consultando base de datos...")
    engine = get_engine()
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)

    try:
        with engine.connect() as conn:
            fr = pd.read_sql(
                text("""
                    SELECT symbol, exchange, rate, volume_24h, interval_hours,
                           funding_ts, captured_at
                    FROM   funding_rate_snapshots
                    WHERE  captured_at >= :cutoff
                    ORDER  BY symbol, exchange, captured_at
                """),
                conn, params={"cutoff": cutoff},
            )
    except Exception as exc:
        sys.exit(f"Error al consultar la DB: {exc}")

    print(f"  funding_rate_snapshots : {len(fr):,} filas")
    fr.to_csv(cache_fr, index=False)
    return fr


# ── Feature helpers ───────────────────────────────────────────────────────────

def estimate_fee_drag(settlement_avg: float, ppd: float,
                      hold_days: int = 30) -> float:
    """fee_drag = fees round-trip / revenue esperado en hold_days.

    Round-trip ~0.30% (spot 0.10% ×2 + perp 0.05% ×2).
    """
    revenue = abs(settlement_avg) * ppd * hold_days
    if revenue < 1e-10:
        return 1.0
    return min(0.003 / revenue, 1.0)


def base_window_features(rates: list, volumes: list, intervs: list,
                         i: int, lookback: int) -> dict | None:
    """Computa los features crudos de la ventana que termina en el índice `i`.

    `rates`/`volumes`/`intervs` son listas por par (oldest→newest). `i` es el
    índice del snapshot "actual"; la ventana de lookback es rates[i-lookback:i].

    Devuelve un dict con cv, min_ratio, streak, pct, settlement_avg, ppd,
    fee_drag, current_rate, hist — o None si la ventana no tiene ≥5 muestras
    con magnitud no trivial (mismo gate que usa el scoring real).
    """
    lb_start = max(0, i - lookback)
    hist = rates[lb_start:i]            # ventana de lookback (oldest→newest)
    curr = rates[i]
    vol = volumes[i]
    ih = max(float(intervs[i]), 1.0)
    ppd = 24.0 / ih

    abs_hist = [abs(r) for r in hist if abs(r) > 1e-12]
    if len(abs_hist) < 5:
        return None

    mean_h = statistics.mean(abs_hist)
    std_h = statistics.stdev(abs_hist) if len(abs_hist) > 1 else 0.0
    cv = std_h / mean_h if mean_h > 1e-12 else 999.0
    min_ratio = min(abs_hist) / mean_h if mean_h > 1e-12 else 0.0

    # streak: positivos consecutivos desde el final
    streak = 0
    for r in reversed(hist):
        if r > 0:
            streak += 1
        else:
            break

    pct = sum(1 for r in hist if r > 0) / len(hist) * 100
    settlement_avg = mean_h

    return {
        "cv": cv,
        "min_ratio": min_ratio,
        "streak": streak,
        "pct": pct,
        "settlement_avg": settlement_avg,
        "ppd": ppd,
        "fee_drag": estimate_fee_drag(settlement_avg, ppd),
        "current_rate": curr,
        "interval_h": ih,
        "volume": vol,
        "hist": hist,
    }
