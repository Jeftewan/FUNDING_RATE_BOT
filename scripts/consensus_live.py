#!/usr/bin/env python3
"""Validación en vivo del consenso — LOCAL, solo lectura.

Une lo que la UI mostró cada hora (market_feature_snapshots.model_prediction / consensus /
consensus_p, registrados desde 2026-11-26) con el funding LIQUIDADO después
(cache/binance/fr_binance.csv; correr antes `python scripts/binance_dataset.py`, incremental).
Éxito = APR neto realizado (misma regla y costos que scripts/consensus_study.py: salida al
tercer pago seguido ≤ 0, tope 30 d) ≥ APR neto predicho. Solo cuentan las horas cuyo label ya
cerró; una fila por par y settlement (la primera hora registrada antes del pago).

    python -u scripts/consensus_live.py            # DATABASE_URL en el entorno (.env)
"""
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))
import consensus_study as cst  # noqa: E402
import ml_binance as mb  # noqa: E402
import ml_v2  # noqa: E402


def load_shown() -> pd.DataFrame:
    import sqlalchemy as sa
    url = re.sub(r"^postgres(ql)?://", "postgresql+psycopg2://", os.environ["DATABASE_URL"])
    q = ("SELECT symbol, ts_hour, spot_source, model_prediction, rel_width, consensus, consensus_p, "
         "consensus_version FROM market_feature_snapshots WHERE exchange = 'Binance' "
         "AND model_prediction IS NOT NULL ORDER BY symbol, ts_hour")
    with sa.create_engine(url).connect() as c:
        return pd.read_sql(sa.text(q), c)


def main():
    sys.stdout.reconfigure(encoding="utf-8")
    s = load_shown()
    if s.empty:
        sys.exit("sin filas con model_prediction registradas todavía")
    s = s[s["model_prediction"] > 0].copy()
    fr = pd.read_csv(mb.DATA, usecols=["symbol", "rate", "interval_hours", "funding_ts"])
    lab = cst.k_labels(fr).dropna(subset=["fund_K"]).sort_values("funding_ts")
    # cada hora mostrada → el próximo settlement del par (el que cobraría al entrar)
    s["t_ms"] = s["ts_hour"].astype("int64") * 1000
    s = pd.merge_asof(s.sort_values("t_ms"), lab, left_on="t_ms", right_on="funding_ts", by="symbol",
                      direction="forward", tolerance=8 * 3_600_000).dropna(subset=["fund_K"])
    s = s.sort_values("ts_hour").drop_duplicates(["symbol", "funding_ts"])
    alpha = s["spot_source"].eq("alpha")
    cost = np.where(alpha, ml_v2.FEE_ALPHA + cst.ALPHA_PRICE_COST, ml_v2.FEE_CEX)
    s["y_real"] = cst.apr(s["fund_K"] - cost, s["hold_K"])
    s["success"] = (s["y_real"] >= s["model_prediction"]).astype(int)
    print(f"horas con label cerrado: {len(s):,} ({s['symbol'].nunique()} pares), "
          f"{pd.to_datetime(s['ts_hour'].min(), unit='s'):%Y-%m-%d} → "
          f"{pd.to_datetime(s['ts_hour'].max(), unit='s'):%Y-%m-%d}; éxito {s['success'].mean():.1%}")
    today = datetime.now(timezone.utc).strftime("%Y%m%d")
    L = [f"# Consenso en vivo — {today}", "", f"Filas: {len(s):,}; tasa de éxito {s['success'].mean():.1%}.", ""]
    has_p = s["consensus_p"].notna()
    if has_p.any():
        d = s[has_p]
        p, y = d["consensus_p"].to_numpy(float), d["success"].to_numpy(float)
        rel, res, unc, ece = cst.murphy(p, y)
        L += [f"P calibrada: n {len(d):,}, Brier {cst.brier(p, y):.4f}, ECE {ece:.3f}, resolución {res:.4f}", "",
              "| consenso | n | P media | % éxito | APR real mediano |", "| --- | --- | --- | --- | --- |"]
        for r in cst.tramo_table(p, y, d["y_real"].to_numpy(float)):
            L.append(f"| {r['tramo'] - 1}–{r['tramo']} | {r['n']} | {r['p_media']:.0%} | {r['exito']:.0%} | "
                     f"{r['apr_med']:.1f} |")
    d = s[s["consensus"].notna()]
    if len(d):
        L += ["", "Consenso mostrado (cualquier versión):", "", "| consenso | n | % éxito | APR real mediano |",
              "| --- | --- | --- | --- |"]
        for k, g in d.groupby(np.minimum(np.floor(d["consensus"]).astype(int), 9)):
            L.append(f"| {k}–{k + 1} | {len(g)} | {g['success'].mean():.0%} | {g['y_real'].median():.1f} |")
    rep = ROOT / "reports" / f"consensus_live_{today}.md"
    rep.write_text("\n".join(L), encoding="utf-8")
    print("\n".join(L))


if __name__ == "__main__":
    main()
