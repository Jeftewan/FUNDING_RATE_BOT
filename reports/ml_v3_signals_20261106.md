# Modelo v3 — señales de mercado (3 meses) — 20261106

Filas con label H30: 1,275,384; ventana de señales desde 2026-07-27 (163,530 filas, Alpha 54,787). WF semanal 2026-09-02 → 2026-10-23 (8 folds, purga 16 d); hold-out 2026-10-23 → 2026-11-06. Cartera = la de ml_v2 (5 slots, 5.200 USD, 1x, H30); todos los rankers con gate pred > 0.

Cobertura de señales en WF: basis_24h 89%, basis_chg_24h 89%, log_oi_usd 91%, oi_chg_24h_pct 91%, top_ls_24h 91%, volatility_7d 91%

Sesgo conocido: las filas más recientes solo tienen label si salieron pronto (H30 mira 30 d) → el hold-out favorece salidas tempranas para todos los rankers por igual.

| ranking | IC diario WF | top-5 y_rate WF | WF PnL USD | WF anual % | trades | % gan. | IC hold-out | top-5 hold-out | hold-out PnL USD | hold-out anual % | trades |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| vigente | 0.503 | 9.6 | 157.6 | 21.8 | 18 | 61.1 | 0.069 | -38.5 | -2.0 | -1.0 | 4 |
| E_all | 0.514 | 4.4 | 148.7 | 20.5 | 20 | 55.0 | 0.157 | -40.5 | 9.1 | 4.6 | 4 |
| E_all_sig | 0.532 | 7.7 | 177.4 | 24.5 | 17 | 76.5 | 0.175 | -62.8 | -8.9 | -4.5 | 5 |
| E_win | 0.505 | 8.9 | 149.5 | 20.6 | 19 | 63.2 | 0.112 | -61.3 | 6.0 | 3.0 | 5 |
| E_win_sig | 0.521 | 7.7 | 150.9 | 20.8 | 22 | 45.5 | 0.141 | -72.8 | -23.9 | -12.0 | 6 |

## Gate

- `E_all_sig`: NO pasa (WF y hold-out > vigente y > sin señales, top-5 no peor)
- `E_win_sig`: NO pasa (WF y hold-out > vigente y > sin señales, top-5 no peor)

## Veredicto

- **No se exporta.** `E_all_sig` (14 features + señales, todo el historial) es el mejor en walk-forward
  (IC 0.532 vs 0.503 del vigente, PnL 177 vs 158 USD, 76.5 % vs 61.1 % de trades ganadores), pero en el
  hold-out de 2 semanas pierde (−8.9 vs −2.0 USD) y su top-5 diario es peor (−62.8 vs −38.5).
- Muestra chica: ~18 trades en WF y 4–6 en hold-out por ranking; las diferencias son ruido estadístico.
  El hold-out (23-oct → 6-nov) es además un tramo malo para todos (top-5 negativo en los 5 rankings).
- Las señales que más pesan en el modelo son `log_oi_usd` y `volatility_7d`; `basis_24h`, `top_ls_24h` y
  `basis_chg_24h` no suman fuera de muestra (ΔR² negativo). `basis_chg_24h` además tiene paridad baja
  con prod (ρ 0.66): no usarla.
- Re-probar con ≥ 6 meses de señales (`signals_dataset.py --days 200`, ~1 h de descarga).
- Paridad offline vs prod (`signals_dataset.py --check`, 419 símbolos, 28-oct → 6-nov): basis_24h ρ 0.93,
  log_oi_usd 1.00, oi_chg_24h 0.99, top_ls_24h 1.00, volatility_7d 0.97, basis_chg_24h 0.66.

## Importancia por permutación (E_all_sig, hold-out, ΔR²)

| feature | ΔR² |
| --- | --- |
| pct | 0.0840 |
| volume | 0.0795 |
| cv | 0.0634 |
| reality_ratio | 0.0491 |
| log_oi_usd | 0.0414 |
| settlement_avg | 0.0331 |
| volatility_7d | 0.0221 |
| pctl_points | 0.0002 |
| oi_chg_24h_pct | -0.0044 |
| fee_drag_det | -0.0095 |
| z_value | -0.0109 |
| min_ratio | -0.0151 |
| current_rate_abs | -0.0162 |
| pctl_percentile | -0.0165 |
| basis_24h | -0.0196 |
| mom_points | -0.0198 |
| top_ls_24h | -0.0252 |
| basis_chg_24h | -0.0254 |
| streak | -0.0448 |
| ppd | -0.0607 |

Filas de la ventana usadas: 163,530. Reproducir: `python -u scripts/signals_dataset.py --days 105` y `python -u scripts/ml_v3_signals.py`.
