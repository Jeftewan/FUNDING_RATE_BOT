"""profit_diagnosis.py — ¿el scoring premia ganancia neta a 7–15 días?

Etapa 1 del plan de scoring predictivo. Read-only sobre el feature CSV
(no DB, no producción). Responde con números:

  1. ¿Cuán rentables son las oportunidades NETO de fees, por horizonte? (sensibilidad al fee)
  2. ¿Qué drivers crudos predicen la ganancia neta a ~8 días vs cuánto peso les da v10.6?
     → expone si estabilidad/consistencia están SOBRE-premiadas vs su poder predictivo.
  3. ¿Corto-alto (yield alto) rinde más neto que largo-bajo (estable) en 8d y 24d?
  4. ¿El score v10.6 rankea bien la ganancia neta (monotonicidad por decil)?

Uso:
  python scripts/profit_diagnosis.py
  python scripts/profit_diagnosis.py --features reports/backtest_20260615_features.csv \
         --primary-fee 0.30 --fees 0.05,0.10,0.20,0.30
"""
from __future__ import annotations

import argparse
from datetime import date
from pathlib import Path

import pandas as pd

from _profit_common import (
    DEFAULT_FEATURES, DEFAULT_FEES, DEFAULT_FEE_PCT, DIMENSION_DRIVERS,
    HORIZON_DAYS, PRIMARY_HORIZON, V106_BASE, V106_WEIGHTS,
    decile_table, load_features, md_table, net, pearson, realized,
    sharpe_like, spearman,
)

HORIZONS = [8, 24, 72]


def section_net_overview(df: pd.DataFrame, fees: list[float], raw: bool) -> str:
    """% rentable y net medio por horizonte × fee (sensibilidad al fee)."""
    rows = []
    for h in HORIZONS:
        r = realized(df, h, raw=raw)
        row = {"horizonte": f"{h}i (~{HORIZON_DAYS[h]:.0f}d)",
               "bruto_medio": round(r.mean(), 4)}
        for f in fees:
            n = r - f
            row[f"%rent@{f:.2f}"] = round(100 * (n > 0).mean(), 1)
        rows.append(row)
    cols = ["horizonte", "bruto_medio"] + [f"%rent@{f:.2f}" for f in fees]
    return md_table(pd.DataFrame(rows), cols)


def section_driver_power(df: pd.DataFrame, fee: float, raw: bool) -> str:
    """Driver crudo → Spearman/Pearson vs net a horizontes 8/24/72, con peso v10.6."""
    rows = []
    for dim, drivers in DIMENSION_DRIVERS.items():
        w = V106_WEIGHTS[dim]
        for col, direction in drivers:
            if col not in df.columns:
                continue
            drv = df[col] * direction  # orientar para que +corr = "ayuda a ganar"
            r = {
                "dimension": dim,
                "peso_v106": w,
                "driver": f"{col}{'(inv)' if direction < 0 else ''}",
                "sp_net8": round(spearman(drv, net(df, 8, fee, raw=raw)), 3),
                "sp_net24": round(spearman(drv, net(df, 24, fee, raw=raw)), 3),
                "sp_net72": round(spearman(drv, net(df, 72, fee, raw=raw)), 3),
                "pe_net24": round(pearson(drv, net(df, 24, fee, raw=raw)), 3),
            }
            rows.append(r)
    cols = ["dimension", "peso_v106", "driver",
            "sp_net8", "sp_net24", "sp_net72", "pe_net24"]
    return md_table(pd.DataFrame(rows), cols)


def section_weight_gap(df: pd.DataFrame, fee: float, raw: bool) -> str:
    """Brecha peso↔poder: peso v106 (% del base) vs mejor |Spearman| de sus drivers."""
    tgt = net(df, PRIMARY_HORIZON, fee, raw=raw)
    rows = []
    for dim, drivers in DIMENSION_DRIVERS.items():
        w = V106_WEIGHTS[dim]
        best = 0.0
        best_drv = ""
        for col, direction in drivers:
            if col not in df.columns:
                continue
            sp = spearman(df[col] * direction, tgt)
            if pd.notna(sp) and abs(sp) > abs(best):
                best, best_drv = sp, col
        rows.append({
            "dimension": dim,
            "peso_v106": w,
            "peso_%": round(100 * w / V106_BASE, 1),
            "mejor_driver": best_drv,
            "sp_vs_net24": round(best, 3),
        })
    t = pd.DataFrame(rows).sort_values("peso_v106", ascending=False)
    # flag: alto peso + bajo poder = sobre-premiado
    t["sesgo"] = t.apply(
        lambda r: "SOBRE-premiado" if (r["peso_%"] >= 20 and abs(r["sp_vs_net24"]) < 0.10)
        else ("SUB-premiado" if (r["peso_%"] < 20 and abs(r["sp_vs_net24"]) >= 0.15)
              else "—"), axis=1)
    cols = ["dimension", "peso_v106", "peso_%", "mejor_driver", "sp_vs_net24", "sesgo"]
    return md_table(t, cols)


def section_short_vs_long(df: pd.DataFrame, fee: float, raw: bool) -> str:
    """Corto-alto (yield alto) vs largo-bajo (estable) neto de fees a 24i y 72i."""
    sa = df["settlement_avg"]
    hi_yield = sa >= sa.quantile(0.80)
    stable_lo = (df["streak"] >= df["streak"].quantile(0.70)) & \
                (df["pct_positive"] >= 70) & (sa <= sa.quantile(0.50))
    rows = []
    for label, mask in [("corto-alto (yield top 20%)", hi_yield),
                        ("largo-bajo (estable, yield bajo)", stable_lo)]:
        sub = df[mask]
        for h in (24, 72):
            n = net(sub, h, fee, raw=raw)
            rows.append({
                "grupo": label,
                "horizonte": f"{h}i (~{HORIZON_DAYS[h]:.0f}d)",
                "n": len(sub),
                "mean_net": round(n.mean(), 4),
                "median_net": round(n.median(), 4),
                "%rent": round(100 * (n > 0).mean(), 1),
                "sharpe": round(sharpe_like(n), 3),
            })
    cols = ["grupo", "horizonte", "n", "mean_net", "median_net", "%rent", "sharpe"]
    return md_table(pd.DataFrame(rows), cols)


def section_score_monotonicity(df: pd.DataFrame, fee: float, raw: bool):
    """Decil de score v10.6 → net a ~8d. Devuelve (markdown, rho_monotonicidad)."""
    t = decile_table(df, "score", fee, PRIMARY_HORIZON, raw=raw)
    rho = spearman(t["decile"].astype(float), t["mean_net"])
    cols = ["decile", "n", "score_lo", "score_hi",
            "mean_net", "median_net", "pct_profit", "sharpe"]
    return md_table(t, cols), rho, t


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--features", default=DEFAULT_FEATURES)
    ap.add_argument("--primary-fee", type=float, default=DEFAULT_FEE_PCT)
    ap.add_argument("--fees", default=",".join(f"{f:.2f}" for f in DEFAULT_FEES))
    ap.add_argument("--universe", default="positive", choices=["positive", "all"],
                    help="positive = solo funding>0 (enterable sin ambiguedad); "
                         "all = todas con retorno direccion-ajustado")
    ap.add_argument("--raw", action="store_true",
                    help="(universe=all) usar suma cruda con signo, no direccion-ajustada")
    args = ap.parse_args()
    fees = [float(x) for x in args.fees.split(",")]
    fee = args.primary_fee

    print(f"Cargando {args.features} (universe={args.universe}) ...")
    df = load_features(args.features, universe=args.universe)
    print(f"  {len(df):,} filas | fee primario {fee:.2f}% | "
          f"net direccion-{'cruda(raw)' if args.raw else 'ajustada'}")

    s_overview = section_net_overview(df, fees, args.raw)
    s_drivers = section_driver_power(df, fee, args.raw)
    s_gap = section_weight_gap(df, fee, args.raw)
    s_svl = section_short_vs_long(df, fee, args.raw)
    s_mono, rho, mono_t = section_score_monotonicity(df, fee, args.raw)

    # baseline: el score total vs net
    sp_score = spearman(df["score"], net(df, PRIMARY_HORIZON, fee, raw=args.raw))

    out = f"""# Diagnóstico de scoring vs ganancia neta — {date.today():%Y-%m-%d}

**Fuente:** `{args.features}` ({len(df):,} filas) | **Universo:** `{args.universe}` {'(solo funding>0, enterable sin ambiguedad de direccion — vale para todo modo)' if args.universe == 'positive' else '(todas, retorno direccion-ajustado; sobre-acredita funding negativo en spot_perp)'}
**Score:** v10.6 (adoptado 2026-06-11; el CSV es de 2026-06-15 → score actual).
**Net:** `realizado − fee`, realizado direccion-{'cruda (raw)' if args.raw else 'ajustada (sign(rate)×fwd)'}.
**Fee primario:** {fee:.2f}% round-trip (= 0.003 fracción). Horizonte primario: {PRIMARY_HORIZON}i (~8d, dentro de 7–15d).

> Recordatorio de unidades: `fwd_total` y `net` están en **puntos porcentuales**.
> Un net de 0.10 = 0.10% de retorno sobre el notional en la ventana.

---

## 1. Rentabilidad neta por horizonte y sensibilidad al fee

¿Qué fracción de oportunidades es rentable neto de fees, según el fee asumido?

{s_overview}

**Lectura:** si `%rent` sube fuerte con el horizonte, el fee se amortiza con holds más
largos → favorece "ganancia baja sostenida". Si baja el fee, la ventana corta 7–15d
se vuelve viable. Aquí está el cruce que decide corto-alto vs largo-bajo.

---

## 2. Poder predictivo de cada driver vs net a 8/24/72 intervalos

Spearman de cada **driver crudo** (independiente de la versión del scoring) contra el net.
`(inv)` = driver invertido para que correlación positiva signifique "ayuda a ganar".

{s_drivers}

**Tesis a validar:** los drivers de **consistencia** (streak, pct_positive, peso 46) y
**estabilidad** (cv, min_ratio, peso 21) deberían rankear BAJO en `sp_net24`, mientras
**yield** (settlement_avg, peso 17) rankea ALTO. Eso probaría el sobre-premio.

---

## 3. Brecha peso↔poder predictivo (horizonte ~8d, fee {fee:.2f}%)

Compara cuánto PESO da v10.6 a cada dimensión vs cuánto PREDICE realmente la ganancia.

{s_gap}

`SOBRE-premiado` = ≥20% del score pero |Spearman| < 0.10 vs net a 8d.
`SUB-premiado` = <20% del score pero |Spearman| ≥ 0.15.

---

## 4. Corto-alto vs largo-bajo (neto de fees)

Responde directo a tu pregunta: ¿conviene yield alto en ventana corta, o estable y bajo
sostenido un mes?

{s_svl}

---

## 5. ¿El score v10.6 rankea bien la ganancia neta? (monotonicidad por decil)

Decil 1 = peor score, decil 10 = mejor. Si el score predice ganancia, `mean_net` debe
crecer con el decil.

{s_mono}

- **Spearman(decil, mean_net):** `{rho:.3f}` (1.0 = monotonicidad perfecta).
- **Spearman(score, net_24) a nivel fila:** `{sp_score:.3f}`.
- **Net del decil 10 vs decil 1:** `{mono_t.iloc[-1]['mean_net']:+.4f}` vs `{mono_t.iloc[0]['mean_net']:+.4f}`.

---

## Veredicto rápido

- Monotonicidad score↔net a 8d: **{'BUENA' if rho >= 0.7 else ('DÉBIL' if rho >= 0.3 else 'POBRE')}** (ρ={rho:.2f}).
- Correlación fila score↔net: **{sp_score:.3f}** (referencia: el optimizer apunta a maximizar esto).
- Revisar la tabla §3: toda dimensión marcada `SOBRE-premiado` es candidata a bajar peso
  en la Etapa 2 (re-objetivo Sharpe), y toda `SUB-premiado` a subir.
"""

    outpath = Path("reports") / f"profit_diagnosis_{date.today():%Y%m%d}.md"
    outpath.write_text(out, encoding="utf-8")
    print(f"\nReporte escrito: {outpath}\n")
    print(f"  Monotonicidad score-net (rho por decil): {rho:.3f}")
    print(f"  Spearman fila score-net_24:              {sp_score:.3f}")
    print(f"  Net decil10 vs decil1: {mono_t.iloc[-1]['mean_net']:+.4f} "
          f"vs {mono_t.iloc[0]['mean_net']:+.4f}")


if __name__ == "__main__":
    main()
