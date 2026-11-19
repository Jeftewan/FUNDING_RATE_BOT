"""Reglas deterministas de entrada y salida derivadas del piloto LLM — NO adoptadas en prod
(reports/llm_policy_pilot_20261118.md), evaluadas en scripts/profit_study.py --signals
(reports/signal_rules_study_20261118.md: ninguna pasa el criterio). Funciones puras, sin I/O;
si una variante futura pasa, prod debe usar estas mismas funciones (paridad, como ml_features).

- Salida (EXIT_NOW): el funding cayó al piso en los últimos `n` pagos liquidados y el basis
  ejecutable de venta paga ≥ `tp` → cerrar libera el cupo con ganancia de basis.
- Entrada (fill share): fracción de las últimas 24 h en que el basis ejecutable de compra
  estuvo ≥ θ. Si es baja, la espera de 6 h casi nunca llena y bloquea el cupo.
"""

# Piso del funding de Binance: interés 0.03 %/día (0.01 % por 8 h), en % diario.
FUNDING_FLOOR_DAILY = 0.03
FLOOR_EPS = 0.005


def daily_pct(rate: float, interval_hours: float = 8.0) -> float:
    """Tasa por intervalo (fracción, p. ej. 0.0001) → % diario."""
    return float(rate) * 100 * 24 / (interval_hours or 8.0)


def funding_at_floor(daily_rates, n: int) -> bool:
    """Los últimos `n` pagos liquidados (en % diario) están en el piso o debajo."""
    if n <= 0 or len(daily_rates) < n:
        return False
    lim = FUNDING_FLOOR_DAILY + FLOOR_EPS
    return all(r <= lim for r in list(daily_rates)[-n:])


def exit_signal(daily_rates, sell_basis_pct, tp: float, n: int) -> bool:
    """Salir ya: funding en el piso en los últimos `n` pagos y basis de venta ≥ tp (%)."""
    if sell_basis_pct is None:
        return False
    return funding_at_floor(daily_rates, n) and sell_basis_pct >= tp


def fill_share(basis_values, theta: float):
    """Fracción de las lecturas válidas con basis ejecutable ≥ θ. None sin lecturas."""
    vals = [v for v in basis_values if v is not None and v == v]
    if not vals:
        return None
    return sum(v >= theta for v in vals) / len(vals)
