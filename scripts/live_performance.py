#!/usr/bin/env python3
"""Rendimiento real vs esperado — LOCAL, read-only sobre la DB de prod.

¿Por qué no se logran las rentabilidades esperadas? Separa tres causas:
  * modelo (ranking): lo predicho al entrar (`payments_json[auto_open].entry_model_prediction`)
    vs el funding realmente cobrado.
  * nivel de mercado: funding del universo (top-decil diario de `cache/binance/fr_binance.csv`)
    semana a semana — si el mercado se comprimió, todo rinde menos aunque el ranking acierte.
  * ejecución: PnL de precio (basis + slippage) y fees por trade, en APR sobre el nocional.

Rentabilidad sobre capital TOTAL (spot + margen; a 1x la mitad del capital cobra funding).
Uso:  python -u scripts/live_performance.py [--days 120]
Salida: reports/live_performance_YYYYMMDD.md
"""
import argparse
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

FR_CSV = ROOT / "cache" / "binance" / "fr_binance.csv"
MIN_CAPITAL = 50.0           # excluye posiciones de prueba
TOP_FRAC = 0.10


def load_db():
    from dotenv import load_dotenv
    from sqlalchemy import text
    load_dotenv(ROOT / ".env")
    from _scoring_data import get_engine
    with get_engine().connect() as c:
        h = pd.read_sql(text("SELECT id, user_id, symbol, exchange, mode, capital_used, exposure, leverage, "
                             "hours, earned, fees, net_earned, price_pnl, slippage_usd, reason, closed_at "
                             "FROM user_history"), c)
        p = pd.read_sql(text("SELECT id, user_id, symbol, exchange, status, entry_time, capital_used, exposure, "
                             "leverage, earned_real, entry_fees, entry_fees_real, exit_fees_est, realized_price_pnl, "
                             "spot_venue, source, closed_at, payments_json FROM user_positions"), c)
    return h, p


def _auto_open(payments):
    for x in payments or []:
        if isinstance(x, dict) and x.get("kind") == "auto_open":
            return x
    return {}


def link(h, p):
    """user_history → user_positions (símbolo + closed_at ±10 min)."""
    p = p.copy()
    p["closed_at"] = pd.to_datetime(p["closed_at"], utc=True)
    h = h.copy()
    h["closed_at"] = pd.to_datetime(h["closed_at"], utc=True)
    pos_id, used = [], set()
    for r in h.itertuples():
        c = p[(p["symbol"] == r.symbol) & (p["status"] == "closed") & ~p["id"].isin(used)]
        if len(c):
            d = (c["closed_at"] - r.closed_at).abs()
            k = d.idxmin()
            if d[k] <= pd.Timedelta(minutes=10):
                used.add(int(c.at[k, "id"]))
                pos_id.append(int(c.at[k, "id"]))
                continue
        pos_id.append(None)
    h["position_id"] = pos_id
    return h, p


def trades_table(h, p):
    h, p = link(h, p)
    pmap = p.set_index("id")
    rows = []
    for r in h.itertuples():
        if (r.capital_used or 0) < MIN_CAPITAL or not r.hours:
            continue
        ao = _auto_open(pmap.at[r.position_id, "payments_json"]) if r.position_id else {}
        days = r.hours / 24
        notional = r.exposure or r.capital_used / 2
        price = r.price_pnl if r.price_pnl is not None and not pd.isna(r.price_pnl) else 0.0
        ann = 365 / max(days, 1 / 24) * 100
        rows.append({
            "id": r.id, "symbol": r.symbol, "closed": r.closed_at, "opened": r.closed_at - timedelta(days=days),
            "days": days, "capital": r.capital_used, "notional": notional,
            "funding": r.earned, "price": price, "fees": r.fees, "net": r.net_earned,
            "pred_napr": ao.get("entry_model_prediction"),
            "funding_apr_n": r.earned / notional * ann,
            "price_apr_n": price / notional * ann,
            "fees_apr_n": -r.fees / notional * ann,
            "net_apr_n": r.net_earned / notional * ann,
            "roc_cap_pct": r.net_earned / r.capital_used * 100,
            "apr_cap": r.net_earned / r.capital_used * ann,
            "venue": pmap.at[r.position_id, "spot_venue"] if r.position_id else "",
        })
    return pd.DataFrame(rows).sort_values("opened")


def market_level(days: int):
    """Funding diario del top-decil del universo Binance (tasas liquidadas), por semana."""
    if not FR_CSV.exists():
        return None, None
    fr = pd.read_csv(FR_CSV, usecols=["symbol", "rate", "funding_ts"])
    t0 = (datetime.now(timezone.utc) - timedelta(days=days)).timestamp() * 1000
    fr = fr[fr["funding_ts"] >= t0]
    fr["day"] = pd.to_datetime(fr["funding_ts"], unit="ms", utc=True).dt.floor("D")
    d = fr.groupby(["day", "symbol"])["rate"].sum().reset_index()
    def top(g):
        n = max(1, int(len(g) * TOP_FRAC))
        return pd.Series({"top_decile_daily_pct": g["rate"].nlargest(n).mean() * 100,
                          "n_ge_base": int((g["rate"] >= 0.0003).sum()),
                          "median_daily_pct": g["rate"].median() * 100})
    daily = d.groupby("day").apply(top, include_groups=False)
    weekly = daily.resample("W-SUN").mean()
    return weekly, fr["day"].max()


def md(df, cols, fmt=None):
    fmt = fmt or {}
    L = ["| " + " | ".join(cols) + " |", "| " + " | ".join("---" for _ in cols) + " |"]
    for _, r in df.iterrows():
        L.append("| " + " | ".join(fmt.get(c, lambda v: f"{v:.2f}" if isinstance(v, (float, np.floating)) else str(v))(r[c])
                                   for c in cols) + " |")
    return L


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=180, help="ventana del nivel de mercado")
    a = ap.parse_args()
    sys.stdout.reconfigure(encoding="utf-8")
    h, p = load_db()
    t = trades_table(h, p)
    today = datetime.now(timezone.utc)
    L = [f"# Rendimiento real vs esperado — {today:%Y-%m-%d}", "",
         f"Trades cerrados con capital ≥ {MIN_CAPITAL:.0f} USD: {len(t)}. APR `_n` = sobre el nocional "
         "(lo que predice el modelo); `apr_cap` = sobre el capital total (spot + margen).", ""]

    # 1. Por mes de cierre
    t["month"] = t["closed"].dt.strftime("%Y-%m")
    g = t.groupby("month").apply(lambda x: pd.Series({
        "n": len(x), "capital_dias": (x["capital"] * x["days"]).sum(),
        "net": x["net"].sum(), "funding": x["funding"].sum(), "price": x["price"].sum(), "fees": x["fees"].sum(),
        "win": (x["net"] > 0).mean() * 100}), include_groups=False).reset_index()
    g["apr_cap"] = g["net"] / g["capital_dias"] * 365 * 100
    g["funding_apr_cap"] = g["funding"] / g["capital_dias"] * 365 * 100
    g["price_apr_cap"] = g["price"] / g["capital_dias"] * 365 * 100
    g["fees_apr_cap"] = -g["fees"] / g["capital_dias"] * 365 * 100
    L += ["## 1. Por mes de cierre (APR ponderado por capital × días, sobre capital total)", ""]
    L += md(g, ["month", "n", "net", "funding", "price", "fees", "win", "apr_cap", "funding_apr_cap",
                "price_apr_cap", "fees_apr_cap"], {"n": lambda v: f"{int(v)}", "win": lambda v: f"{v:.0f}%"})

    # 2. Predicho vs realizado
    tp = t.dropna(subset=["pred_napr"]).copy()
    L += ["", f"## 2. Predicho al entrar vs realizado (n = {len(tp)} trades auto con predicción)", ""]
    if len(tp):
        tp["pred_napr"] = tp["pred_napr"].astype(float)
        L += md(tp, ["symbol", "opened", "days", "capital", "pred_napr", "funding_apr_n", "price_apr_n",
                     "fees_apr_n", "net_apr_n", "apr_cap"],
                {"opened": lambda v: f"{v:%Y-%m-%d}", "days": lambda v: f"{v:.1f}", "capital": lambda v: f"{v:.0f}"})
        # PnL de precio > 10 % del capital = evento atípico (p. ej. JCT #46, recompra externa).
        out = tp["price"].abs() > 0.10 * tp["capital"]
        core = tp[~out]
        w = core["notional"] * core["days"]
        wm = lambda c: float((core[c] * w).sum() / w.sum())
        rho = tp[["pred_napr", "funding_apr_n"]].corr(method="spearman").iloc[0, 1] if len(tp) >= 4 else float("nan")
        L += ["", f"Sin atípicos ({', '.join(tp.loc[out, 'symbol']) or 'ninguno'}), ponderado por nocional × días "
                  f"(n = {len(core)}): predicho {wm('pred_napr'):.1f} % · funding {wm('funding_apr_n'):.1f} % · "
                  f"precio {wm('price_apr_n'):.1f} % · fees {wm('fees_apr_n'):.1f} % · neto {wm('net_apr_n'):.1f} % "
                  f"(sobre nocional) · neto sobre capital {float((core['net'].sum()) / (core['capital'] * core['days']).sum() * 36500):.1f} %.",
              f"Spearman predicho ↔ funding cobrado: {rho:.2f}. Mediana por trade: predicho "
              f"{tp['pred_napr'].median():.1f} % · funding {tp['funding_apr_n'].median():.1f} % · precio "
              f"{tp['price_apr_n'].median():.1f} % · neto {tp['net_apr_n'].median():.1f} %.",
              f"Brecha predicho − (funding − fees): {wm('pred_napr') - wm('funding_apr_n') - wm('fees_apr_n'):.1f} pp "
              f"(modelo/nivel); brecha de ejecución (PnL de precio): {-wm('price_apr_n'):.1f} pp."]

    # 2b. Antes / después de la espera de basis (PR #120, 2026-11-11) y duración del hold.
    #     profit_study_20261117: salir a los 3 d deja +0.07 % por operación vs +1.65 % con la regla.
    cut = pd.Timestamp("2026-11-11", tz="UTC")
    t["periodo"] = np.where(t["opened"] >= cut, "con espera de basis", "antes (ejecutar ya)")
    t["hold_band"] = pd.cut(t["days"], [0, 3, 7, 14, 1e9], labels=["< 3 d", "3–7 d", "7–14 d", "≥ 14 d"])
    L += ["", "## 2b. Método de entrada y duración del hold (sobre capital total)", ""]
    for col in ("periodo", "hold_band"):
        gg = t.groupby(col, observed=True).apply(lambda x: pd.Series({
            "n": len(x), "dias_med": x["days"].median(), "net": x["net"].sum(), "price": x["price"].sum(),
            "apr_cap": x["net"].sum() / (x["capital"] * x["days"]).sum() * 36500}), include_groups=False).reset_index()
        L += md(gg, [col, "n", "dias_med", "net", "price", "apr_cap"], {"n": lambda v: f"{int(v)}"}) + [""]

    # 3. Nivel de mercado
    weekly, last = market_level(a.days)
    L += ["", f"## 3. Nivel del mercado (Binance USDⓈ-M, tasas liquidadas, últimos {a.days} d; datos hasta {last:%Y-%m-%d})"
          if weekly is not None else "## 3. Nivel del mercado — sin dataset", ""]
    if weekly is not None:
        wk = weekly.reset_index().rename(columns={"day": "semana"})
        L += md(wk, ["semana", "top_decile_daily_pct", "median_daily_pct", "n_ge_base"],
                {"semana": lambda v: f"{v:%Y-%m-%d}", "top_decile_daily_pct": lambda v: f"{v:.4f}",
                 "median_daily_pct": lambda v: f"{v:.4f}", "n_ge_base": lambda v: f"{v:.0f}"})
        first, lastw = weekly.iloc[:4].mean(), weekly.iloc[-4:].mean()
        L += ["", f"Top-decil diario: {first['top_decile_daily_pct']:.4f} % (primeras 4 semanas) → "
                  f"{lastw['top_decile_daily_pct']:.4f} % (últimas 4); pares ≥ 0.03 %/día: "
                  f"{first['n_ge_base']:.0f} → {lastw['n_ge_base']:.0f}."]

    path = ROOT / "reports" / f"live_performance_{today:%Y%m%d}.md"
    path.write_text("\n".join(L) + "\n", encoding="utf-8")
    print(f"Reporte: {path}")


if __name__ == "__main__":
    main()
