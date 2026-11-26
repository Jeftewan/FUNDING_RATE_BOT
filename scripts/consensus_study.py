#!/usr/bin/env python3
"""Consenso calibrado contra el éxito real — LOCAL, read-only (salvo --export).

Pregunta: ¿puede el consenso 1–10 significar "en k×10 % de los casos parecidos se cumplió lo
esperado"? Éxito = APR neto REALIZADO ≥ APR neto que predecía el modelo al entrar (p_prod).

Label realista (no el H30 del entrenamiento):
  * salida = regla de prod (CLOSE_NEG_SETTLED: tercer pago seguido ≤ 0), tope 30 d;
  * costo: CEX = fee del modelo; Alpha = fees reales (0.41 %) + costo de precio medido en la
    operación real (ALPHA_PRICE_COST, ops_review 2026-11-24: 0.6–0.8 % del nocional);
  * confirmación con el motor de ejecución (cache/profit_study.pkl, variante "3 pagos ≤ 0",
    25 tokens Alpha, velas 1m, costo por token, espera de basis): mismo éxito, sin re-entrenar.

Agregadores (walk-forward mensual, purga PURGE_DAYS, todo fuera de muestra):
  A1  consenso vigente (models/analyst_table.json v1) + isotónica  ← referencia
  B   pool logit: cada analista → tabla por deciles P(éxito) (log-odds vs tasa base) →
      regresión logística L2 (pesos = votos; "extremizar" sale de la pendiente, Satopää et al.
      2016) → Venn-Abers inductivo (Vovk & Petej 2014) sobre los últimos CALIB_DAYS del train
  C   HistGB clasificador sobre las mismas variables + Venn-Abers (techo con esos datos)

Criterio PRE-REGISTRADO (fijado 2026-11-26, antes de correr), label amplio, 12 meses:
  1. ΔBrier vs A1 < 0 con IC 95 % bootstrap (bloques símbolo × semana) bajo cero y mejor en
     ≥ 80 % de los trimestres.
  2. Calibración: ECE ≤ 0.05; cada tramo 1–10 con n ≥ 50 dentro de ±10 pp de su centro;
     tasa de éxito creciente por tramo (se toleran inversiones < 3 pp).
  3. Resolución: tramo ≥ 9 con éxito ≥ 1.5× la tasa base (o, si no hay n ≥ 50 en ≥ 9, el
     decil superior de P ≥ 1.5× la base).
  4. Entre las que pasan, la más simple si la diferencia de Brier es < 0.002 (B antes que C:
     B se sirve en stdlib).
  5. Confirmación con el motor: Brier skill vs tasa base > 0 y ECE ≤ 0.08.
  Sizing (1.25× si P ≥ tercil alto / 0.75× si ≤ tercil bajo de los picks previos) reemplaza al
  vigente solo si gana en anual y en todos los trimestres menos uno (gate de analyst_study).
La referencia es A1 y no el consenso crudo /10: así la mejora tiene que venir de información,
no solo de re-escalar.

Entrada: cache/binance/ml_v2_rows.pkl (ml_v2.py --refresh), fr_binance.csv, signals_1h.csv
(signals_dataset.py --days 365), cache/profit_study.pkl.
Salida: reports/consensus_study_YYYYMMDD.md; --export → models/analyst_table.json (v2).
    python -u scripts/consensus_study.py [--export]
"""
import argparse
import json
import math
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))
import analyst_study as ast_  # noqa: E402
import attribution_study as att  # noqa: E402
import confidence_study as cs  # noqa: E402
import ml_binance as mb  # noqa: E402
import ml_v2  # noqa: E402
import ml_v3_signals as v3  # noqa: E402
from analysis.ml_features import FEATURE_NAMES  # noqa: E402
from analysis.signal_features import SIGNAL_FEATURES  # noqa: E402

CACHE = ROOT / "cache" / "consensus_rows.pkl"
ENGINE = ROOT / "cache" / "profit_study.pkl"
ENGINE_VI = 11                 # profit_study.variants(): "X2 salir con 3 pagos ≤ 0"
OUT = ROOT / "models" / "analyst_table.json"
K_NEG = 3                      # CLOSE_NEG_SETTLED
ALPHA_PRICE_COST = 0.0070      # costo de precio real por operación (fracción del nocional)
PURGE_DAYS = 31                # el label mira hasta 30 d + 1 d de embargo
CALIB_DAYS = 60
N_BINS = 10
MIN_BIN_ROWS = 2000            # filas con dato en el train para que un analista entre
SMOOTH = 20                    # suavizado de cada decil hacia la tasa base (pseudo-filas)

# clave: (feature, etiqueta, qué mira). Solo variables que la lista de prod puede calcular con
# paridad (basis_chg_24h queda fuera: ρ 0.59 prod vs offline en signals_dataset --check).
ANALYSTS = {
    "modelo": ("p_prod", "Modelo", "APR neto que predice el modelo"),
    "tasa": ("rate_apr", "Tasa vigente", "APR de la tasa actual"),
    "brecha": ("pred_gap", "Brecha", "predicción − tasa actual (reversión a la media)"),
    "incertidumbre": ("relw", "Incertidumbre", "ancho del rango p10–p90 del modelo"),
    "racha": ("streak", "Racha", "pagos positivos seguidos"),
    "constancia": ("pct", "Constancia", "% de pagos positivos en los últimos 30"),
    "regimen": ("regime_median_daily", "Régimen", "funding mediano del mercado"),
    "volumen": ("log_volume", "Volumen", "volumen 24 h del perp (log)"),
    "intervalo": ("ih", "Intervalo", "horas entre pagos"),
    "multitud": ("crowd", "Multitud", "oportunidades con pred > 0 a la vez"),
    "basis": ("basis_24h", "Basis", "prima perp vs spot, media 24 h"),
    "oi": ("log_oi_usd", "Interés abierto", "tamaño del OI en USD (log)"),
    "oi_tend": ("oi_chg_24h_pct", "Flujo OI", "cambio del OI en 24 h"),
    "posicionamiento": ("top_ls_24h", "Top traders", "long/short de los top traders, media 24 h"),
    "volatilidad": ("volatility_7d", "Volatilidad", "desvío diario 7 d del perp"),
}
FEATS = [v[0] for v in ANALYSTS.values()]


# ── Label ───────────────────────────────────────────────────────────────────
def k_labels(fr: pd.DataFrame, k: int = K_NEG, cap_days: float = 30.0) -> pd.DataFrame:
    """Funding de i+1 hasta la salida (k pagos seguidos ≤ 0, tope cap_days) y días de hold.
    Con k = 2 reproduce ml_v2.h30_labels."""
    out = []
    for sym, g in fr.groupby("symbol"):
        g = g.sort_values("funding_ts")
        r = g["rate"].to_numpy(float)
        ih = np.maximum(1.0, g["interval_hours"].to_numpy(float))
        n = len(r)
        cs_ = np.concatenate([[0.0], np.cumsum(r)])
        neg = (r <= 0).astype(int)
        run = np.zeros(n, int)                     # largo de la racha ≤ 0 que termina en m
        for m in range(n):
            run[m] = run[m - 1] + 1 if neg[m] and m else neg[m]
        idx = np.where(run >= k, np.arange(n), n)
        next_end = np.minimum.accumulate(idx[::-1])[::-1]
        i = np.arange(n)
        max_int = np.rint(cap_days * 24 / ih).astype(int)
        first = np.minimum(i + k, n - 1)
        exit_k = np.where(i + k <= n - 1, next_end[first], n)
        exit_ = np.minimum(exit_k, i + max_int)
        ok = exit_ <= n - 1
        e = np.where(ok, exit_, 0)
        out.append(pd.DataFrame({"symbol": sym, "funding_ts": g["funding_ts"].to_numpy(np.int64),
                                 "fund_K": np.where(ok, cs_[e + 1] - cs_[i + 1], np.nan),
                                 "hold_K": np.where(ok, (e - i) * ih / 24, np.nan)}))
    return pd.concat(out, ignore_index=True)


def apr(net_frac, hold_days):
    return (net_frac / np.maximum(hold_days, 1 / 3) * 365 * 100).clip(-200, 400)


def engine_labels(cand: pd.DataFrame) -> pd.Series:
    """Éxito según el motor de ejecución (NaN fuera de sus 25 tokens o sin llenado)."""
    e = pd.read_pickle(ENGINE)
    e = e[(e["vi"] == ENGINE_VI) & (e["frac"] > 0)].copy()
    hold = (e["out_ms"] - e["t_ms"]) / 86_400_000
    e["y_eng"] = apr(e["net"] / 100, hold)
    e = e.rename(columns={"sym": "symbol"})[["symbol", "t_ms", "y_eng"]].sort_values("t_ms")
    c = cand[["symbol", "funding_ts"]].reset_index().sort_values("funding_ts")
    m = pd.merge_asof(c, e, left_on="funding_ts", right_on="t_ms", by="symbol",
                      direction="forward", tolerance=5 * 60_000)
    return m.set_index("index")["y_eng"].reindex(cand.index)


# ── Filas ───────────────────────────────────────────────────────────────────
def build_rows(refresh: bool) -> pd.DataFrame:
    if CACHE.exists() and not refresh:
        return pd.read_pickle(CACHE)
    import joblib
    df = ml_v2.build(False)
    df["p_prod"] = joblib.load(ROOT / "models" / "scoring_model.joblib")["model"].predict(
        df[FEATURE_NAMES].to_numpy())
    cand = df[df["p_prod"] > 0].copy()
    fr = pd.read_csv(mb.DATA, usecols=["symbol", "rate", "interval_hours", "funding_ts"])
    cand = cand.merge(k_labels(fr), on=["symbol", "funding_ts"], how="left").dropna(subset=["fund_K"])
    # Censura: cerca del final solo tienen label las que ya salieron (sesgo a pérdidas) →
    # solo filas con el horizonte completo (30 d + 1) observable.
    end = int(fr["funding_ts"].max()) - (30 + 1) * 86_400_000
    cand = cand[cand["funding_ts"] <= end]
    cost = np.where(cand["alpha"] == 1, ml_v2.FEE_ALPHA + ALPHA_PRICE_COST, ml_v2.FEE_CEX)
    cand["net_real"] = cand["fund_K"] - cost
    cand["y_real"] = apr(cand["net_real"], cand["hold_K"])
    cand["success"] = (cand["y_real"] >= cand["p_prod"]).astype(int)
    cand["y_eng"] = engine_labels(cand)
    # Analistas nuevos (calculables en prod con lo que ya trae la oportunidad)
    cand["rate_apr"] = cand["daily_rate"] * 365 * 100
    cand["pred_gap"] = cand["p_prod"] - cand["rate_apr"]
    cand["log_volume"] = np.log10(cand["volume"].clip(lower=1))
    cand["crowd"] = cand.groupby(cand["t"].dt.floor("8h"))["symbol"].transform("nunique")
    # Señales (ventana del CSV) y ancho relativo fuera de muestra
    sig_lo = pd.to_datetime(pd.read_csv(v3.SIG_CSV, usecols=["ts_hour"])["ts_hour"].min(), unit="s", utc=True)
    win = (sig_lo + timedelta(days=3)).ceil("D")
    cand = pd.concat([cand[cand["t"] < win].assign(**{c: np.nan for c in SIGNAL_FEATURES}),
                      v3.attach_signals(cand[cand["t"] >= win].copy())])
    relw_start = (cand["t"].min() + timedelta(days=150)).floor("D")
    print(f"ancho relativo fuera de muestra desde {relw_start:%Y-%m-%d}", flush=True)
    cand["relw"] = ast_.oos_relw(cand, relw_start)
    cand.attrs["sig_start"] = str(win)
    cand.to_pickle(CACHE)
    return cand


# ── Analistas: tabla por deciles ────────────────────────────────────────────
def logit(p):
    p = np.clip(p, 1e-4, 1 - 1e-4)
    return np.log(p / (1 - p))


def fit_bins(x: pd.Series, y: pd.Series, base: float):
    """Cortes por deciles del train y log-odds suavizado de cada decil vs la base."""
    ok = x.notna()
    if ok.sum() < MIN_BIN_ROWS:
        return None
    xs, ys = x[ok].to_numpy(float), y[ok].to_numpy(float)
    edges = np.unique(np.quantile(xs, np.linspace(0, 1, N_BINS + 1)[1:-1]))
    b = np.searchsorted(edges, xs, side="right")
    z = []
    for k in range(len(edges) + 1):
        m = b == k
        p = (ys[m].sum() + SMOOTH * base) / (m.sum() + SMOOTH)
        z.append(float(logit(p) - logit(base)))
    return {"edges": [float(e) for e in edges], "z": z}


def apply_bins(x: pd.Series, spec) -> np.ndarray:
    if spec is None:
        return np.zeros(len(x))
    v = x.to_numpy(float)
    z = np.asarray(spec["z"])[np.searchsorted(spec["edges"], np.nan_to_num(v, nan=0.0), side="right")]
    return np.where(np.isnan(v), 0.0, z)


# ── Venn-Abers inductivo (grilla) ───────────────────────────────────────────
def _iso_at(s_cal, y_cal, s, lab):
    from sklearn.isotonic import IsotonicRegression
    xs = np.append(s_cal, s)
    ys = np.append(y_cal, lab)
    ir = IsotonicRegression(y_min=0, y_max=1, out_of_bounds="clip").fit(xs, ys)
    return float(ir.predict([s])[0])


def ivap_grid(s_cal: np.ndarray, y_cal: np.ndarray, n_grid: int = 101) -> dict:
    """p0/p1 de Venn-Abers en una grilla de cuantiles del score de calibración; prod interpola.
    p = p1 / (1 − p0 + p1) (fusión de Vovk et al. 2015 para log-loss)."""
    grid = np.unique(np.quantile(s_cal, np.linspace(0, 1, n_grid)))
    p0 = np.array([_iso_at(s_cal, y_cal, s, 0) for s in grid])
    p1 = np.array([_iso_at(s_cal, y_cal, s, 1) for s in grid])
    p = p1 / (1 - p0 + p1)
    return {"grid": grid.tolist(), "p0": p0.tolist(), "p1": p1.tolist(), "p": p.tolist()}


def ivap_apply(g: dict, s: np.ndarray):
    return (np.interp(s, g["grid"], g["p"]), np.interp(s, g["grid"], g["p0"]),
            np.interp(s, g["grid"], g["p1"]))


# ── Modelos por fold ────────────────────────────────────────────────────────
def fit_pool(tr: pd.DataFrame, feats=None):
    from sklearn.linear_model import LogisticRegression
    feats = feats or FEATS
    base = float(tr["success"].mean())
    bins = {f: fit_bins(tr[f], tr["success"], base) for f in feats}
    Z = np.column_stack([apply_bins(tr[f], bins[f]) for f in feats])
    lr = LogisticRegression(C=1.0, max_iter=2000).fit(Z, tr["success"].to_numpy())
    return {"base": base, "bins": bins, "coef": lr.coef_[0].tolist(),
            "intercept": float(lr.intercept_[0]), "feats": feats}


def pool_score(m, d: pd.DataFrame) -> np.ndarray:
    Z = np.column_stack([apply_bins(d[f], m["bins"][f]) for f in m["feats"]])
    return Z @ np.asarray(m["coef"]) + m["intercept"]


def fit_hgb(tr: pd.DataFrame):
    """(modelo, columnas): sin las variables que el train aún no tiene (señales antes de su ventana)."""
    from sklearn.ensemble import HistGradientBoostingClassifier
    cols = [f for f in FEATS if tr[f].notna().sum() >= MIN_BIN_ROWS]
    m = HistGradientBoostingClassifier(max_leaf_nodes=15, learning_rate=0.05, max_iter=300,
                                       min_samples_leaf=200, random_state=0).fit(
        tr[cols].to_numpy(float), tr["success"].to_numpy())
    return m, cols


def walk_forward(c: pd.DataFrame, start, v1_specs, calib_days: int = None) -> pd.DataFrame:
    from sklearn.isotonic import IsotonicRegression
    t_max = c["t"].max()
    edges = list(pd.date_range(start, t_max, freq="MS", tz="UTC"))
    edges = [start] + [e for e in edges if e > start] + [t_max + timedelta(seconds=1)]
    out = []
    purge = timedelta(days=PURGE_DAYS)
    for fs, fe in zip(edges[:-1], edges[1:]):
        te = c[(c["t"] >= fs) & (c["t"] < fe)].copy()
        tr = c[c["t"] < fs - purge]
        if te.empty or len(tr) < 20000:
            continue
        cal_lo = fs - purge - timedelta(days=calib_days or CALIB_DAYS)
        prop, cal = tr[tr["t"] < cal_lo], tr[tr["t"] >= cal_lo]
        te["p_base"] = tr["success"].mean()
        # A1: consenso vigente + isotónica sobre todo el train
        ok = tr["cons_v1"].notna()
        ir = IsotonicRegression(y_min=0, y_max=1, out_of_bounds="clip").fit(
            tr.loc[ok, "cons_v1"], tr.loc[ok, "success"])
        te["p_A1"] = np.where(te["cons_v1"].notna(), ir.predict(te["cons_v1"].fillna(5.5)), te["p_base"])
        # B: pool logit + Venn-Abers
        m = fit_pool(prop)
        g = ivap_grid(pool_score(m, cal), cal["success"].to_numpy(float))
        te["p_B"], te["p0_B"], te["p1_B"] = ivap_apply(g, pool_score(m, te))
        # C: HistGB + Venn-Abers
        h, hc = fit_hgb(prop)
        gh = ivap_grid(h.predict_proba(cal[hc].to_numpy(float))[:, 1], cal["success"].to_numpy(float))
        te["p_C"] = ivap_apply(gh, h.predict_proba(te[hc].to_numpy(float))[:, 1])[0]
        print(f"  fold {fs:%Y-%m}: train {len(prop):,}+{len(cal):,} test {len(te):,} "
              f"éxito {te['success'].mean():.2f}", flush=True)
        out.append(te)
    return pd.concat(out)


# ── Métricas ────────────────────────────────────────────────────────────────
def brier(p, y):
    return float(np.mean((p - y) ** 2))


def logloss(p, y):
    p = np.clip(p, 1e-4, 1 - 1e-4)
    return float(-np.mean(y * np.log(p) + (1 - y) * np.log(1 - p)))


def murphy(p, y, bins=10):
    """(confiabilidad, resolución, incertidumbre, ECE) con tramos de ancho 0.1."""
    b = np.minimum((p * bins).astype(int), bins - 1)
    ybar = y.mean()
    rel = res = ece = 0.0
    for k in range(bins):
        m = b == k
        if m.any():
            w = m.mean()
            rel += w * (p[m].mean() - y[m].mean()) ** 2
            res += w * (y[m].mean() - ybar) ** 2
            ece += w * abs(p[m].mean() - y[m].mean())
    return rel, res, ybar * (1 - ybar), ece


def consensus_of(p):
    return np.clip(np.round(10 * p, 1), 1, 10)


def tramo_table(p, y, yr, n_min=50):
    """Tramo k = consenso en [k−1, k) (10 incluye 10). Centro nominal (k − 0.5)/10."""
    c = np.minimum(np.floor(10 * p).astype(int) + 1, 10)
    rows = []
    for k in range(1, 11):
        m = c == k
        if m.any():
            rows.append({"tramo": k, "n": int(m.sum()), "p_media": float(p[m].mean()),
                         "exito": float(y[m].mean()), "apr_med": float(np.median(yr[m])),
                         "ok": bool(m.sum() < n_min or abs(y[m].mean() - (k - 0.5) / 10) <= 0.10)})
    return rows


def block_boot_delta(d, a, b, reps=1000, seed=0):
    """IC 95 % de Brier(a) − Brier(b) remuestreando bloques símbolo × semana."""
    blk = d["symbol"].astype(str) + "|" + d["t"].dt.strftime("%G-%V")
    diff = (d[a] - d["success"]) ** 2 - (d[b] - d["success"]) ** 2
    agg = pd.DataFrame({"s": diff, "blk": blk}).groupby("blk")["s"].agg(["sum", "count"])
    s, n = agg["sum"].to_numpy(), agg["count"].to_numpy()
    rng = np.random.default_rng(seed)
    vals = []
    for _ in range(reps):
        k = rng.integers(0, len(s), len(s))
        vals.append(s[k].sum() / n[k].sum())
    return float(diff.mean()), float(np.quantile(vals, 0.025)), float(np.quantile(vals, 0.975))


def evaluate(d: pd.DataFrame, col: str, ref: str = "p_A1") -> dict:
    p, y = d[col].to_numpy(float), d["success"].to_numpy(float)
    rel, res, unc, ece = murphy(p, y)
    base = d["p_base"].to_numpy(float)
    tr = tramo_table(p, y, d["y_real"].to_numpy(float))
    rates = [r["exito"] for r in tr if r["n"] >= 50]
    mono = all(b2 >= a2 - 0.03 for a2, b2 in zip(rates, rates[1:]))
    top = [r for r in tr if r["tramo"] >= 9 and r["n"] >= 50]
    br = float(y.mean())
    if top:
        top_rate = sum(r["exito"] * r["n"] for r in top) / sum(r["n"] for r in top)
    else:
        top_rate = float(y[p >= np.quantile(p, 0.9)].mean())
    q = []
    for per, g in d.groupby(d["t"].dt.to_period("Q")):
        q.append((str(per), len(g), brier(g[col].to_numpy(), g["success"].to_numpy()),
                  brier(g[ref].to_numpy(), g["success"].to_numpy())))
    delta = block_boot_delta(d, col, ref) if col != ref else (0.0, 0.0, 0.0)
    return {"brier": brier(p, y), "logloss": logloss(p, y), "bss_base": 1 - brier(p, y) / brier(base, y),
            "rel": rel, "res": res, "unc": unc, "ece": ece, "tramos": tr, "mono": mono,
            "top_rate": top_rate, "base_rate": br, "quarters": q, "delta": delta,
            "q_better": sum(a < b for _, _, a, b in q) / max(1, len(q))}


def passes(ev: dict) -> dict:
    lo, hi = ev["delta"][1], ev["delta"][2]
    c = {"1 ΔBrier vs A1 (IC < 0, ≥ 80 % trimestres)": hi < 0 and ev["q_better"] >= 0.8,
         "2 calibración (ECE ≤ 0.05, tramos ±10 pp, monótono)":
             ev["ece"] <= 0.05 and all(r["ok"] for r in ev["tramos"]) and ev["mono"],
         "3 resolución (tramo ≥ 9 ≥ 1.5× base)": ev["top_rate"] >= 1.5 * ev["base_rate"]}
    return c


# ── Sizing ──────────────────────────────────────────────────────────────────
def sizing(d: pd.DataFrame, col: str, start, t_max):
    e = d.assign(net_frac=d["net_real"], hold_days=d["hold_K"])
    pk = cs.picks(e)
    w_v1 = pd.Series(np.where(pk["cons_v1"] >= 6, 1.25, np.where(pk["cons_v1"] <= 5, 0.75, 1.0)),
                     index=pk.index)
    hi = pk[col].expanding().quantile(2 / 3).shift(1)
    lo = pk[col].expanding().quantile(1 / 3).shift(1)
    w_p = pd.Series(np.where(pk[col] >= hi, 1.25, np.where(pk[col] <= lo, 0.75, 1.0)), index=pk.index) \
        .where(hi.notna(), 1.0)
    days = max(1.0, (t_max - start).total_seconds() / 86400)
    tot = {"sin sizing": cs.pnl_stats(pk, pd.Series(1.0, index=pk.index), days),
           "consenso vigente": cs.pnl_stats(pk, w_v1, days), "P calibrada": cs.pnl_stats(pk, w_p, days)}
    notional = mb.CAPITAL / mb.SLOTS / 2
    qs = []
    for per, g in pk.groupby(pk["t"].dt.to_period("Q")):
        qs.append((str(per), len(g), float((g["net_frac"] * notional * w_v1.loc[g.index]).sum()),
                   float((g["net_frac"] * notional * w_p.loc[g.index]).sum())))
    gate = tot["P calibrada"]["ann"] > tot["consenso vigente"]["ann"] and \
        sum(b >= a for *_, a, b in qs) >= len(qs) - 1
    return pk, tot, qs, gate, (float(lo.iloc[-1]), float(hi.iloc[-1])) if len(pk) else (None, None)


# ── Export ──────────────────────────────────────────────────────────────────
def export_table(c: pd.DataFrame, ev: dict, size_gate: bool, size_cuts, today: str, t_max):
    cal_lo = t_max - timedelta(days=PURGE_DAYS + CALIB_DAYS)
    prop = c[c["t"] < cal_lo]
    cal = c[(c["t"] >= cal_lo) & (c["t"] < t_max - timedelta(days=PURGE_DAYS))]
    m = fit_pool(prop)
    g = ivap_grid(pool_score(m, cal), cal["success"].to_numpy(float))
    analysts = {}
    for (key, (feat, label, why)), coef in zip(ANALYSTS.items(), m["coef"]):
        b = m["bins"][feat]
        analysts[key] = {"feature": feat, "label": label, "why": why, "votes": b is not None,
                         "coef": round(coef, 4), "bins": b}
    tbl = {"version": today, "kind": "logit_pool_v2", "success": "y_real >= p_prod",
           "eval_window": [f"{c['t'].min():%Y-%m-%d}", f"{t_max:%Y-%m-%d}"],
           "base_rate": round(m["base"], 4), "intercept": round(m["intercept"], 4), "analysts": analysts,
           "ivap": {k: [round(float(x), 5) for x in v] for k, v in g.items()},
           "consensus_calib": [{"lo": r["tramo"] - 1, "hi": r["tramo"] + (0.01 if r["tramo"] == 10 else 0),
                                "n": r["n"], "reach": round(r["exito"], 3), "med": round(r["apr_med"], 1)}
                               for r in ev["tramos"]],
           "size_by_consensus": bool(size_gate),
           "size_cuts": [round(x, 4) for x in size_cuts] if size_gate and size_cuts[0] is not None else None}
    OUT.write_text(json.dumps(tbl, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"exportado {OUT}")


# ── Main ────────────────────────────────────────────────────────────────────
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--export", action="store_true")
    ap.add_argument("--refresh", action="store_true", help="recalcular filas")
    ap.add_argument("--calib-days", type=int, default=CALIB_DAYS,
                    help=f"ventana de Venn-Abers (registrada: {CALIB_DAYS}); otra = sensibilidad, no se exporta")
    a = ap.parse_args()
    sys.stdout.reconfigure(encoding="utf-8")
    c = build_rows(a.refresh)
    v1 = json.loads(OUT.read_text(encoding="utf-8"))
    v1_specs = v1["analysts"] if v1.get("kind") != "logit_pool_v2" else json.loads(
        (ROOT / "reports" / "analyst_table_v1.json").read_text(encoding="utf-8"))["analysts"]
    c = c.copy()
    # el consenso v1 usa edad del pico (solo modal) → aquí se abstiene, como en la lista de prod
    c["cons_v1"] = ast_.consensus_series(c.assign(ep_age_days=np.nan), v1_specs)
    t_max = c["t"].max()
    start = (t_max - timedelta(days=365)).floor("D")
    print(f"candidatos con label: {len(c):,}; evaluación {start:%Y-%m-%d} → {t_max:%Y-%m-%d}", flush=True)
    d = walk_forward(c, start, v1_specs, calib_days=a.calib_days)
    evs = {k: evaluate(d, k) for k in ("p_base", "p_A1", "p_B", "p_C")}
    chk = {k: passes(evs[k]) for k in ("p_B", "p_C")}
    ok = [k for k in ("p_B", "p_C") if all(chk[k].values())]
    if len(ok) == 2 and evs["p_B"]["brier"] - evs["p_C"]["brier"] < 0.002:
        ok = ["p_B"]
    winner = min(ok, key=lambda k: evs[k]["brier"]) if ok else None
    # Confirmación con el motor de ejecución
    de = d[d["y_eng"].notna()].copy()
    de["success"] = (de["y_eng"] >= de["p_prod"]).astype(int)
    de["y_real"] = de["y_eng"]
    eng = {k: evaluate(de, k) for k in ("p_base", "p_A1", "p_B", "p_C")} if len(de) > 500 else {}
    eng_ok = {k: bool(eng and eng[k]["bss_base"] > 0 and eng[k]["ece"] <= 0.08) for k in ("p_B", "p_C")}
    if winner and not eng_ok.get(winner):
        winner = None
    pk, size_tot, size_q, size_gate, size_cuts = sizing(d, winner or "p_B", start, t_max)

    today = datetime.now(timezone.utc).strftime("%Y%m%d")
    tag = "" if a.calib_days == CALIB_DAYS else f"_cal{a.calib_days}"
    L = [f"# Consenso calibrado contra el éxito real — {today}"
         + (f" (sensibilidad: Venn-Abers con {a.calib_days} d)" if tag else ""), "",
         f"Éxito = APR neto realizado (salida al {K_NEG}.er pago seguido ≤ 0, tope 30 d; costo Alpha "
         f"{(ml_v2.FEE_ALPHA + ALPHA_PRICE_COST) * 100:.2f} % del nocional, CEX {ml_v2.FEE_CEX * 100:.2f} %) "
         f"≥ APR neto predicho al entrar. Candidatos con pred > 0 (CEX ∪ Alpha): {len(c):,}; evaluados fuera "
         f"de muestra {len(d):,} ({start:%Y-%m-%d} → {t_max:%Y-%m-%d}, walk-forward mensual, purga "
         f"{PURGE_DAYS} d). Señales desde {c.attrs.get('sig_start', '?')[:10]}. Tasa base de éxito "
         f"{d['success'].mean():.1%}.", "",
         "## Métricas fuera de muestra", "",
         "| P | Brier | log-loss | BSS vs base | confiabilidad | resolución | ECE | ΔBrier vs A1 [IC 95 %] | "
         "trimestres mejor | tramo ≥ 9 | monótono |", "| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |"]
    names = {"p_base": "tasa base", "p_A1": "A1 consenso vigente + isotónica", "p_B": "B pool logit + Venn-Abers",
             "p_C": "C HistGB + Venn-Abers"}
    for k, ev in evs.items():
        dl = ev["delta"]
        L.append(f"| {names[k]} | {ev['brier']:.4f} | {ev['logloss']:.4f} | {ev['bss_base']:+.3f} | {ev['rel']:.4f} | "
                 f"{ev['res']:.4f} | {ev['ece']:.3f} | {dl[0]:+.4f} [{dl[1]:+.4f}, {dl[2]:+.4f}] | "
                 f"{ev['q_better']:.0%} | {ev['top_rate']:.0%} | {'sí' if ev['mono'] else 'no'} |")
    for k in ("p_A1", "p_B", "p_C"):
        L += ["", f"### Tramos — {names[k]}", "", "| consenso | n | P media | % éxito | APR real mediano | ±10 pp |",
              "| --- | --- | --- | --- | --- | --- |"]
        for r in evs[k]["tramos"]:
            L.append(f"| {r['tramo'] - 1}–{r['tramo']} | {r['n']} | {r['p_media']:.0%} | {r['exito']:.0%} | "
                     f"{r['apr_med']:.1f} | {'ok' if r['ok'] else 'NO'} |")
    L += ["", "### Brier por trimestre (vs A1)", "", "| trimestre | n | B | C | A1 |", "| --- | --- | --- | --- | --- |"]
    qb, qc = evs["p_B"]["quarters"], evs["p_C"]["quarters"]
    for (per, n, b, a1), (_, _, cc, _) in zip(qb, qc):
        L.append(f"| {per} | {n} | {b:.4f} | {cc:.4f} | {a1:.4f} |")
    L += ["", "## Criterio pre-registrado", ""]
    for k in ("p_B", "p_C"):
        for name, v in chk[k].items():
            L.append(f"- {names[k]} — {name}: {'PASA' if v else 'no'}")
        L.append(f"- {names[k]} — 5 confirmación motor (BSS > 0, ECE ≤ 0.08): {'PASA' if eng_ok[k] else 'no'}")
    if eng:
        L += ["", f"## Confirmación con el motor de ejecución ({len(de):,} filas, 25 tokens Alpha, "
              f"éxito {de['success'].mean():.1%})", "", "| P | Brier | BSS vs base | ECE | tramo ≥ 9 |",
              "| --- | --- | --- | --- | --- |"]
        for k, ev in eng.items():
            L.append(f"| {names[k]} | {ev['brier']:.4f} | {ev['bss_base']:+.3f} | {ev['ece']:.3f} | {ev['top_rate']:.0%} |")
    # Analistas: peso final (último fold reentrenado sobre todo) y separación univariada
    m_all = fit_pool(c[c["t"] < t_max - timedelta(days=PURGE_DAYS)])
    L += ["", "## Analistas (pool logit entrenado con todo)", "",
          "| analista | variable | coef | log-odds decil bajo → alto | cobertura |", "| --- | --- | --- | --- | --- |"]
    for (key, (feat, label, _)), coef in zip(ANALYSTS.items(), m_all["coef"]):
        b = m_all["bins"][feat]
        rng_ = f"{b['z'][0]:+.2f} → {b['z'][-1]:+.2f}" if b else "sin datos"
        L.append(f"| {label} | `{feat}` | {coef:+.3f} | {rng_} | {c[feat].notna().mean():.0%} |")
    L += ["", "## Éxito por quintil de predicción (lo esperado sube → cuesta más cumplir)", "",
          "| quintil p_prod | n | pred mediana | % éxito | APR real mediano |", "| --- | --- | --- | --- | --- |"]
    for qi, g in d.groupby(pd.qcut(d["p_prod"], 5, labels=False)):
        L.append(f"| {qi + 1} | {len(g)} | {g['p_prod'].median():.1f} | {g['success'].mean():.0%} | "
                 f"{g['y_real'].median():.1f} |")
    L += ["", f"## Sizing sobre los picks ({len(pk)}; P = {names[winner or 'p_B']})", "",
          "| sizing | PnL USD | anual % | DD % |", "| --- | --- | --- | --- |"]
    for lbl, st in size_tot.items():
        L.append(f"| {lbl} | {st['pnl']:.1f} | {st['ann']:.1f} | {st['dd']:.2f} |")
    L += ["", "| trimestre | picks | consenso vigente | P calibrada |", "| --- | --- | --- | --- |"]
    for per, n, a1, b1 in size_q:
        L.append(f"| {per} | {n} | {a1:.1f} | {b1:.1f} |")
    L += ["", f"Gate sizing: {'PASA' if size_gate else 'NO pasa'}", "", "## Veredicto", "",
          f"- Agregador elegido: {names[winner] if winner else 'ninguno pasa → se mantiene el consenso vigente'}."]
    rep = ROOT / "reports" / f"consensus_study_{today}{tag}.md"
    rep.write_text("\n".join(L), encoding="utf-8")
    print("\n".join(L))
    d.to_pickle(ROOT / "cache" / f"consensus_oos{tag}.pkl")

    if a.export and tag:
        print("no se exporta una corrida de sensibilidad")
        return
    if a.export:
        if winner != "p_B":
            print("no se exporta: solo B (pool logit) se sirve en prod y debe pasar el criterio")
            return
        (ROOT / "reports" / "analyst_table_v1.json").write_text(json.dumps(v1, ensure_ascii=False, indent=1),
                                                                encoding="utf-8")
        export_table(c, evs["p_B"], size_gate, size_cuts, today, t_max)


if __name__ == "__main__":
    main()
