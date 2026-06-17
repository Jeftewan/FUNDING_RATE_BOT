"""profit_simulation.py — simulación de hold fee-aware por decil de score.

Etapa 1 del plan. Read-only sobre el feature CSV. Simula: entrar en una
oportunidad, mantener una ventana fija W, salir, y medir el retorno NETO de fees.
Un hold con entrada/salida única equivale a `realizado_W − fee`, así que reusa
los retornos forward existentes (no reconstruye nada).

Ventanas (intervalos de 8h):
  8i  ≈ 2.7 días
  24i ≈ 8 días   (PRIMARIO — dentro de la ventana objetivo 7–15d)
  72i ≈ 24 días  (comparador "ganancia baja sostenida un mes")

Por decil de score y por ventana reporta: N, mean/median net, % rentable y
Sharpe-like (mean/std, ganancia ajustada a riesgo — alineado con el objetivo elegido).

Uso:
  python scripts/profit_simulation.py
  python scripts/profit_simulation.py --fees 0.10,0.20,0.30
"""
from __future__ import annotations

import argparse
from datetime import date
from pathlib import Path

import pandas as pd

from _profit_common import (
    DEFAULT_FEATURES, DEFAULT_FEES, HORIZON_DAYS, PRIMARY_HORIZON,
    decile_table, load_features, md_table, net, sharpe_like, spearman,
)

WINDOWS = [8, 24, 72]


def headline(df: pd.DataFrame, fee: float, h: int, raw: bool) -> dict:
    t = decile_table(df, "score", fee, h, raw=raw)
    rho = spearman(t["decile"].astype(float), t["mean_net"])
    return {
        "ventana": f"{h}i (~{HORIZON_DAYS[h]:.0f}d)",
        "fee": f"{fee:.2f}",
        "rho_monotonia": round(rho, 3),
        "net_d10": t.iloc[-1]["mean_net"],
        "net_d1": t.iloc[0]["mean_net"],
        "spread_d10_d1": round(t.iloc[-1]["mean_net"] - t.iloc[0]["mean_net"], 4),
        "sharpe_d10": t.iloc[-1]["sharpe"],
        "%rent_d10": t.iloc[-1]["pct_profit"],
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--features", default=DEFAULT_FEATURES)
    ap.add_argument("--fees", default=",".join(f"{f:.2f}" for f in DEFAULT_FEES))
    ap.add_argument("--universe", default="positive", choices=["positive", "all"])
    ap.add_argument("--raw", action="store_true")
    args = ap.parse_args()
    fees = [float(x) for x in args.fees.split(",")]

    print(f"Cargando {args.features} (universe={args.universe}) ...")
    df = load_features(args.features, universe=args.universe)
    print(f"  {len(df):,} filas | net direccion-{'cruda(raw)' if args.raw else 'ajustada'}")

    # Resumen headline: para cada (ventana, fee) el spread top-bottom + monotonicidad
    head_rows = [headline(df, f, h, args.raw) for h in WINDOWS for f in fees]
    head_df = pd.DataFrame(head_rows)
    head_md = md_table(head_df, ["ventana", "fee", "rho_monotonia",
                                 "net_d1", "net_d10", "spread_d10_d1",
                                 "sharpe_d10", "%rent_d10"])

    # Tablas de decil detalladas al fee primario por ventana
    detail_blocks = []
    primary_fee = fees[-1]  # el más conservador de la lista (ej. 0.30)
    for h in WINDOWS:
        t = decile_table(df, "score", primary_fee, h, raw=args.raw)
        block = (f"### Ventana {h}i (~{HORIZON_DAYS[h]:.0f}d) — fee {primary_fee:.2f}%\n\n"
                 + md_table(t, ["decile", "n", "score_lo", "score_hi",
                                "mean_net", "median_net", "pct_profit", "sharpe"]))
        detail_blocks.append(block)
        # exporta CSV por ventana
        t.to_csv(Path("reports") /
                 f"profit_sim_{date.today():%Y%m%d}_w{h}.csv", index=False)

    # Comparación corto vs largo sobre el MISMO decil top (score >= p90)
    top = df[df["score"] >= df["score"].quantile(0.90)]
    cmp_rows = []
    for h in (24, 72):
        for f in fees:
            n = net(top, h, f, raw=args.raw)
            cmp_rows.append({
                "ventana": f"{h}i (~{HORIZON_DAYS[h]:.0f}d)",
                "fee": f"{f:.2f}",
                "mean_net": round(n.mean(), 4),
                "%rent": round(100 * (n > 0).mean(), 1),
                "sharpe": round(sharpe_like(n), 3),
            })
    cmp_md = md_table(pd.DataFrame(cmp_rows),
                      ["ventana", "fee", "mean_net", "%rent", "sharpe"])

    out = f"""# Simulación de hold por decil de score — {date.today():%Y-%m-%d}

**Fuente:** `{args.features}` ({len(df):,} filas) | **Universo:** `{args.universe}` | Score v10.6 | net direccion-{'cruda' if args.raw else 'ajustada'}.
Hold = entrar, mantener W intervalos, salir. Net = `realizado_W − fee` (en puntos %).

---

## 1. Headline: spread top-bottom y monotonicidad por (ventana × fee)

`net_d10` = net medio del mejor decil de score; `net_d1` = el peor.
`rho_monotonia` = Spearman(decil, net medio): 1.0 = el score ordena perfecto la ganancia.

{head_md}

**Cómo leerlo:**
- Si `rho_monotonia` es alto y `spread_d10_d1 > 0`, el score ya separa ganadores de perdedores.
- Compará `sharpe_d10` entre ventanas: dónde el mejor decil tiene mejor ganancia/volatilidad.
- Buscá el cruce de fee donde el decil top deja de ser rentable en la ventana corta (24i).

---

## 2. Detalle por decil (fee {primary_fee:.2f}%)

{chr(10).join(detail_blocks)}

CSV por ventana: `reports/profit_sim_{date.today():%Y%m%d}_w{{8,24,72}}.csv`.

---

## 3. Mejor decil (score ≥ p90): corto vs largo a distintos fees

¿El top del score rinde más neto en ~8d o en ~24d, y a partir de qué fee se invierte?

{cmp_md}

---

## Para decidir Etapa 2

- Si en la ventana 24i (~8d) el decil top tiene `sharpe` y `%rent` pobres pero en 72i
  mejora mucho → el scoring actual sirve para holds largos, no para 7–15d. El re-objetivo
  Sharpe debería re-pesar hacia drivers que SÍ predicen net en 24i (ver §2/§3 del diagnóstico).
- Si el decil top ya es el más rentable y monótono en 24i → el problema no es el ranking
  sino la SELECCIÓN (umbral de score / fees reales). Ajustar threshold, no pesos.
"""

    outpath = Path("reports") / f"profit_simulation_{date.today():%Y%m%d}.md"
    outpath.write_text(out, encoding="utf-8")
    print(f"\nReporte escrito: {outpath}")
    print("\nHeadline (ventana | fee | rho | net_d1 | net_d10 | spread | sharpe_d10 | %rent_d10):")
    for r in head_rows:
        print(f"  {r['ventana']:12s} fee={r['fee']} rho={r['rho_monotonia']:+.2f} "
              f"d1={r['net_d1']:+.3f} d10={r['net_d10']:+.3f} "
              f"spread={r['spread_d10_d1']:+.3f} sharpe={r['sharpe_d10']} "
              f"%rent={r['%rent_d10']}")


if __name__ == "__main__":
    main()
