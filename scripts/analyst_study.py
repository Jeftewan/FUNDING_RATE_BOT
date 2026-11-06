#!/usr/bin/env python3
"""Panel de analistas + consenso + análogos — LOCAL, read-only (salvo --export).

Pregunta: entre varias oportunidades con pred > 0, ¿qué variables dicen de verdad cuál
tiene más probabilidad de cumplirse? Cada "analista" (una variable, voto por terciles) se
evalúa FUERA DE MUESTRA contra el APR neto realizado (y_rate, salida H30, fee por venue):
  * IC diario (Spearman transversal) por trimestre (12 m) o por semana (señales, ~3 m);
  * vota solo si el signo es consistente en ≥ 75 % de los periodos y |IC| ≥ 0.03.
Luego se mide si el consenso mejora la cartera (orden / filtro / sizing) frente al modelo
solo, y se construyen las tablas de frecuencias reales que muestra la UI:
  * consenso → % que cumplió (APR ≥ 5 %), % con ganancia, mediana y p25–p75 del APR;
  * análogos → mismas cifras por celda (quintil de predicción × venue × ancho del rango).

Entrada: cache/binance/ml_v3_rows.pkl (scripts/ml_v3_signals.py) + fr_binance.csv.
Salida: reports/analyst_study_YYYYMMDD.md; --export → models/analyst_table.json.
    python -u scripts/analyst_study.py [--export]
"""
import argparse
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))
import ml_binance as mb  # noqa: E402
import confidence_study as cs  # noqa: E402
from analysis import analysts as an  # noqa: E402
from analysis.ai_analyzer import ENTER_NAPR  # noqa: E402
from analysis.ml_features import FEATURE_NAMES  # noqa: E402
from analysis.signal_features import SIGNAL_FEATURES  # noqa: E402

ROWS = ROOT / "cache" / "binance" / "ml_v3_rows.pkl"
OUT = ROOT / "models" / "analyst_table.json"
MIN_IC, MIN_CONSIST = 0.03, 0.75
# Iguales para todos los símbolos a la misma hora: el IC transversal no los mide → solo contexto.
TIME_ONLY = {"regimen"}
# clave: (feature, etiqueta, qué mira)
ANALYSTS = {
    "modelo": ("p_prod", "Modelo", "APR neto que predice el modelo"),
    "racha": ("streak", "Racha", "pagos positivos seguidos"),
    "constancia": ("pct", "Constancia", "% de pagos positivos en los últimos 30"),
    "picos": ("ep_med_days", "Duración de picos", "mediana de días que duraron sus rachas anteriores"),
    "edad": ("ep_age_days", "Edad del pico", "días que lleva la racha actual"),
    "incertidumbre": ("relw", "Incertidumbre", "ancho del rango p10–p90 del modelo"),
    "regimen": ("regime_median_daily", "Régimen", "funding mediano del mercado"),
    "basis": ("basis_24h", "Basis", "prima perp vs spot, media 24 h"),
    "basis_tend": ("basis_chg_24h", "Tendencia basis", "cambio de la prima vs las 48 h previas"),
    "oi": ("log_oi_usd", "Interés abierto", "tamaño del OI en USD (log)"),
    "oi_tend": ("oi_chg_24h_pct", "Flujo OI", "cambio del OI en 24 h"),
    "posicionamiento": ("top_ls_24h", "Top traders", "long/short de los top traders, media 24 h"),
    "volatilidad": ("volatility_7d", "Volatilidad", "desvío diario 7 d del perp"),
}


# ── Datos ───────────────────────────────────────────────────────────────────
def add_episodes(cand: pd.DataFrame) -> pd.DataFrame:
    fr = pd.read_csv(mb.DATA, usecols=["symbol", "rate", "interval_hours", "funding_ts"]).sort_values("funding_ts")
    med, age = np.full(len(cand), np.nan), np.full(len(cand), np.nan)
    pos = {ix: k for k, ix in enumerate(cand.index)}
    for sym, g in cand.groupby("symbol"):
        f = fr[fr["symbol"] == sym]
        if f.empty:
            continue
        eps = an.funding_episodes(f["rate"].to_numpy(), f["funding_ts"].to_numpy(),
                                  float(f["interval_hours"].iloc[-1]))
        for ix, t in zip(g.index, g["funding_ts"].to_numpy()):
            t = int(t)
            # visto en t: solo episodios que empezaron antes; el que sigue abierto en t no terminó aún
            seen = [e if e["end_ts"] < t else {**e, "ended": False} for e in eps if e["start_ts"] <= t]
            st = an.episode_stats(seen, t)
            k = pos[ix]
            med[k] = np.nan if st["ep_med_days"] is None else st["ep_med_days"]
            age[k] = np.nan if st["ep_age_days"] is None else st["ep_age_days"]
    cand = cand.copy()
    cand["ep_med_days"], cand["ep_age_days"] = med, age
    return cand


def oos_relw(cand: pd.DataFrame, start) -> pd.Series:
    """Ancho relativo q90−q10 fuera de muestra (cabezas de confidence_study por trimestre)."""
    t_max = cand["t"].max()
    edges = list(pd.date_range(start, t_max, freq="QS", tz="UTC"))
    edges = [start] + [e for e in edges if e > start] + [t_max + timedelta(seconds=1)]
    out = pd.Series(np.nan, index=cand.index)
    for fs, fe in zip(edges[:-1], edges[1:]):
        tr = cand[cand["t"] < fs - timedelta(days=mb.PURGE_DAYS + 1)]
        te = cand[(cand["t"] >= fs) & (cand["t"] < fe)]
        if len(tr) < 5000 or te.empty:
            continue
        print(f"  cabezas {fs:%Y-%m-%d}: train {len(tr):,} test {len(te):,}", flush=True)
        out.loc[te.index] = cs.predict_heads(cs.fit_heads(tr), te)["relw"]
    return out


# ── Evaluación ──────────────────────────────────────────────────────────────
def period_ic(d: pd.DataFrame, x: str, freq: str):
    """IC diario medio por periodo (solo filas con dato)."""
    d = d.dropna(subset=[x])
    res = []
    for p, g in d.groupby(d["t"].dt.to_period(freq)):
        ic = mb.daily_ic(g, x, "y_rate")[0]
        if not np.isnan(ic):
            res.append((str(p), ic, len(g)))
    return res


def vectorized_vote(vals: pd.Series, spec: dict) -> pd.Series:
    lo, hi = spec["cuts"]
    raw = np.where(vals >= hi, 1, np.where(vals <= lo, -1, 0)) * (1 if spec["dir"] >= 0 else -1)
    return pd.Series(raw, index=vals.index).where(vals.notna())


def consensus_series(d: pd.DataFrame, specs: dict) -> pd.Series:
    num = pd.Series(0.0, index=d.index)
    den = pd.Series(0.0, index=d.index)
    for spec in specs.values():
        if not spec["votes"]:
            continue
        v = vectorized_vote(d[spec["feature"]], spec)
        num += (v * spec["weight"]).fillna(0)
        den += v.notna() * spec["weight"]
    return (5.5 + 4.5 * num / den.replace(0, np.nan)).round(1)


def outcome(g: pd.DataFrame) -> dict:
    y = g["y_rate"]
    return {"n": int(len(g)), "reach5": round(float((y >= ENTER_NAPR).mean()), 3),
            "win": round(float((g["net_frac"] > 0).mean()), 3), "med": round(float(y.median()), 1),
            "p25": round(float(y.quantile(0.25)), 1), "p75": round(float(y.quantile(0.75)), 1),
            "hold_med": round(float(g["hold_days"].median()), 1)}


CONS_EDGES = [0, 3.5, 5.0, 6.0, 7.5, 10.01]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--export", action="store_true")
    a = ap.parse_args()
    sys.stdout.reconfigure(encoding="utf-8")
    import joblib
    df = pd.read_pickle(ROWS)
    df["p_prod"] = joblib.load(ROOT / "models" / "scoring_model.joblib")["model"].predict(
        df[FEATURE_NAMES].to_numpy())
    t_max = df["t"].max()
    start = (t_max - timedelta(days=365)).floor("D")
    cand_all = df[df["p_prod"] > 0]
    print(f"candidatos pred>0: {len(cand_all):,}; evaluación {start:%Y-%m-%d} → {t_max:%Y-%m-%d}", flush=True)
    cand_all = cand_all.assign(relw=oos_relw(cand_all, start))
    cand = cand_all[cand_all["t"] >= start].dropna(subset=["relw"])
    cand = add_episodes(cand)
    sig_start = cand.loc[cand["basis_24h"].notna(), "t"].min()
    print(f"filas evaluadas {len(cand):,}; señales desde {sig_start:%Y-%m-%d}", flush=True)

    specs, ic_rows = {}, []
    for key, (feat, label, why) in ANALYSTS.items():
        sig = feat in SIGNAL_FEATURES
        d = cand[cand["t"] >= sig_start] if sig else cand
        per = period_ic(d, feat, "W" if sig else "Q")
        if len(per) < 3:
            continue
        ics = np.array([p[1] for p in per])
        mean = float(ics.mean())
        consist = float((np.sign(ics) == np.sign(mean)).mean())
        q = d[feat].dropna().quantile([1 / 3, 2 / 3]).tolist()
        # Terciles iguales (p. ej. racha = 30 en casi todos) → voto binario sin sentido: no vota.
        votes = (abs(mean) >= MIN_IC and consist >= MIN_CONSIST and key not in TIME_ONLY
                 and q[1] - q[0] > 1e-9)
        specs[key] = {"feature": feat, "label": label, "why": why, "cuts": [round(q[0], 6), round(q[1], 6)],
                      "dir": 1 if mean >= 0 else -1, "weight": round(abs(mean), 4), "votes": bool(votes),
                      "ic": round(mean, 4), "consistency": round(consist, 2), "periods": len(per),
                      "coverage": round(float(d[feat].notna().mean()), 3)}
        ic_rows.append((key, label, feat, "semanal" if sig else "trimestral", per, mean, consist, votes,
                        specs[key]["coverage"]))
        print(f"  {key}: IC {mean:+.3f}, consistencia {consist:.0%} → {'VOTA' if votes else 'informativo'}",
              flush=True)

    cand["consensus"] = consensus_series(cand, specs)
    picks = cs.picks(cand)
    picks["consensus"] = cand.loc[picks.index, "consensus"]

    # Cartera: modelo solo vs con consenso
    def blend(d, lam):
        r = d.groupby(d["t"].dt.floor("D"))
        return r["p_prod"].rank(pct=True) + lam * r["consensus"].rank(pct=True).fillna(0.5)
    cand["rank_b03"] = blend(cand, 0.3)
    cand["rank_b10"] = blend(cand, 1.0)
    tests = [("modelo (pred>0)", "p_prod", lambda c: c["p_prod"] > 0),
             ("modelo + filtro consenso > 5", "p_prod", lambda c: (c["p_prod"] > 0) & (c["consensus"].fillna(5.5) > 5)),
             ("mezcla rango 1:0.3", "rank_b03", lambda c: c["p_prod"] > 0),
             ("mezcla rango 1:1", "rank_b10", lambda c: c["p_prod"] > 0),
             ("solo consenso", "consensus", lambda c: c["p_prod"] > 0)]
    sig_c = cand[cand["t"] >= sig_start]
    port = [(lbl, mb.portfolio(cand, col, gate=g), mb.portfolio(sig_c, col, gate=g)) for lbl, col, g in tests]

    # Sizing sobre los picks del modelo
    days = max(1.0, (t_max - start).total_seconds() / 86400)
    w_cons = pd.Series(np.where(picks["consensus"] >= 6, 1.25, np.where(picks["consensus"] <= 5, 0.75, 1.0)),
                       index=picks.index)
    med_relw = picks["relw"].expanding().median().shift(1)
    w_relw = pd.Series(np.where(picks["relw"] <= med_relw, cs.SIZE_HI, cs.SIZE_LO), index=picks.index) \
        .where(med_relw.notna(), 1.0)
    sizing = [("sin sizing", cs.pnl_stats(picks, pd.Series(1.0, index=picks.index), days)),
              ("tiers ancho relativo (vigente)", cs.pnl_stats(picks, w_relw, days)),
              ("tiers consenso", cs.pnl_stats(picks, w_cons, days))]
    # Consistencia por trimestre (gate de adopción del sizing por consenso)
    q_rows = []
    for q, g in picks.groupby(picks["t"].dt.to_period("Q")):
        notional = mb.CAPITAL / mb.SLOTS / 2
        base, rw, cn = (float((g["net_frac"] * notional * w.loc[g.index]).sum())
                        for w in (pd.Series(1.0, index=picks.index), w_relw, w_cons))
        q_rows.append((str(q), len(g), base, rw, cn))
    size_gate = (sum(cn >= rw for *_, rw, cn in q_rows) >= len(q_rows) - 1
                 and sizing[2][1]["ann"] > sizing[1][1]["ann"])

    # Tablas de calibración
    def by_cons(d):
        rows = []
        for lo, hi in zip(CONS_EDGES[:-1], CONS_EDGES[1:]):
            g = d[(d["consensus"] >= lo) & (d["consensus"] < hi)]
            if len(g):
                rows.append({"lo": lo, "hi": hi, "label": an.consensus_label((lo + min(hi, 10)) / 2), **outcome(g)})
        return rows
    calib_cand, calib_picks = by_cons(cand), by_cons(picks)

    pred_cuts = [round(float(x), 2) for x in cand["p_prod"].quantile([0.2, 0.4, 0.6, 0.8])]
    relw_cuts = [round(float(x), 3) for x in cand["relw"].quantile([1 / 3, 2 / 3])]
    cells = {}
    pi = sum((cand["p_prod"] >= c).astype(int) for c in pred_cuts)
    wi = sum((cand["relw"] >= c).astype(int) for c in relw_cuts)
    for (al, p, w), g in cand.groupby([cand["alpha"], pi, wi]):
        if len(g) >= 30:
            cells[f"a{int(al)}_p{int(p)}_w{int(w)}"] = outcome(g)
    for (al, p), g in cand.groupby([cand["alpha"], pi]):
        cells[f"a{int(al)}_p{int(p)}_wx"] = outcome(g)

    # Reporte
    today = datetime.now(timezone.utc).strftime("%Y%m%d")
    L = [f"# Panel de analistas y consenso — {today}", "",
         f"Candidatos (pred vigente > 0, universo CEX ∪ Alpha, label H30): {len(cand):,} filas, "
         f"{start:%Y-%m-%d} → {t_max:%Y-%m-%d}; picks de la cartera: {len(picks)} ({int(picks['alpha'].sum())} Alpha). "
         f"Señales de mercado desde {sig_start:%Y-%m-%d} (~{(t_max - sig_start).days} d). "
         f"Vota un analista si |IC| ≥ {MIN_IC} y el signo se repite en ≥ {MIN_CONSIST:.0%} de los periodos.", "",
         "## Analistas", "",
         "| analista | variable | periodos | IC medio | consistencia | cobertura | ¿vota? | dirección | cortes (terciles) |",
         "| --- | --- | --- | --- | --- | --- | --- | --- | --- |"]
    for key, label, feat, kind, per, mean, consist, votes, cov in ic_rows:
        s = specs[key]
        L.append(f"| {label} | `{feat}` | {len(per)} {kind} | {mean:+.3f} | {consist:.0%} | {cov:.0%} | "
                 f"{'sí' if votes else 'no'} | {'más alto mejor' if s['dir'] > 0 else 'más bajo mejor'} | "
                 f"{s['cuts'][0]:.4g} / {s['cuts'][1]:.4g} |")
    L += ["", "IC por periodo:", ""]
    for key, label, feat, kind, per, *_ in ic_rows:
        L.append(f"- {label}: " + ", ".join(f"{p} {ic:+.2f}" for p, ic, _ in per))
    L += ["", "## Votos en los picks del modelo (APR realizado por voto)", "",
          "| analista | voto | picks | % APR ≥ 5 | % ganan | APR mediano |", "| --- | --- | --- | --- | --- | --- |"]
    for key, s in specs.items():
        v = vectorized_vote(picks[s["feature"]], s)
        for vv, lbl in [(1, "favorable"), (0, "neutral"), (-1, "desfavorable")]:
            g = picks[v == vv]
            if len(g):
                o = outcome(g)
                L.append(f"| {s['label']} | {lbl} | {o['n']} | {o['reach5']:.0%} | {o['win']:.0%} | {o['med']} |")
    L += ["", "## ¿El consenso mejora la cartera?", "",
          "| política | 12 m PnL USD | 12 m anual % | trades | % gan. | ventana señales PnL | anual % | trades |",
          "| --- | --- | --- | --- | --- | --- | --- | --- |"]
    for lbl, r, s in port:
        L.append(f"| {lbl} | {r['pnl_usd']} | {r['ret_ann_pct']} | {r['trades']} | {r['win_pct']} | "
                 f"{s['pnl_usd']} | {s['ret_ann_pct']} | {s['trades']} |")
    L += ["", "## Sizing sobre los picks del modelo", "", "| sizing | PnL USD | anual % | DD % |", "| --- | --- | --- | --- |"]
    for lbl, st in sizing:
        L.append(f"| {lbl} | {st['pnl']:.1f} | {st['ann']:.1f} | {st['dd']:.2f} |")
    L += ["", "Por trimestre (PnL USD de los picks):", "", "| trimestre | picks | sin sizing | ancho relativo | consenso |",
          "| --- | --- | --- | --- | --- |"]
    for q, n, b, rw, cn in q_rows:
        L.append(f"| {q} | {n} | {b:.1f} | {rw:.1f} | {cn:.1f} |")
    L.append("")
    L.append(f"Gate sizing por consenso (≥ ancho relativo en todos los trimestres menos uno y mejor anual): "
             f"{'PASA' if size_gate else 'NO pasa'}")
    for title, rows in [("candidatos", calib_cand), ("picks", calib_picks)]:
        L += ["", f"## Consenso → resultado real ({title})", "",
              "| consenso | etiqueta | n | % APR ≥ 5 | % ganan | APR mediano | p25–p75 | hold mediano d |",
              "| --- | --- | --- | --- | --- | --- | --- | --- |"]
        for r in rows:
            L.append(f"| {r['lo']}–{min(r['hi'], 10):g} | {r['label']} | {r['n']} | {r['reach5']:.0%} | "
                     f"{r['win']:.0%} | {r['med']} | {r['p25']}–{r['p75']} | {r['hold_med']} |")
    L += ["", f"## Análogos: {len(cells)} celdas (quintil de predicción {pred_cuts} × venue × ancho {relw_cuts})", ""]
    gate_rank = port[0][1]["pnl_usd"]
    best = max(port[1:], key=lambda x: x[1]["pnl_usd"])
    L += ["## Veredicto", "",
          f"- Mejor política con consenso: `{best[0]}` {best[1]['pnl_usd']} USD vs modelo solo {gate_rank} USD (12 m).",
          f"- Sizing por consenso: {sizing[2][1]['ann']:.1f} % vs ancho relativo {sizing[1][1]['ann']:.1f} % "
          f"(DD {sizing[2][1]['dd']:.2f} vs {sizing[1][1]['dd']:.2f}).", ""]
    rep = ROOT / "reports" / f"analyst_study_{today}.md"
    rep.write_text("\n".join(L), encoding="utf-8")
    print("\n".join(L))

    if a.export:
        tbl = {"version": today, "eval_window": [f"{start:%Y-%m-%d}", f"{t_max:%Y-%m-%d}"],
               "signals_since": f"{sig_start:%Y-%m-%d}", "enter_napr": ENTER_NAPR,
               "analysts": specs, "consensus_calib": calib_cand, "consensus_calib_picks": calib_picks,
               "analogs": {"pred_cuts": pred_cuts, "relw_cuts": relw_cuts, "cells": cells},
               "ranking": {lbl: r for lbl, r, _ in port}, "sizing": {lbl: st for lbl, st in sizing},
               "size_by_consensus": bool(size_gate)}
        OUT.write_text(json.dumps(tbl, ensure_ascii=False, indent=1), encoding="utf-8")
        print(f"exportado {OUT}")


if __name__ == "__main__":
    main()
