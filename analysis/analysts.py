"""Panel de "analistas" por oportunidad — stdlib, mismo código offline y en prod.

Cada analista mira UNA variable y vota Favorable (+1) / Neutral (0) / Desfavorable (−1)
según cortes (terciles) y dirección medidos fuera de muestra en scripts/analyst_study.py.
Solo los que separan el APR realizado de forma consistente votan (`votes: true`); el resto
se muestra como contexto. El consenso 1–10 es la media de votos ponderada por la fuerza
medida (IC); la tabla de calibración traduce el consenso y la celda de "análogos" a
frecuencias REALES (cuántas veces se cumplió en casos parecidos).

Artefacto: models/analyst_table.json (lo escribe scripts/analyst_study.py --export).
Fallo al cargar / sin datos → None (la UI muestra el resto).
"""
import json
import logging
import os

log = logging.getLogger("bot")

TABLE_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                          "models", "analyst_table.json")
BASE_DAILY = 0.0003          # tasa base Binance (0.01 %/8h) en fracción diaria
_table = None


def load_table(path: str = TABLE_PATH) -> bool:
    global _table
    try:
        with open(path, encoding="utf-8") as f:
            t = json.load(f)
        assert isinstance(t.get("analysts"), dict) and t["analysts"]
        _table = t
        log.info("Analyst table %s cargada (%d analistas)", t.get("version"), len(t["analysts"]))
        return True
    except FileNotFoundError:
        log.info("Analyst table no encontrada (%s)", path)
    except Exception as e:  # noqa: BLE001
        log.warning("Analyst table inválida: %s", e)
    _table = None
    return False


def table():
    return _table


# ── Episodios de cobro (picos anteriores) ─────────────────────────────────
def funding_episodes(rates, timestamps_ms, interval_hours=8.0, base_daily=BASE_DAILY):
    """Rachas de cobro con la política de prod: empiezan cuando la tasa diaria equivalente
    ≥ base y terminan tras dos pagos seguidos ≤ 0 (= CLOSE_SUGGESTED).
    Devuelve [{start_ts, end_ts, days, apr, payments, ended}] en orden temporal."""
    ppd = 24.0 / max(1.0, float(interval_hours or 8))
    eps, cur, neg = [], None, 0
    for r, ts in zip(rates, timestamps_ms):
        r = float(r)
        if cur is None:
            if r * ppd >= base_daily * (1 - 1e-6):
                cur, neg = {"start_ts": int(ts), "sum": r, "n": 1, "last_ts": int(ts)}, 0
            continue
        cur["sum"] += r
        cur["n"] += 1
        cur["last_ts"] = int(ts)
        neg = neg + 1 if r <= 0 else 0
        if neg >= 2:
            eps.append(_close_ep(cur, ppd, True))
            cur, neg = None, 0
    if cur is not None:
        eps.append(_close_ep(cur, ppd, False))
    return eps


def _close_ep(cur, ppd, ended):
    days = cur["n"] / ppd
    return {"start_ts": cur["start_ts"], "end_ts": cur["last_ts"], "days": round(days, 2),
            "apr": round(cur["sum"] / max(days, 1e-9) * 365 * 100, 1), "payments": cur["n"],
            "ended": ended}


def episode_stats(eps, now_ms, lookback_days=120):
    """Mediana de duración de las rachas TERMINADAS en la ventana + edad de la actual."""
    lo = now_ms - lookback_days * 86_400_000
    done = sorted(e["days"] for e in eps if e["ended"] and e["end_ts"] >= lo and e["end_ts"] < now_ms)
    live = next((e for e in reversed(eps) if not e["ended"]), None)
    med = None
    if done:
        k = len(done)
        med = done[k // 2] if k % 2 else (done[k // 2 - 1] + done[k // 2]) / 2
    return {"ep_med_days": med, "ep_count": len(done),
            "ep_age_days": round((now_ms - live["start_ts"]) / 86_400_000, 2) if live else None}


# ── Votos, consenso y calibración ─────────────────────────────────────────
def vote(spec: dict, value):
    """+1 / 0 / −1 con los cortes del analista; None si no hay dato."""
    if value is None:
        return None
    try:
        v = float(value)
    except (TypeError, ValueError):
        return None
    lo, hi = spec["cuts"]
    raw = 1 if v >= hi else (-1 if v <= lo else 0)
    return raw * (1 if spec.get("dir", 1) >= 0 else -1)


def consensus(votes: dict, analysts: dict):
    """Media ponderada de votos de los analistas que votan → escala 1–10 (5.5 = neutro)."""
    num = den = 0.0
    for k, v in votes.items():
        a = analysts.get(k) or {}
        if v is None or not a.get("votes"):
            continue
        w = float(a.get("weight", 0))
        num += w * v
        den += w
    if den <= 0:
        return None
    return round(5.5 + 4.5 * num / den, 1)


def consensus_label(score):
    if score is None:
        return None
    if score >= 7.5:
        return "Muy favorable"
    if score >= 6.0:
        return "Favorable"
    if score > 5.0:
        return "Neutral"
    if score > 3.5:
        return "Desfavorable"
    return "Muy desfavorable"


def size_mult(score, tbl=None):
    """Capital sugerido por consenso (1.25× ≥ 6, 0.75× ≤ 5, 1× entre medio) — solo si el
    estudio lo validó (`size_by_consensus`: ≥ sizing por ancho relativo en los trimestres)."""
    tbl = tbl or _table
    if score is None or not tbl or not tbl.get("size_by_consensus"):
        return None
    return 1.25 if score >= 6 else (0.75 if score <= 5 else 1.0)


def _bucket(rows, x):
    for r in rows:
        if r["lo"] <= x < r["hi"]:
            return r
    return None


def calibrate(score, tbl=None):
    """Frecuencias reales de los casos con consenso en el mismo tramo."""
    tbl = tbl or _table
    if score is None or not tbl:
        return None
    return _bucket(tbl.get("consensus_calib") or [], score)


def analog_cell(pred, alpha: bool, rel_width, tbl=None):
    """Resultado realizado de setups parecidos: celda (quintil de predicción × venue × ancho)."""
    tbl = tbl or _table
    if pred is None or not tbl or not tbl.get("analogs"):
        return None
    an = tbl["analogs"]
    pi = sum(pred >= c for c in an["pred_cuts"])
    wi = sum(rel_width >= c for c in an["relw_cuts"]) if rel_width is not None else "x"
    cells = an["cells"]
    return (cells.get(f"a{int(bool(alpha))}_p{pi}_w{wi}")
            or cells.get(f"a{int(bool(alpha))}_p{pi}_wx"))


def evaluate(values: dict, tbl=None):
    """values = {feature: valor}. Devuelve {analysts:[…], consensus, label, calib}."""
    tbl = tbl or _table
    if not tbl:
        return None
    out, votes = [], {}
    for key, spec in tbl["analysts"].items():
        val = values.get(spec["feature"])
        v = vote(spec, val)
        votes[key] = v
        out.append({"key": key, "label": spec.get("label", key), "feature": spec["feature"],
                    "value": None if val is None else round(float(val), 4), "vote": v,
                    "votes": bool(spec.get("votes")), "weight": spec.get("weight"),
                    "cuts": spec["cuts"], "dir": spec.get("dir", 1), "why": spec.get("why", "")})
    score = consensus(votes, tbl["analysts"])
    return {"analysts": out, "consensus": score, "label": consensus_label(score),
            "calib": calibrate(score, tbl)}
