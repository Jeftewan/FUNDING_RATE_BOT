# ML experiments — variantes del modelo de scoring — 2026-09-13 18:49

**Datos:** 1,438,228 filas, 2026-06-16 → 2026-09-12. Walk-forward idéntico a `ml_train.py`
(`--min-train-days 45`, `--test-days 7`).
Features fijos (14), target `net_apr`. Control = `A_gbr_control` (GBR de prod).

## 1. Walk-forward por candidato (medias sobre folds)

| candidato | descripción | IC ML | uplift vs heur | σ uplift | folds + | net_apr top10 ML | Δtop10 vs heur | Δtop1% vs heur | ΔIC vs control | Δtop10 vs control | fit s (total) |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| B1_hgb_hl30 | HistGB 31 hojas + recencia 30d | 0.752 | +0.127 | 0.03 | 6/6 | 19.017 | +4.8 | +10.6 | +0.015 | +0.6 | 71.9 |
| B2_hgb_deep_hl30 | HistGB 63 hojas + recencia 30d | 0.751 | +0.127 | 0.031 | 6/6 | 19.033 | +4.8 | +9.3 | +0.015 | +0.6 | 167.8 |
| B1_hgb | HistGB 31 hojas, todo el train | 0.75 | +0.126 | 0.03 | 6/6 | 19.167 | +4.9 | +10.8 | +0.014 | +0.8 | 88.6 |
| B2_hgb_deep | HistGB 63 hojas 600it l2=1 | 0.75 | +0.126 | 0.031 | 6/6 | 19.033 | +4.8 | +9.2 | +0.014 | +0.6 | 184.2 |
| B3_hgb_small | HistGB 15 hojas lr0.1 | 0.75 | +0.125 | 0.03 | 6/6 | 19.25 | +5.0 | +10.6 | +0.013 | +0.8 | 66.0 |
| C2_gbr_huber | GBR loss huber | 0.745 | +0.121 | 0.028 | 6/6 | 18.617 | +4.3 | +10.5 | +0.009 | +0.2 | 442.7 |
| B4_hgb_abs | HistGB loss absolute_error | 0.744 | +0.120 | 0.03 | 6/6 | 18.85 | +4.6 | +10.5 | +0.008 | +0.5 | 164.4 |
| C1_gbr_d4 | GBR depth4 600it lr0.03 | 0.744 | +0.120 | 0.028 | 6/6 | 18.567 | +4.3 | +10.6 | +0.008 | +0.2 | 1049.1 |
| A_gbr_hl30 | GBR prod + recencia 30d | 0.74 | +0.116 | 0.027 | 6/6 | 18.433 | +4.2 | +10.4 | +0.004 | +0.0 | 479.4 |
| A_gbr_control | GBR prod (control) | 0.736 | +0.112 | 0.029 | 6/6 | 18.4 | +4.1 | +10.8 | +0.000 | +0.0 | 659.1 |

Criterio de adopción: superar al control en uplift IC **y** en Δtop10, con los
mismos folds positivos y σ ≤ 0.05; artefacto razonable (<5 MB).

## 2. Calibración in-sample vs fuera de muestra (último bloque de test)

| candidato | n_test | % score≥70 (esperado 30) | % score≥85 (esperado 15) | score mediano (esperado 50) | tamaño MB |
| --- | --- | --- | --- | --- | --- |
| A_gbr_control | 46020 | 54.7 | 25.9 | 73.0 | 0.41 |
| B1_hgb | 46020 | 54.5 | 24.4 | 74.0 | 1.1 |
| B2_hgb_deep | 46020 | 54.4 | 23.9 | 74.0 | 4.33 |
