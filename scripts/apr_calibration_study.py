#!/usr/bin/env python3
"""Recalibración del "APR esperado" que muestra la UI — LOCAL, solo lectura.

Hoy la tarjeta muestra `model_prediction` (p_prod) como "APR neto esperado". Contra el APR neto
REALIZADO (salida al tercer pago seguido ≤ 0, tope 30 d; costo Alpha 0.41 % fees + 0.70 % precio,
CEX 0.30 %; filas de scripts/consensus_study.py) se cumple 61 % en el quintil bajo y 29 % en el
alto: el modelo está sobre-disperso. Aquí se ajusta un mapa monótono p_prod → mediana del APR
realizado, para que la cifra mostrada se cumpla ~50 % en todos los tramos. El ranking no cambia
(el mapa es creciente; la lista sigue ordenada por el modelo).

Variantes (walk-forward mensual, 12 meses, purga 31 d):
  M0  p_prod tal cual (vigente)
  M1  mapa global, train expansivo
  M2  mapa por venue (Alpha / CEX), train expansivo
  M3  mapa por venue, train de los últimos 180 d (régimen)
  M4  mapa por venue × tercil de volumen 24 h del perp (cortes del train)      [añadidas 2026-11-26,
  M5  mapa por venue × tercil de OI USD (sin dato = grupo propio)               tras fallar M2/M3 en 3]
Mapa: 20 cuantiles de p_prod en el train → (mediana p_prod, mediana y_real) por tramo, PAVA
ponderado por n para hacerlo creciente, interpolación lineal y plano fuera del rango.

Criterio PRE-REGISTRADO (fijado antes de correr):
  1. Pinball τ = 0.5 (|y − ŷ|/2) menor que M0, IC 95 % bootstrap por bloques símbolo × semana
     < 0, y mejor en ≥ 80 % de los trimestres.
  2. Calibración: cumplimiento global (y_real ≥ mostrado) en 50 ± 5 pp y cada decil del valor
     mostrado con n ≥ 200 en 50 ± 10 pp.
  3. Confirmación con el motor de ejecución (cache/profit_study.pkl, 25 tokens Alpha):
     cumplimiento en 50 ± 10 pp y pinball < M0.
  4. Entre las que pasan, la más simple (M1 < M2 < M3 < M4 < M5) salvo que otra baje el pinball ≥ 1 %.
  M4/M5 se evalúan con el mismo criterio 1–3, sin cambiarlo; solo usan variables con paridad en prod
  (volume_24h de la oportunidad; OI de market_feature_snapshots).
Si ninguna pasa, no se exporta y la UI sigue mostrando p_prod.

    python -u scripts/apr_calibration_study.py [--export]
    → reports/apr_calibration_YYYYMMDD.md ; --export → models/apr_calibration.json
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
import consensus_study as cst  # noqa: E402

OUT = ROOT / "models" / "apr_calibration.json"
N_Q = 20
PURGE_DAYS = cst.PURGE_DAYS
RECENT_DAYS = 180
MIN_DEC_N = 200
VARIANTS = ("M1", "M2", "M3", "M4", "M5")
TIER_FEAT = {"M4": "log_volume", "M5": "log_oi_usd"}


# ── Mapa monótono ───────────────────────────────────────────────────────────
def pava(y, w):
    """Regresión isotónica creciente (pool adjacent violators) ponderada."""
    blocks = []                                   # [valor, peso, cantidad]
    for yi, wi in zip(map(float, y), map(float, w)):
        blocks.append([yi, wi, 1])
        while len(blocks) > 1 and blocks[-2][0] > blocks[-1][0]:
            v2, w2, n2 = blocks.pop()
            v1, w1, n1 = blocks.pop()
            blocks.append([(v1 * w1 + v2 * w2) / (w1 + w2), w1 + w2, n1 + n2])
    out = []
    for v, _, n in blocks:
        out += [v] * n
    return np.array(out)


def fit_map(x: np.ndarray, y: np.ndarray, n_q: int = N_Q):
    """Nudos [(x, y)] crecientes: mediana de y por cuantil de x, PAVA por n."""
    x, y = np.asarray(x, float), np.asarray(y, float)
    edges = np.unique(np.quantile(x, np.linspace(0, 1, n_q + 1)))
    idx = np.clip(np.searchsorted(edges, x, side="right") - 1, 0, len(edges) - 2)
    kx, ky, kn = [], [], []
    for b in range(len(edges) - 1):
        m = idx == b
        if m.sum() == 0:
            continue
        kx.append(float(np.median(x[m])))
        ky.append(float(np.median(y[m])))
        kn.append(int(m.sum()))
    ky = pava(ky, kn)
    return [[round(a, 3), round(float(b), 3)] for a, b in zip(kx, ky)]


def apply_map(knots, x):
    k = np.asarray(knots, float)
    return np.interp(np.asarray(x, float), k[:, 0], k[:, 1])


def _venue(d):
    return np.where(d["alpha"] == 1, "alpha", "cex")


def _groups(d, v, cuts):
    ven = _venue(d)
    if v not in TIER_FEAT:
        return ven
    x = d[TIER_FEAT[v]].to_numpy(float)
    tier = np.full(len(d), "na", dtype=object)
    for k in ("alpha", "cex"):
        m = (ven == k) & ~np.isnan(x)
        tier[m] = np.searchsorted(cuts[k], x[m], side="right").astype(str)
    return np.char.add(np.char.add(ven.astype(str), "|"), tier.astype(str))


def fit_variant(tr: pd.DataFrame, v: str) -> dict:
    if v in TIER_FEAT:
        f = TIER_FEAT[v]
        cuts = {k: np.nanquantile(g[f], [1 / 3, 2 / 3]).round(4).tolist()
                for k, g in tr.groupby(_venue(tr))}
        gr = _groups(tr, v, cuts)
        maps = {k: fit_map(tr.loc[gr == k, "p_prod"], tr.loc[gr == k, "y_real"])
                for k in np.unique(gr) if (gr == k).sum() >= 2000}
        return {"feature": f, "cuts": cuts, "maps": maps,
                "fallback": {k: fit_map(g["p_prod"], g["y_real"]) for k, g in tr.groupby(_venue(tr))}}
    if v == "M3":
        tr = tr[tr["t"] >= tr["t"].max() - timedelta(days=RECENT_DAYS)]
    if v == "M1":
        return {"all": fit_map(tr["p_prod"], tr["y_real"])}
    return {ven: fit_map(g["p_prod"], g["y_real"])
            for ven, g in tr.groupby(np.where(tr["alpha"] == 1, "alpha", "cex"))}


def apply_variant(maps: dict, d: pd.DataFrame) -> np.ndarray:
    if "cuts" in maps:
        v = next(k for k, f in TIER_FEAT.items() if f == maps["feature"])
        gr, ven = _groups(d, v, maps["cuts"]), _venue(d)
        out = apply_variant(maps["fallback"], d)
        for k, kn in maps["maps"].items():
            m = gr == k
            out[m] = apply_map(kn, d.loc[m, "p_prod"])
        return out
    if "all" in maps:
        return apply_map(maps["all"], d["p_prod"])
    out = np.full(len(d), np.nan)
    ven = np.where(d["alpha"] == 1, "alpha", "cex")
    for k, kn in maps.items():
        m = ven == k
        out[m] = apply_map(kn, d.loc[m, "p_prod"])
    return out


# ── Walk-forward ────────────────────────────────────────────────────────────
def walk_forward(c: pd.DataFrame, start, t_max) -> pd.DataFrame:
    folds = pd.date_range(start, t_max, freq="MS", tz="UTC")
    folds = [start] + [f for f in folds if f > start]
    parts = []
    for i, fs in enumerate(folds):
        fe = folds[i + 1] if i + 1 < len(folds) else t_max + timedelta(seconds=1)
        te = c[(c["t"] >= fs) & (c["t"] < fe)].copy()
        tr = c[c["t"] < fs - timedelta(days=PURGE_DAYS)]
        if te.empty or len(tr) < 5000:
            continue
        te["M0"] = te["p_prod"]
        for v in VARIANTS:
            te[v] = apply_variant(fit_variant(tr, v), te)
        parts.append(te)
        print(f"  fold {fs:%Y-%m}: train {len(tr):,} test {len(te):,} "
              f"cumple M0 {(te['y_real'] >= te['M0']).mean():.0%} M2 {(te['y_real'] >= te['M2']).mean():.0%}",
              flush=True)
    return pd.concat(parts)


# ── Métricas ────────────────────────────────────────────────────────────────
def pinball(y, q):
    return float(np.mean(np.abs(np.asarray(y, float) - np.asarray(q, float)) / 2))


def boot_delta(d: pd.DataFrame, a: str, b: str = "M0", reps: int = 1000, seed: int = 0):
    """Δ pinball (a − b) con IC 95 % por bloques símbolo × semana ISO."""
    diff = (np.abs(d["y"] - d[a]) - np.abs(d["y"] - d[b])) / 2
    blk = d["symbol"].astype(str) + "_" + d["t"].dt.strftime("%G-%V")
    s = diff.groupby(blk).agg(["sum", "count"])
    rng = np.random.default_rng(seed)
    n = len(s)
    sm, ct = s["sum"].to_numpy(), s["count"].to_numpy()
    bs = [sm[i].sum() / ct[i].sum() for i in (rng.integers(0, n, n) for _ in range(reps))]
    return float(diff.mean()), float(np.quantile(bs, 0.025)), float(np.quantile(bs, 0.975))


def deciles(d: pd.DataFrame, col: str):
    q = pd.qcut(d[col].rank(method="first"), 10, labels=False)
    rows = []
    for k, g in d.groupby(q):
        rows.append({"dec": int(k) + 1, "n": len(g), "shown": float(g[col].median()),
                     "hit": float((g["y"] >= g[col]).mean()), "real": float(g["y"].median())})
    return rows


def evaluate(d: pd.DataFrame, col: str) -> dict:
    hit = float((d["y"] >= d[col]).mean())
    dec = deciles(d, col)
    q = [(str(p), len(g), pinball(g["y"], g[col]), pinball(g["y"], g["M0"]),
          float((g["y"] >= g[col]).mean())) for p, g in d.groupby(d["t"].dt.tz_localize(None).dt.to_period("Q"))]
    q = [r for r in q if r[1] >= 1000]
    ev = {"pin": pinball(d["y"], d[col]), "hit": hit, "dec": dec, "quarters": q,
          "q_better": sum(r[2] < r[3] for r in q) / max(len(q), 1)}
    if col != "M0":
        ev["delta"] = boot_delta(d, col)
    return ev


def passes(ev: dict, eng: dict) -> dict:
    dd = [r for r in ev["dec"] if r["n"] >= MIN_DEC_N]
    return {
        "1_pinball": ev["delta"][2] < 0 and ev["q_better"] >= 0.8,
        "2_calib": abs(ev["hit"] - 0.5) <= 0.05 and all(abs(r["hit"] - 0.5) <= 0.10 for r in dd),
        "3_motor": bool(eng) and abs(eng["hit"] - 0.5) <= 0.10 and eng["pin"] < eng["pin_m0"],
    }


def export(c: pd.DataFrame, v: str, ev: dict, today: str):
    maps = fit_variant(c, v)
    tbl = {"version": today, "kind": "median_map_v1", "variant": v,
           "target": "mediana del APR neto realizado (salida 3.er pago ≤ 0, tope 30 d, costo real)",
           "input": "model_prediction (> 0)", "maps": maps,
           "train": [f"{c['t'].min():%Y-%m-%d}" if v != "M3" else
                     f"{c['t'].max() - timedelta(days=RECENT_DAYS):%Y-%m-%d}", f"{c['t'].max():%Y-%m-%d}"],
           "oos": {"hit": round(ev["hit"], 3), "pinball": round(ev["pin"], 2)}}
    OUT.write_text(json.dumps(tbl, indent=1, ensure_ascii=False), encoding="utf-8")
    print(f"exportado {OUT}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--export", action="store_true")
    a = ap.parse_args()
    sys.stdout.reconfigure(encoding="utf-8")
    c = cst.build_rows(False)
    c = c[c["p_prod"] > 0].copy()
    t_max = c["t"].max()
    start = (t_max - timedelta(days=365)).floor("D")
    print(f"filas {len(c):,}; evaluación {start:%Y-%m-%d} → {t_max:%Y-%m-%d}", flush=True)
    d = walk_forward(c, start, t_max)
    d["y"] = d["y_real"]
    evs = {k: evaluate(d, k) for k in ("M0",) + VARIANTS}
    de = d[d["y_eng"].notna()].copy()
    de["y"] = de["y_eng"]
    eng = {}
    for k in VARIANTS:
        if len(de) > 500:
            eng[k] = {"n": len(de), "hit": float((de["y"] >= de[k]).mean()), "pin": pinball(de["y"], de[k]),
                      "pin_m0": pinball(de["y"], de["M0"]), "hit_m0": float((de["y"] >= de["M0"]).mean())}
    chk = {k: passes(evs[k], eng.get(k)) for k in VARIANTS}
    ok = [k for k in VARIANTS if all(chk[k].values())]
    winner = None
    for k in ok:                                   # orden de simplicidad
        if winner is None or evs[k]["pin"] < evs[winner]["pin"] * 0.99:
            winner = k

    today = datetime.now(timezone.utc).strftime("%Y%m%d")
    L = [f"# Recalibración del APR esperado — {today}", "",
         "Script: `scripts/apr_calibration_study.py` (criterio pre-registrado en el docstring). "
         f"Filas: candidatos `p_prod > 0` de `consensus_study` ({len(c):,}); OOS {len(d):,} "
         f"({start:%Y-%m-%d} → {t_max:%Y-%m-%d}), walk-forward mensual con purga {PURGE_DAYS} d.", "",
         "Cumple = APR neto realizado ≥ APR mostrado. Pinball = |real − mostrado| / 2 (pp de APR).", "",
         "| variante | pinball | Δ vs M0 [IC 95 %] | cumple | trimestres mejor | 1 | 2 | 3 |",
         "| --- | --- | --- | --- | --- | --- | --- | --- |"]
    for k in ("M0",) + VARIANTS:
        e = evs[k]
        dl = f"{e['delta'][0]:+.2f} [{e['delta'][1]:+.2f}, {e['delta'][2]:+.2f}]" if "delta" in e else "—"
        ck = chk.get(k, {})
        L.append(f"| {k} | {e['pin']:.2f} | {dl} | {e['hit']:.1%} | {e['q_better']:.0%} | "
                 + " | ".join("✔" if ck.get(x) else ("✘" if ck else "—")
                              for x in ("1_pinball", "2_calib", "3_motor")) + " |")
    L += ["", f"**Resultado: {'pasa ' + winner if winner else 'ninguna pasa → no se exporta'}.**", ""]
    for k in ("M0",) + VARIANTS:
        L += [f"### {k} por decil del valor mostrado", "", "| decil | n | mostrado (mediana) | cumple | real mediano |",
              "| --- | --- | --- | --- | --- |"]
        L += [f"| {r['dec']} | {r['n']:,} | {r['shown']:.1f} | {r['hit']:.0%} | {r['real']:.1f} |"
              for r in evs[k]["dec"]]
        L += ["", "Trimestres (cumple): " + ", ".join(f"{p} {h:.0%}" for p, _, _, _, h in evs[k]["quarters"]), ""]
    if eng:
        L += ["### Confirmación con el motor de ejecución", "", "| variante | n | cumple | cumple M0 | pinball | pinball M0 |",
              "| --- | --- | --- | --- | --- | --- |"]
        L += [f"| {k} | {e['n']:,} | {e['hit']:.1%} | {e['hit_m0']:.1%} | {e['pin']:.2f} | {e['pin_m0']:.2f} |"
              for k, e in eng.items()]
        # Diagnóstico (no entra al criterio): mismas filas con el label simple de costo fijo
        L += ["", "Mismas filas con el label de costo fijo (Alpha 1.11 %): "
              + ", ".join(f"{k} cumple {(de['y_real'] >= de[k]).mean():.1%}" for k in ("M0",) + VARIANTS)
              + f"; APR real mediano: costo fijo {de['y_real'].median():.1f} vs motor {de['y_eng'].median():.1f}."]
    rep = ROOT / "reports" / f"apr_calibration_{today}.md"
    rep.write_text("\n".join(L) + "\n", encoding="utf-8")
    print("\n".join(L))
    if a.export and winner:
        export(c, winner, evs[winner], today)
    elif a.export:
        print("no se exporta: ninguna variante pasa el criterio")


if __name__ == "__main__":
    main()
