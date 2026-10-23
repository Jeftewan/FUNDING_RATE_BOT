#!/usr/bin/env python3
"""Entrenamiento y validación del modelo con el historial de Binance — LOCAL.

Fuente: cache/binance/fr_binance.csv (scripts/binance_dataset.py): tasas
LIQUIDADAS desde 2024, con deslistados, sin huecos de recolección.

Qué cambia frente a scripts/ml_train.py (snapshots de la DB):
  * Paridad exacta con prod (feature_spec "settled_v1"): ventana = 30
    liquidaciones ANTERIORES a la última y "tasa actual" = última liquidada.
    Prod lee ambas del mismo endpoint (/fapi/v1/fundingRate) → mismo número.
  * Label realista de horizonte fijo ("real14"): entrar tras la liquidación i,
    cobrar i+1… y salir DESPUÉS de pagar el primer funding ≤ 0 (o a los 14 d);
    neto = Σ tasas − 0.30 % (round-trip); APR = neto / 14 d (el capital
    liberado antes queda ocioso → no premia holds cortos).
  * Evaluación solo sobre filas ELEGIBLES en prod (spot CEX, volumen ≥ 100 k,
    tasa diaria ≥ base), IC por día, walk-forward mensual purgado (15 d) y
    hold-out intocado de los últimos 30 días.
  * Backtest de cartera con la política del bot (5 slots, 5.2 k USD, 1x):
    modelo vs heurístico v11.0 vs "top por settlement_avg".

Variantes (todas registradas en el reporte para no esconder la selección):
  E0  14 features actuales, entrenado en filas elegibles.
  E0a 14 features, entrenado en TODAS las filas.
  E1  E0 + régimen de mercado (mediana transversal de la tasa diaria y % de
      pares con prima en la misma hora).

Uso:
    python -u scripts/ml_binance.py                 # experimentos + reporte
    python -u scripts/ml_binance.py --export E0     # además exporta el .joblib
"""
import argparse
import json
import math
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

from analysis.indicators import compute_all_indicators            # noqa: E402
from analysis.ml_features import (FEATURE_NAMES, LOOKBACK, build_feature_vector,  # noqa: E402
                                  fee_drag_deterministic, window_stats)
from analysis.scoring import opportunity_score                    # noqa: E402
from _scoring_data import split_on_gaps                           # noqa: E402

DATA = ROOT / "cache" / "binance" / "fr_binance.csv"
FEAT_CACHE = ROOT / "cache" / "binance" / "features_v1.pkl"
REPORT_DIR = ROOT / "reports"
MODELS_DIR = ROOT / "models"

FEE = 0.003                 # round-trip sobre el nocional (spot 0.10%×2 + perp 0.05%×2)
HORIZON_DAYS = 14
BASE_DAILY = 0.0003         # tasa base Binance (0.01 %/8h) → piso del scan
MIN_VOLUME = 100_000
PURGE_DAYS = 15             # label mira 14 d + 1 d de embargo
HOLDOUT_DAYS = 30
WF_MONTHS = 12
CAPITAL, SLOTS = 5200.0, 5
MODEL_PARAMS = dict(max_iter=300, learning_rate=0.05, max_leaf_nodes=31,
                    min_samples_leaf=200, l2_regularization=0.0,
                    early_stopping=False, random_state=42)
REGIME = ["regime_median_daily", "regime_frac_premium"]


# ── Features + labels ────────────────────────────────────────────────────────
def build_rows(fr: pd.DataFrame) -> pd.DataFrame:
    fr = fr.sort_values(["symbol", "funding_ts"])
    rows = []
    groups = fr.groupby(["symbol", "exchange"])
    t0 = time.time()
    for k, ((sym, ex), g) in enumerate(groups, 1):
        if k % 100 == 0:
            print(f"  {k}/{len(groups)} pares ({time.time() - t0:.0f}s)", flush=True)
        rates = g["rate"].to_numpy(float)
        ihs = g["interval_hours"].to_numpy(float)
        vols = g["volume_24h"].to_numpy(float)
        spot = g["has_spot"].to_numpy(int)
        ts = g["funding_ts"].to_numpy(np.int64)
        n = len(g)
        for i in range(LOOKBACK, n):
            ih = max(1.0, ihs[i])
            ppd = 24.0 / ih
            cur = rates[i]
            hist = rates[i - LOOKBACK:i].tolist()
            ws = window_stats(hist)
            if ws is None:
                continue
            # Label realista: cobra i+1… y sale tras PAGAR el primer ≤ 0, tope 14 d.
            max_int = int(round(HORIZON_DAYS * 24 / ih))
            cumul, held, exited = 0.0, 0, False
            for j in range(1, max_int + 1):
                if i + j >= n:
                    break
                r = rates[i + j]
                cumul += r
                held += 1
                if r <= 0:
                    exited = True
                    break
            if not exited and held < max_int:
                continue                      # label incompleto (fin de la serie)
            net = cumul - FEE
            # Oráculo (label del modelo vigente): sale ANTES del primer ≤ 0.
            o_cum, o_n = 0.0, 0
            for j in range(1, max_int + 1):
                if i + j >= n or rates[i + j] <= 0:
                    break
                o_cum += rates[i + j]
                o_n += 1
            o_days = o_n * ih / 24
            oracle = max(-100.0, min(400.0, (o_cum - FEE) / max(o_days, 1 / ppd) * 365 * 100)) \
                if o_n else -100.0
            params = {"cv": ws["cv"], "min_ratio": ws["min_ratio"], "streak": ws["streak"],
                      "pct": ws["pct"], "volume": vols[i], "settlement_avg": ws["settlement_avg"],
                      "payments_per_day": ppd, "current_rate": cur}
            ind = compute_all_indicators(abs(cur), hist)
            vec = build_feature_vector(params, ind)
            eligible = bool(spot[i]) and vols[i] >= MIN_VOLUME and cur * ppd >= BASE_DAILY * (1 - 1e-6)
            heur = np.nan
            if eligible:   # heurístico v11.0 de PROD con la misma ventana real
                heur = opportunity_score({**params, "rates": hist, "mode": "spot_perp",
                                          "fee_drag": fee_drag_deterministic(ws["settlement_avg"], ppd)})
            rows.append([sym, ts[i], ih, cur * ppd, vols[i], spot[i], eligible, heur, net,
                         held * ih / 24, net / HORIZON_DAYS * 365 * 100, oracle,
                         ws["settlement_avg"], *vec])
    cols = ["symbol", "funding_ts", "ih", "daily_rate", "volume", "has_spot", "eligible", "heur",
            "net_frac", "hold_days", "y_real14", "y_oracle", "w_sa", *FEATURE_NAMES]
    df = pd.DataFrame(rows, columns=cols)
    df["t"] = pd.to_datetime(df["funding_ts"], unit="ms", utc=True)
    df["hour"] = df["t"].dt.floor("h")
    # Régimen de mercado: estadísticos transversales por hora (todas las filas).
    g = df.groupby("hour")["daily_rate"]
    df["regime_median_daily"] = g.transform("median")
    df["regime_frac_premium"] = g.transform(lambda s: float((s > BASE_DAILY * 1.05).mean()))
    return df


def dedupe_columns(df: pd.DataFrame) -> pd.DataFrame:
    """'volume' es a la vez metadato y feature (mismo valor): una sola columna,
    para que df[FEATURE_NAMES] tenga exactamente 14 columnas como en prod."""
    return df.loc[:, ~df.columns.duplicated()]


# ── Métricas ─────────────────────────────────────────────────────────────────
def daily_ic(df, score_col, y="y_real14"):
    ics = []
    for _, g in df.groupby(df["t"].dt.floor("D")):
        if len(g) >= 20 and g[score_col].nunique() > 1:
            ics.append(g[score_col].rank().corr(g[y].rank()))
    a = np.array([x for x in ics if not math.isnan(x)])
    if not len(a):
        return float("nan"), float("nan"), 0
    return float(a.mean()), float(a.mean() / (a.std(ddof=1) / math.sqrt(len(a)))) if len(a) > 2 else float("nan"), len(a)


def top_decile(df, score_col, y="y_real14"):
    vals = []
    for _, g in df.groupby(df["t"].dt.floor("D")):
        if len(g) >= 10:
            vals.append(g.nlargest(max(1, len(g) // 10), score_col)[y].mean())
    return float(np.mean(vals)) if vals else float("nan")


def portfolio(df, score_col, gate=None):
    """5 slots × 1.040 USD (1x → 520 nocional), decisión cada 8 h (00/08/16 UTC).
    Entra al mejor no tenido; sale con la regla del label (tras pagar el 1er ≤0, máx 14 d)."""
    d = df.sort_values("t")
    notional = CAPITAL / SLOTS / 2
    starts = d["t"].min().floor("8h")
    ends = d["t"].max()
    by_slot = d.groupby(d["t"].dt.floor("8h"))
    open_pos, pnl, trades, wins, days_held = [], 0.0, 0, 0, 0.0
    for t, g in by_slot:
        open_pos = [p for p in open_pos if p["exit"] > t]
        free = SLOTS - len(open_pos)
        if free <= 0:
            continue
        held = {p["sym"] for p in open_pos}
        cand = g.sort_values(score_col, ascending=False).drop_duplicates("symbol")
        if gate is not None:
            cand = cand[gate(cand)]
        for r in cand.itertuples(index=False):
            if free <= 0:
                break
            if r.symbol in held:
                continue
            exit_t = r.t + timedelta(days=float(r.hold_days))
            open_pos.append({"sym": r.symbol, "exit": exit_t})
            pnl += r.net_frac * notional
            trades += 1
            wins += r.net_frac > 0
            days_held += r.hold_days
            held.add(r.symbol)
            free -= 1
    span_days = max(1.0, (ends - starts).total_seconds() / 86400)
    return {"pnl_usd": round(pnl, 1), "ret_ann_pct": round(pnl / CAPITAL / span_days * 365 * 100, 1),
            "trades": trades, "win_pct": round(100 * wins / trades, 1) if trades else 0,
            "avg_hold_d": round(days_held / trades, 1) if trades else 0, "days": round(span_days)}


# ── Walk-forward ─────────────────────────────────────────────────────────────
def fit(X, y):
    from sklearn.ensemble import HistGradientBoostingRegressor
    m = HistGradientBoostingRegressor(**MODEL_PARAMS)
    m.fit(X, y)
    return m


def variant_cols(name):
    return FEATURE_NAMES + (REGIME if name == "E1" else [])


def run_variant(df, name, folds, holdout_start):
    cols = variant_cols(name)
    preds = pd.Series(np.nan, index=df.index)
    for fs, fe in folds:
        tr = df[(df["t"] < fs - timedelta(days=PURGE_DAYS))]
        if name != "E0a":
            tr = tr[tr["eligible"]]
        te = df[(df["t"] >= fs) & (df["t"] < fe) & df["eligible"]]
        if len(tr) < 5000 or len(te) < 500:
            continue
        m = fit(tr[cols].to_numpy(), tr["y_real14"].to_numpy())
        preds.loc[te.index] = m.predict(te[cols].to_numpy())
    return preds


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--export", choices=["E0", "E0a", "E1"], help="exporta el .joblib de esa variante")
    ap.add_argument("--rebuild", action="store_true", help="recalcular features (ignora la caché)")
    a = ap.parse_args()

    if FEAT_CACHE.exists() and not a.rebuild and FEAT_CACHE.stat().st_mtime > DATA.stat().st_mtime:
        df = pd.read_pickle(FEAT_CACHE)
        print(f"features desde caché: {len(df):,} filas", flush=True)
    else:
        fr = pd.read_csv(DATA)
        fr["captured_at"] = pd.to_datetime(fr["captured_at"], utc=True)
        fr = split_on_gaps(fr)                 # deslistes/relistados → series separadas
        print(f"dataset: {len(fr):,} liquidaciones, {fr['symbol'].nunique()} símbolos, "
              f"{fr['captured_at'].min():%Y-%m-%d} → {fr['captured_at'].max():%Y-%m-%d}", flush=True)
        df = build_rows(fr)
        df.to_pickle(FEAT_CACHE)
    df = dedupe_columns(df)
    elig = df[df["eligible"]]
    print(f"filas con label: {len(df):,} | elegibles en prod: {len(elig):,}", flush=True)

    t_max = df["t"].max()
    holdout_start = (t_max - timedelta(days=HOLDOUT_DAYS)).floor("D")
    month_starts = pd.date_range(end=holdout_start, periods=WF_MONTHS + 1, freq="MS", tz="UTC")
    folds = [(month_starts[k], month_starts[k + 1]) for k in range(WF_MONTHS)]
    folds[-1] = (folds[-1][0], holdout_start)

    df["sa_rank"] = df["w_sa"] * 24.0 / df["ih"]

    results, oos = {}, {}
    wf_mask = (df["t"] >= folds[0][0]) & (df["t"] < holdout_start) & df["eligible"]
    for name in ("E0", "E0a", "E1"):
        print(f"walk-forward {name}...", flush=True)
        p = run_variant(df, name, folds, holdout_start)
        df[f"p_{name}"] = p
        sub = df[wf_mask & p.notna()]
        ic, tstat, nd = daily_ic(sub, f"p_{name}")
        per_fold = []
        for fs, fe in folds:
            f = sub[(sub["t"] >= fs) & (sub["t"] < fe)]
            if len(f):
                per_fold.append((daily_ic(f, f"p_{name}")[0], daily_ic(f, "heur")[0]))
        results[name] = {"ic": ic, "t": tstat, "days": nd, "top10": top_decile(sub, f"p_{name}"),
                         "folds": per_fold}
        oos[name] = sub
    base = df[wf_mask]
    results["heur"] = {"ic": daily_ic(base, "heur")[0], "top10": top_decile(base, "heur")}
    results["sa"] = {"ic": daily_ic(base, "sa_rank")[0], "top10": top_decile(base, "sa_rank")}
    results["oracle_vs_real"] = {"ic_model_oracle": daily_ic(oos["E0"], "p_E0", "y_oracle")[0]}

    # Gate: uplift IC > 0 en ≥ 80 % de folds, media ≥ 0.05 y mejor cartera que heur y sa.
    def gate(name):
        r = results[name]
        ups = [m - h for m, h in r["folds"] if not (math.isnan(m) or math.isnan(h))]
        share = sum(u > 0 for u in ups) / len(ups) if ups else 0
        return {"share_pos": round(share, 2), "mean_up": round(float(np.mean(ups)) if ups else float("nan"), 3)}

    pf = {}
    for name in ("E0", "E0a", "E1"):
        sub = oos[name]
        pf[name] = portfolio(sub, f"p_{name}")
        pf[name + "+gate"] = portfolio(sub, f"p_{name}", gate=lambda c, n=name: c[f"p_{n}"] > 0)
    pf["heur"] = portfolio(base, "heur")
    pf["heur+gate70"] = portfolio(base, "heur", gate=lambda c: c["heur"] >= 70)
    pf["sa"] = portfolio(base, "sa_rank")

    # Hold-out final intocado: entrenar con todo lo anterior (purgado) y evaluar.
    hold = df[(df["t"] >= holdout_start) & df["eligible"]]
    hold_res = {}
    for name in ("E0", "E0a", "E1"):
        cols = variant_cols(name)
        tr = df[df["t"] < holdout_start - timedelta(days=PURGE_DAYS)]
        if name != "E0a":
            tr = tr[tr["eligible"]]
        m = fit(tr[cols].to_numpy(), tr["y_real14"].to_numpy())
        h = hold.copy()
        h["p"] = m.predict(h[cols].to_numpy())
        hold_res[name] = {"ic": daily_ic(h, "p")[0], "top10": top_decile(h, "p"),
                          "pf": portfolio(h, "p"), "pf_gate": portfolio(h, "p", gate=lambda c: c["p"] > 0)}
    hold_res["heur"] = {"ic": daily_ic(hold, "heur")[0], "top10": top_decile(hold, "heur"),
                        "pf": portfolio(hold, "heur")}
    hold_res["sa"] = {"ic": daily_ic(hold, "sa_rank")[0], "top10": top_decile(hold, "sa_rank"),
                      "pf": portfolio(hold, "sa_rank")}

    gates = {n: gate(n) for n in ("E0", "E0a", "E1")}
    write_report(df, results, pf, hold_res, gates, folds, holdout_start)

    if a.export:
        export(df, a.export, holdout_start, results, hold_res, gates)


def export(df, name, holdout_start, results, hold_res, gates):
    import joblib
    import sklearn
    cols = variant_cols(name)
    if cols != FEATURE_NAMES:
        raise SystemExit("Exportar E1 requiere soporte de features de régimen en prod (no implementado).")
    tr = df if name == "E0a" else df[df["eligible"]]
    tr = tr[tr["t"] < df["t"].max() - timedelta(days=PURGE_DAYS)]    # todas con label completo
    m = fit(tr[cols].to_numpy(), tr["y_real14"].to_numpy())
    oos = df[f"p_{name}"].dropna()
    recent = oos[df.loc[oos.index, "t"] >= df["t"].max() - timedelta(days=90)]
    calib = [float(v) for v in np.percentile(recent if len(recent) > 1000 else oos, np.arange(0, 101))]
    today = datetime.now(timezone.utc).strftime("%Y%m%d")
    bundle = {
        "model": m, "calibration_pcts": calib, "feature_names": cols,
        "feature_spec": "settled_v1", "label": "real14", "source": "binance_vision",
        "model_version": f"{today}b",
        "train_window": {"from": str(tr["t"].min().date()), "to": str(tr["t"].max().date()),
                         "rows": int(len(tr))},
        "val_metrics": {"wf": {k: v for k, v in results[name].items() if k != "folds"},
                        "holdout": {k: v for k, v in hold_res[name].items() if not k.startswith("pf")},
                        "gate": gates[name]},
        "sklearn_version": sklearn.__version__,
    }
    MODELS_DIR.mkdir(exist_ok=True)
    joblib.dump(bundle, MODELS_DIR / "scoring_model.joblib", compress=3)
    print(f"exportado models/scoring_model.joblib ({name}, {bundle['model_version']})", flush=True)


def _f(x, d=3):
    return "—" if x is None or (isinstance(x, float) and math.isnan(x)) else f"{x:.{d}f}"


def write_report(df, results, pf, hold_res, gates, folds, holdout_start):
    REPORT_DIR.mkdir(exist_ok=True)
    today = datetime.now(timezone.utc).strftime("%Y%m%d")
    elig = df[df["eligible"]]
    L = [f"# ML Binance — dataset de tasas liquidadas — {today}", "",
         f"**Datos:** {len(df):,} filas con label ({df['symbol'].nunique()} símbolos), "
         f"{df['t'].min():%Y-%m-%d} → {df['t'].max():%Y-%m-%d}; elegibles en prod {len(elig):,}.",
         f"**Label:** real14 (sale tras pagar el 1er funding ≤ 0, tope 14 d, fee 0.30 %, APR sobre 14 d, "
         f"por nocional). **Walk-forward:** {len(folds)} folds mensuales {folds[0][0]:%Y-%m} → "
         f"{holdout_start:%Y-%m-%d}, purga {PURGE_DAYS} d. **Hold-out intocado:** desde {holdout_start:%Y-%m-%d}.",
         "", "## 1. Walk-forward (filas elegibles, IC = Spearman diario promedio)", "",
         "| variante | IC | t | días | top-10% y_real14 | folds con uplift>0 | uplift medio |",
         "| --- | --- | --- | --- | --- | --- | --- |"]
    for n in ("E0", "E0a", "E1"):
        r = results[n]
        L.append(f"| {n} | {_f(r['ic'])} | {_f(r['t'], 1)} | {r['days']} | {_f(r['top10'], 1)} | "
                 f"{gates[n]['share_pos']} | {_f(gates[n]['mean_up'])} |")
    L.append(f"| heurístico v11.0 | {_f(results['heur']['ic'])} | | | {_f(results['heur']['top10'], 1)} | | |")
    L.append(f"| top settlement_avg | {_f(results['sa']['ic'])} | | | {_f(results['sa']['top10'], 1)} | | |")
    L += ["", f"IC del modelo E0 contra el label oráculo (el del modelo vigente): "
              f"{_f(results['oracle_vs_real']['ic_model_oracle'])}.", "",
          "## 2. Cartera simulada (5 slots, 5.200 USD, 1x, decisión cada 8 h, política del label)", "",
          "| estrategia | PnL USD | retorno anual % | trades | % ganadoras | hold medio d |",
          "| --- | --- | --- | --- | --- | --- |"]
    for k, v in pf.items():
        L.append(f"| {k} | {v['pnl_usd']} | {v['ret_ann_pct']} | {v['trades']} | {v['win_pct']} | {v['avg_hold_d']} |")
    L += ["", f"## 3. Hold-out intocado ({HOLDOUT_DAYS} d)", "",
          "| estrategia | IC | top-10% | cartera PnL | anual % | trades | cartera con gate pred>0 |",
          "| --- | --- | --- | --- | --- | --- | --- |"]
    for k, v in hold_res.items():
        g = v.get("pf_gate")
        L.append(f"| {k} | {_f(v['ic'])} | {_f(v['top10'], 1)} | {v['pf']['pnl_usd']} | {v['pf']['ret_ann_pct']} | "
                 f"{v['pf']['trades']} | {(str(g['pnl_usd']) + ' / ' + str(g['ret_ann_pct']) + '%') if g else '—'} |")
    L += ["", "## 4. Por fold (IC modelo / IC heurístico)", "",
          "| fold | " + " | ".join(("E0", "E0a", "E1")) + " |", "| --- | --- | --- | --- |"]
    for k, (fs, fe) in enumerate(folds):
        cells = []
        for n in ("E0", "E0a", "E1"):
            fl = results[n]["folds"]
            cells.append(f"{_f(fl[k][0])} / {_f(fl[k][1])}" if k < len(fl) else "—")
        L.append(f"| {fs:%Y-%m} | " + " | ".join(cells) + " |")
    L += ["", "Variantes probadas: 3 (E0, E0a, E1). Se elige la más simple que pase el gate "
              "(uplift>0 en ≥80 % de folds, uplift medio ≥0.05, mejor cartera que heurístico y "
              "settlement_avg en walk-forward Y en hold-out)."]
    path = REPORT_DIR / f"ml_binance_{today}.md"
    path.write_text("\n".join(L), encoding="utf-8")
    print(f"reporte: {path}", flush=True)
    print(json.dumps({"wf": {k: {kk: vv for kk, vv in v.items() if kk != "folds"} for k, v in results.items()},
                      "gates": gates, "pf": pf,
                      "holdout": {k: {kk: vv for kk, vv in v.items()} for k, v in hold_res.items()}},
                     default=str, indent=1), flush=True)


if __name__ == "__main__":
    main()
