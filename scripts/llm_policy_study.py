#!/usr/bin/env python3
"""Piloto: Claude como decisor (entradas, tamaño, salidas) vs el modelo ML (P0) — LOCAL, read-only.

Mismo motor que profit_study (scripts/_exec_engine.py + profit_study.lifecycle: espera de basis, tramos
cada 15 min, SL al 80 % del camino a liquidación, venta límite Kyber, gas). Cartera de 10,000 USD,
5 cupos, 1x (nocional 1,000 USD por cupo). Universo: candidatos del modelo (`p_prod` > 0) en los 25 tokens
solo-Alpha con velas 1m. Ventana 2026-07-01 → fin del caché (posterior al corte de conocimiento del LLM);
entradas hasta 30 d antes del fin.

Brazos
  P0      modelo + reglas vigentes de profit_study (`variants()[0]`): entra en cada liquidación, salida B
          (2 pagos ≤ 0, tope 30 d). Es `profit_study.portfolio()` sobre la ventana.
  P0d     igual, pero decide una vez al día a las 00:04 UTC (mismo reloj que el LLM).
  RND     al azar entre los candidatos del día (5 semillas), salida B.
  LLM     Claude decide entradas, tamaño (0.5–1.5 del cupo) y salidas, sin la predicción del modelo.
  LLM+ML  igual, con `p_prod` visible.
El SL y el tope de 30 d los manda el motor; la ejecución (espera, tramos) también.

Criterio (fijado antes de correr): el LLM "mejora" si su rentabilidad mensual media supera a P0 sin
empeorar el p10 mensual ni el p5 por operación. Con ~4 meses el resultado es direccional.

LLM: Claude Code headless (`claude -p`) con la suscripción del usuario — sin API key. Cada respuesta se
guarda en cache/llm_policy/<sha256>.json → se reanuda donde quedó si se topa el límite.

    python -u scripts/llm_policy_study.py --verify             # P0 = portfolio() de profit_study
    python -u scripts/llm_policy_study.py --arm LLM --model haiku --days 5     # piloto
    python -u scripts/llm_policy_study.py --arm LLM --model claude-opus-5-5 [--rep 2]
    python -u scripts/llm_policy_study.py --report             # → reports/llm_policy_pilot_YYYYMMDD.md
"""
import argparse
import hashlib
import json
import os
import pickle
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))
import _exec_engine as E  # noqa: E402
import profit_study as PS  # noqa: E402

WIN0 = pd.Timestamp("2026-07-01", tz="UTC")
CAPITAL, SLOTS, LEV = 10_000.0, 5, 1.0
SLOT_USD = CAPITAL / SLOTS * LEV / (LEV + 1)         # 1,000 USD
DEC_MIN = 4                                           # decisión diaria a las 00:04 UTC
CAP_MIN = 30 * 1440                                   # tope de 30 d (motor)
TAIL = 4 * 2880 + 240                                 # margen de datos tras la salida (igual que profit_study)
DAY_MS, MIN_MS = 86_400_000, 60_000
V0 = PS.variants()[0]
POL = V0.pol
LLM_DIR = ROOT / "cache" / "llm_policy"
ROWS_CACHE = LLM_DIR / "rows.pkl"
RUNS = LLM_DIR / "runs"
MONTHS = [m for m in PS.MONTHS if m >= "2026-05"]   # 2 meses de historia para 30 liquidaciones y vol7
# LLM_CLAUDE_BIN permite usar otro binario (p. ej. el de la app de escritorio: Opus 5.5 exige CLI ≥ 2.1.280).
CLAUDE = Path(os.environ.get("LLM_CLAUDE_BIN")
              or Path(os.path.expanduser("~")) / ".local" / "bin" / ("claude.exe" if os.name == "nt" else "claude"))
SIZE_MIN, SIZE_MAX = 0.5, 1.5


# ── Datos ───────────────────────────────────────────────────────────────
def all_rows():
    """Filas ml_v2 de los tokens Alpha con velas 1m, con p_prod (sin filtrar). Cacheado."""
    if ROWS_CACHE.exists():
        return pd.read_pickle(ROWS_CACHE)
    import joblib
    from analysis.ml_features import FEATURE_NAMES
    syms = E.cached_symbols()
    df = pd.read_pickle(PS.ROWS)
    df = df[df["symbol"].isin(syms.keys()) & (df["alpha"] == 1)].copy()
    bundle = joblib.load(ROOT / "models" / "scoring_model.joblib")
    df["p_prod"] = bundle["model"].predict(df[FEATURE_NAMES].to_numpy())
    df = df[["symbol", "funding_ts", "t", "p_prod", "daily_rate", "fund_H30", "hold_H30"]]
    LLM_DIR.mkdir(parents=True, exist_ok=True)
    df.to_pickle(ROWS_CACHE)
    return df


def load_syms(symbols):
    syms = E.cached_symbols()
    funding, cost = E.funding_table(syms), E.cost_table()
    out = {}
    for s in sorted(symbols):
        if s not in syms or s not in funding:
            continue
        full = E.load(s, [m for m in syms[s] if m in PS.MONTHS])     # mismo filtro que profit_study.work
        if full is None or len(full["ts"]) < 40000:
            continue
        F = E.Sym(s, full, funding[s], cost(s))
        with np.errstate(invalid="ignore"):
            if np.nanmean(np.abs(F.pc / F.sc - 1) * 100) > 3:
                continue
        del full, F
        d = E.load(s, [m for m in syms[s] if m in MONTHS])
        if d is None:
            continue
        out[s] = E.Sym(s, d, funding[s], cost(s))
    return out


def data_end(S_all):
    """Último instante de decisión válido: datos hasta S.n − TAIL en la mayoría de los tokens."""
    ends = [int(S.ts[S.n - TAIL - 1]) for S in S_all.values()]
    return int(np.median(ends))


# ── P0: portfolio() de profit_study en la ventana ──────────────────────
def p0_trades(rows, S_all, t_lo, t_hi):
    out = []
    cand = rows[(rows.p_prod > 0) & (rows.funding_ts >= t_lo) & (rows.funding_ts <= t_hi)]
    for c in cand.itertuples():
        S = S_all.get(c.symbol)
        if S is None:
            continue
        t = int(np.searchsorted(S.ts, int(c.funding_ts)))
        if t < 10080 or t >= S.n - TAIL:
            continue
        hold = PS.exit_minutes(S, t, V0.k_neg, V0.min_days, V0.cap_days)
        if hold is None or t + hold >= S.n - TAIL:
            continue
        r = PS.lifecycle(S, t, SLOT_USD, V0, hold)
        if r is None:
            continue
        out.append(trade_row(S, c.symbol, t, SLOT_USD, 1.0, c.p_prod, r))
    return pd.DataFrame(out)


def trade_row(S, sym, t, usd, size, pp, r):
    return {"sym": sym, "t_ms": int(S.ts[t]), "usd": usd, "size": size, "p_prod": pp,
            "in_ms": int(S.ts[r["t_in"]]), "out_ms": int(S.ts[min(r["t_out"], S.n - 1)]),
            **{k: r[k] for k in ("net", "entry", "exit", "fund", "frac", "case", "gas")}}


def p0_select(trades):
    """Misma selección que profit_study.portfolio (cupos, un token a la vez, cupo reservado 6 h)."""
    tr = trades.sort_values(["t_ms", "p_prod"], ascending=[True, False])
    busy, held, keep = [], {}, []
    for r in tr.itertuples():
        busy = [b for b in busy if b > r.t_ms]
        held = {s: e for s, e in held.items() if e > r.t_ms}
        if len(busy) >= SLOTS or r.sym in held:
            continue
        end = r.out_ms if r.frac > 0 else r.t_ms + 6 * 3_600_000
        busy.append(end)
        held[r.sym] = end
        keep.append(r.Index)
    return tr.loc[keep]


# ── Estado diario ───────────────────────────────────────────────────────
def k_at(S, ms):
    return int(np.searchsorted(S.ts, ms, "right")) - 1


def last_valid(arr, k, back=60):
    lo = max(0, k - back)
    seg = arr[lo:k + 1]
    ok = np.where(~np.isnan(seg))[0]
    return float(seg[ok[-1]]) if len(ok) else None


def settlements(S, ms, n=30):
    i = int(np.searchsorted(S.f_ts, ms, "right"))
    return S.daily[max(0, i - n):i], S.f_ts[max(0, i - n):i]


def r3(x, d=3):
    return None if x is None or (isinstance(x, float) and not np.isfinite(x)) else round(float(x), d)


def cand_features(S, k, usd):
    """Lo que ve el decisor de un candidato (sin nombre, sin precios)."""
    ms = int(S.ts[k])
    d30, f_ts = settlements(S, ms)
    hours = float(np.median(np.diff(f_ts)) / 3_600_000) if len(f_ts) > 2 else None
    streak = 0
    for v in d30[::-1]:
        if v <= 0:
            break
        streak += 1
    daily = S.pc[max(0, k - 10080):k + 1:1440]
    vol7 = float(np.std(np.diff(np.log(daily)))) * 100 if len(daily) > 3 else None
    with np.errstate(invalid="ignore"):
        rng24 = float(np.nanmax(S.ph[k - 1440:k]) / np.nanmin(S.pl[k - 1440:k]) - 1) * 100
    n = E.n_slices(S, usd, POL)
    c = S.a + S.b * usd / n
    ex = S.ex("buy", c)
    w = ex[k - 1440:k + 1]
    w = w[~np.isnan(w)]
    return {
        "tasa_ult_pct_dia": r3(d30[-1]) if len(d30) else None,
        "liq30": {"media": r3(np.mean(d30)), "mediana": r3(np.median(d30)), "min": r3(np.min(d30)),
                  "max": r3(np.max(d30)), "pct_pos": r3(np.mean(d30 > 0) * 100, 0), "racha_pos": streak,
                  "ult6": [r3(x) for x in d30[-6:]], "intervalo_h": r3(hours, 0)} if len(d30) else None,
        "vol7_pct": r3(vol7, 2), "recorrido24h_pct": r3(rng24, 1),
        "basis_compra_ahora_pct": r3(last_valid(ex, k)),
        "basis_24h": {"mediana": r3(np.median(w)) if len(w) else None,
                      "pct_min_sobre_umbral": r3(np.mean(w >= POL.th_e) * 100, 0) if len(w) else None},
        "costo": {"comision_pct": r3(S.a), "impacto_pct_por_1000usd": r3(S.b * 1000),
                  "tramos": n, "ida_vuelta_est_pct": r3(2 * c + 2 * E.PERP_COST, 2)},
    }


class Pos:
    def __init__(self, pid, sym, S, t, usd, size, pp):
        self.pid, self.sym, self.S, self.t, self.usd, self.size, self.pp = pid, sym, S, t, usd, size, pp
        res = E.leg(S, t, "buy", usd, POL)
        self.filled = bool(res and res[0])
        self.result, self.out_ms, self.exit_req = None, None, None
        if not self.filled:
            self.frac = 0.0
            self.out_ms = int(S.ts[t]) + 6 * 3_600_000
            self.result = {"net": 0.0, "entry": 0.0, "exit": 0.0, "fund": 0.0, "frac": 0.0, "case": 0,
                           "gas": 0.0, "t_in": t, "t_out": t}
            return
        fills, n = res
        self.frac = len(fills) / n
        js = np.array([j for j, _ in fills])
        cs = np.array([c for _, c in fills])
        self.SE = float(np.mean(S.sc[js] * (1 + cs / 100)))
        self.PE = float(np.mean(S.pc[js] * (1 - E.PERP_COST / 100)))
        self.G0 = float(np.mean(S.G[js]))
        self.k_in = int(js[-1]) + 1
        self.in_ms = int(S.ts[js[0]])
        liq = self.PE * (1 + 1 / LEV) / (1 + E.MMR)
        self.sl = self.PE + POL.F * (liq - self.PE)

    def close(self, hold):
        """Fija la salida (pedida, tope o fin de datos); el motor resuelve SL/espera/tramos."""
        S = self.S
        hold = int(min(hold, CAP_MIN, S.n - TAIL - 1 - self.t))
        r = PS.lifecycle(S, self.t, self.usd, V0, hold)
        if r is None:
            raise RuntimeError(f"lifecycle None {self.sym} t={self.t} hold={hold}")
        self.result, self.out_ms, self.exit_req = r, int(S.ts[min(r["t_out"], S.n - 1)]), hold

    def sl_fired(self, k):
        return self.filled and E.protect(self.S, self.k_in, k, self.PE, self.SE, self.frac * self.usd,
                                         POL, LEV) is not None

    def view(self, k):
        S = self.S
        ms = int(S.ts[k])
        days = (ms - self.in_ms) / DAY_MS
        i0 = int(np.searchsorted(S.f_ts, self.in_ms, "right"))
        i1 = int(np.searchsorted(S.f_ts, ms, "right"))
        paid = S.daily[i0:i1]
        fund = (S.G[k] - self.G0) / self.PE * 100 * self.frac
        n = E.n_slices(S, self.usd * self.frac, POL)
        c = S.a + S.b * self.usd * self.frac / n
        bx = last_valid(S.ex("sell", c), k)
        spot = float(S.scf[k])
        price = (spot / self.SE - S.pc[k] / self.PE) * 100 * self.frac
        path = (S.mh[self.k_in:k + 1].max() - self.PE) / (self.sl - self.PE) * 100 if k >= self.k_in else 0.0
        d30, _ = settlements(S, ms)
        return {"id": self.pid, "dias": r3(days, 1), "tam_cupo": self.size, "llenado_pct": r3(self.frac * 100, 0),
                "funding_cobrado_pct": r3(fund, 2), "pagos": int(len(paid)), "pagos_no_pos": int((paid <= 0).sum()),
                "ult_pagos_pct_dia": [r3(x) for x in paid[-6:]],
                "liq30_media_pct_dia": r3(np.mean(d30)) if len(d30) else None,
                "pnl_precio_mercado_pct": r3(price, 2), "basis_venta_ahora_pct": r3(bx),
                "camino_sl_pct": r3((S.mh[k] - self.PE) / (self.sl - self.PE) * 100, 0),
                "camino_sl_max_pct": r3(path, 0),
                "neto_est_si_sale_pct": r3(fund + price - (c + E.PERP_COST) * self.frac, 2)}


# ── LLM ─────────────────────────────────────────────────────────────────
SYSTEM = """Eres el gestor de una cartera de arbitraje de funding (simulación diaria). Estrategia: compras el token spot \
on-chain y abres un short del perpetuo del mismo token por el mismo nocional (delta neutro, 1x). Cobras el funding \
cuando la tasa es positiva y lo pagas cuando es negativa. Capital 10,000 USD en 5 cupos; un cupo = 1,000 USD de \
nocional (la otra mitad es margen del short). Un token a la vez.

Cada día (00:04 UTC) decides:
- enter: qué candidatos abrir y con qué tamaño (0.5 a 1.5 cupos). La suma de tamaños abiertos no puede pasar de 5 \
cupos ni haber más de 5 posiciones. Puedes no abrir nada.
- exit: qué posiciones cerrar hoy.

Lo que hace el motor (no lo controlas):
- Entrada: espera hasta 6 h a que el basis ejecutable (perp sobre spot, neto de costo) sea ≥ 0.25 %; si no llega, \
no entra y el cupo se libera. Compra en tramos de ≤ 0.15 % de impacto cada 15 min.
- Salida: espera hasta 24 h a que el basis de venta sea ≥ −0.15 %, luego fuerza. Tramos cada 15 min.
- Stop-loss del short al 80 % del camino a la liquidación (precio del perp ≈ +72 % sobre la entrada); si salta, \
cierra todo. Tope de 30 días por posición.
- Costos: comisión del pool + impacto (a + b·USD) por swap, 0.05 % por lado en el perp, gas.

Unidades: tasas de funding en % por día (0.10 = 0.10 %/día ≈ 36.5 % anual). Basis y costos en %. Ganancia neta de \
una posición = funding cobrado + PnL de precio (basis de entrada y salida, impacto) − costos. Objetivo: maximizar \
la ganancia neta de la cartera (rentabilidad mensual) sin colas de pérdida grandes.

Responde SOLO con el JSON pedido: {"enter": [{"id": "T03", "size": 1.0}], "exit": ["P4"], "why": "≤ 60 palabras"}."""

SCHEMA = {"type": "object", "additionalProperties": False,
          "properties": {"enter": {"type": "array", "items": {
              "type": "object", "additionalProperties": False,
              "properties": {"id": {"type": "string"}, "size": {"type": "number"}}, "required": ["id", "size"]}},
              "exit": {"type": "array", "items": {"type": "string"}},
              "why": {"type": "string"}},
          "required": ["enter", "exit", "why"]}


def _clean_env():
    drop = ("CLAUDE", "ANTHROPIC", "AI_AGENT", "BAGGAGE")
    env = {k: v for k, v in os.environ.items() if not k.upper().startswith(drop)}
    # Antivirus con inspección HTTPS (Kaspersky): el CLI necesita su CA raíz.
    ca = Path.home() / ".claude" / "kaspersky_root.pem"
    if "NODE_EXTRA_CA_CERTS" not in env and ca.exists():
        env["NODE_EXTRA_CA_CERTS"] = str(ca)
    return env


class LimitHit(RuntimeError):
    pass


def ask(prompt, model, rep=1, effort=None):
    """Respuesta del LLM (dict) — caché en disco por hash del prompt. Suscripción vía `claude -p`."""
    key = hashlib.sha256(json.dumps([model, rep, effort, SYSTEM, prompt]).encode()).hexdigest()
    path = LLM_DIR / f"{key}.json"
    if path.exists():
        return json.loads(path.read_text(encoding="utf-8"))
    cwd = Path(tempfile.gettempdir()) / "llm_policy_cwd"      # fuera del repo: sin CLAUDE.md del proyecto
    cwd.mkdir(exist_ok=True)
    cmd = [str(CLAUDE), "-p", "--model", model, "--output-format", "json", "--json-schema", json.dumps(SCHEMA),
           "--tools", "", "--strict-mcp-config", "--setting-sources", "local", "--no-session-persistence",
           "--system-prompt", SYSTEM]
    if effort:
        cmd += ["--effort", effort]
    last = None
    for attempt in range(3):
        t0 = time.time()
        try:
            p = subprocess.run(cmd, input=prompt, capture_output=True, text=True, encoding="utf-8", cwd=cwd,
                               env=_clean_env(), timeout=600)
        except subprocess.TimeoutExpired:
            last = "timeout"
            continue
        try:
            out = json.loads(p.stdout)
        except Exception:
            last = (p.stdout or p.stderr)[:300]
            continue
        msg = str(out.get("result", ""))
        if out.get("is_error"):
            low = msg.lower()
            if any(w in low for w in ("limit", "not logged", "login", "quota", "credit")):
                raise LimitHit(msg[:300])
            last = msg[:300]
            time.sleep(10 * (attempt + 1))
            continue
        dec = out.get("structured_output")
        if dec is None:
            try:
                txt = msg[msg.find("{"):msg.rfind("}") + 1]
                dec = json.loads(txt)
            except Exception:
                last = msg[:300]
                continue
        rec = {"decision": dec, "model": model, "rep": rep, "secs": round(time.time() - t0, 1),
               "usage": out.get("usage"), "cost_usd_equiv": out.get("total_cost_usd"),
               "model_usage": out.get("modelUsage")}
        path.write_text(json.dumps(rec, ensure_ascii=False), encoding="utf-8")
        return rec
    raise RuntimeError(f"LLM sin respuesta válida: {last}")


# ── Simulación diaria ───────────────────────────────────────────────────
def day_candidates(rows_by_sym, S_all, dms, busy_syms, entry_end):
    """Candidatos del día: última fila por token en las 24 h previas con p_prod > 0."""
    if dms > entry_end:
        return []
    out = []
    for sym, g in rows_by_sym.items():
        S = S_all.get(sym)
        if S is None or sym in busy_syms:
            continue
        i = int(np.searchsorted(g.funding_ts.to_numpy(), dms, "right")) - 1
        if i < 0:
            continue
        r = g.iloc[i]
        if r.funding_ts < dms - DAY_MS or r.p_prod <= 0:
            continue
        k = k_at(S, dms)
        if k < 10080 or k + CAP_MIN >= S.n - TAIL:
            continue
        out.append((sym, k, float(r.p_prod)))
    return out


def simulate(arm, rows, S_all, end_ms, model=None, rep=1, seed=0, max_days=None, effort=None, verbose=True):
    """Corre un brazo diario (P0d, RND, LLM, LLM+ML). → (trades DataFrame, log de decisiones)."""
    rows = rows[rows.symbol.isin(S_all)]
    rows_by_sym = {s: g.sort_values("funding_ts") for s, g in rows.groupby("symbol")}
    entry_end = end_ms - CAP_MIN * MIN_MS
    rng = np.random.default_rng(seed)
    positions, log, pid = [], [], 0
    day0 = int(WIN0.value // 1_000_000)
    n_days = int((end_ms - day0) // DAY_MS) + 1
    for d in range(n_days):
        if max_days is not None and d >= max_days:
            break
        dms = day0 + d * DAY_MS + DEC_MIN * MIN_MS
        if dms > end_ms:
            break
        # 1) el motor: SL disparado, tope 30 d o fin de datos
        for p in positions:
            if p.result is not None:
                continue
            k = k_at(p.S, dms)
            if p.sl_fired(k) or k - p.t >= CAP_MIN or k >= p.S.n - TAIL - 1:
                p.close(k - p.t)
        live = [p for p in positions if p.out_ms is None or p.out_ms > dms]
        open_ = [p for p in live if p.result is None]
        if dms > entry_end and not open_:
            if all(p.out_ms <= dms for p in positions):
                break
            continue
        busy_syms = {p.sym for p in live}
        cands = day_candidates(rows_by_sym, S_all, dms, busy_syms, entry_end)
        used = sum(p.size for p in live)
        free = SLOTS - len(live)
        enters, exits, why = [], [], ""
        if arm in ("P0d", "RND"):
            if arm == "P0d":
                cands = sorted(cands, key=lambda c: -c[2])
            else:
                cands = [cands[i] for i in rng.permutation(len(cands))]
            enters = [(c, 1.0) for c in cands[:max(0, free)]]
        else:
            if not cands and not open_:
                continue
            # ids anónimos en orden aleatorio fijo por día (no por p_prod)
            perm = np.random.default_rng(int(dms // DAY_MS)).permutation(len(cands)) if cands else []
            ids = {f"T{i + 1:02d}": cands[j] for i, j in enumerate(perm)}
            cview = []
            for tid, (sym, k, pp) in ids.items():
                f = {"id": tid, **cand_features(S_all[sym], k, SLOT_USD)}
                if arm == "LLM+ML":
                    f["pred_modelo_apr_neto_pct"] = r3(pp, 1)
                cview.append(f)
            pview = [p.view(k_at(p.S, dms)) for p in open_]
            closing = len([p for p in live if p.result is not None])
            done = [p for p in positions if p.result is not None and p.out_ms <= dms and p.filled]
            recent = [{"dias": r3((p.out_ms - p.in_ms) / DAY_MS, 1), "neto_pct": r3(p.result["net"], 2),
                       "sl": p.result["case"] > 1} for p in sorted(done, key=lambda p: p.out_ms)[-5:]]
            state = {"dia": d + 1, "entradas_permitidas": dms <= entry_end,
                     "cupos_libres": free, "cupos_en_uso": round(used, 2), "posiciones_en_cierre": closing,
                     "cerradas": {"n": len(done), "neto_usd": r3(sum(p.result["net"] / 100 * p.usd for p in done), 1),
                                  "ultimas": recent},
                     "posiciones_abiertas": pview, "candidatos": cview}
            prompt = ("Estado de la cartera hoy (JSON). Decide entradas y salidas.\n"
                      + json.dumps(state, ensure_ascii=False, separators=(",", ":")))
            rec = ask(prompt, model, rep, effort)
            dec = rec["decision"]
            why = dec.get("why", "")
            open_ids = {p.pid: p for p in open_}
            exits = [open_ids[x] for x in dict.fromkeys(dec.get("exit") or []) if x in open_ids]
            budget_n, budget_s = free + len(exits), SLOTS - used + sum(p.size for p in exits)
            for e in dec.get("enter") or []:
                c = ids.get(str(e.get("id")))
                if c is None or c[0] in {x[0][0] for x in enters}:
                    continue
                size = float(np.clip(float(e.get("size") or 1.0), SIZE_MIN, SIZE_MAX))
                if budget_n < 1 or size > budget_s + 1e-9:
                    continue
                enters.append((c, size))
                budget_n -= 1
                budget_s -= size
            log.append({"dia": d + 1, "ms": dms, "n_cand": len(cands), "n_open": len(open_),
                        "enter": [(ids_key(ids, c), s) for c, s in enters], "exit": [p.pid for p in exits],
                        "why": why, "secs": rec.get("secs"), "usage": rec.get("usage"),
                        "cost": rec.get("cost_usd_equiv")})
            if verbose:
                print(f"  día {d + 1:3d}: cand {len(cands):2d} abiertas {len(open_)} → entra "
                      f"{[ids_key(ids, c) for c, _ in enters]} sale {[p.pid for p in exits]} | {why[:90]}", flush=True)
            for p in exits:
                p.close(k_at(p.S, dms) - p.t)
        for (sym, k, pp), size in enters:
            pid += 1
            S = S_all[sym]
            p = Pos(f"P{pid}", sym, S, k, SLOT_USD * size, size, pp)
            if p.filled and arm in ("P0d", "RND"):
                hold = PS.exit_minutes(S, k, V0.k_neg, V0.min_days, V0.cap_days)
                p.close(hold if hold is not None else CAP_MIN)
            positions.append(p)
    # cierre de lo que quede (solo si se cortó con --days): desde el corte manda la salida B del motor
    # (la misma de P0d); si su salida ya pasó, se cierra en el corte.
    cut_ms = day0 + (max_days or n_days) * DAY_MS + DEC_MIN * MIN_MS
    for p in positions:
        if p.result is None:
            el = k_at(p.S, min(cut_ms, end_ms, int(p.S.ts[p.S.n - TAIL - 2]))) - p.t
            rule = PS.exit_minutes(p.S, p.t, V0.k_neg, V0.min_days, V0.cap_days)
            p.close(max(el, rule if rule is not None else CAP_MIN))
    trades = pd.DataFrame([trade_row(p.S, p.sym, p.t, p.usd, p.size, p.pp, p.result) for p in positions])
    return trades, log


def ids_key(ids, c):
    for k, v in ids.items():
        if v is c:
            return k
    return None


# ── Métricas ────────────────────────────────────────────────────────────
def stats(trades, t0, t1, t_end=None):
    """Cartera sobre capital total: mensual por mes de cierre, por operación, SL, utilización, DD, semanas.
    t1 = fin de la ventana de entradas (normaliza y mide utilización); t_end = último cierre posible."""
    t_end = t_end or t1
    f = trades[trades.frac > 0].copy()
    f["usd_pnl"] = f.net / 100 * f.usd
    months = pd.period_range(pd.to_datetime(t0, unit="ms"), pd.to_datetime(t_end, unit="ms"), freq="M").strftime("%Y-%m")
    f["m"] = pd.to_datetime(f.out_ms, unit="ms", utc=True).dt.strftime("%Y-%m")
    monthly = f.groupby("m").usd_pnl.sum().reindex(months, fill_value=0.0) / CAPITAL * 100
    days = (t1 - t0) / DAY_MS
    used = sum(r.size * (min(r.out_ms, t1) - max(r.in_ms, t0)) for r in f.itertuples() if r.out_ms > r.in_ms) / DAY_MS
    eq = CAPITAL + f.sort_values("out_ms").usd_pnl.cumsum()
    dd = float(((eq.cummax() - eq) / eq.cummax()).max() * 100) if len(eq) else 0.0
    wk = ((f.out_ms - t0) // (7 * DAY_MS)).astype(int)
    weekly = f.groupby(wk).usd_pnl.sum().reindex(range(int((t_end - t0) // (7 * DAY_MS)) + 3), fill_value=0.0)
    return {"monthly": monthly, "norm_m": float(f.usd_pnl.sum() / CAPITAL * 100 / (days / 30.44)),
            "mean_m": float(monthly.mean()), "p10_m": float(monthly.quantile(0.1)),
            "p10_w": float(weekly.quantile(0.1) / CAPITAL * 100 * 30.44 / 7),
            "n": len(f), "tried": len(trades), "mean_t": float(f.net.mean()) if len(f) else np.nan,
            "p5_t": float(np.percentile(f.net, 5)) if len(f) else np.nan,
            "med_t": float(f.net.median()) if len(f) else np.nan,
            "sl": int((f.case > 1).sum()), "sl_pct": float((f.case > 1).mean() * 100) if len(f) else 0.0,
            "util": used / (days * SLOTS) * 100, "dd": dd, "usd": float(f.usd_pnl.sum()),
            "days_med": float(((f.out_ms - f.in_ms) / DAY_MS).median()) if len(f) else np.nan,
            "size_mean": float(f["size"].mean()) if len(f) else np.nan, "weekly": weekly}


def boot_weekly(wa, wb, n=5000, seed=7):
    """IC 95 % de la diferencia de PnL semanal medio (a − b), en % mensual del capital."""
    d = (wa - wb).to_numpy() / CAPITAL * 100 * (30.44 / 7)
    rng = np.random.default_rng(seed)
    bs = d[rng.integers(0, len(d), (n, len(d)))].mean(1)
    return float(d.mean()), float(np.percentile(bs, 2.5)), float(np.percentile(bs, 97.5)), float((bs > 0).mean() * 100)


# ── CLI ─────────────────────────────────────────────────────────────────
def setup():
    rows = all_rows()
    S_all = load_syms(rows.symbol.unique())
    end_ms = data_end(S_all)
    return rows, S_all, end_ms


def verify(rows, S_all, end_ms):
    t0 = int(WIN0.value // 1_000_000)
    entry_end = end_ms - CAP_MIN * MIN_MS
    mine = p0_trades(rows, S_all, t0, entry_end)
    ref = pd.read_pickle(ROOT / "cache" / "profit_study.pkl")
    ref = ref[(ref.vi == 0) & (ref.usd == PS.REF_USD) & (ref.t_ms >= t0) & (ref.t_ms <= entry_end)]
    a = PS.portfolio(mine, CAPITAL, SLOTS, LEV)
    b = PS.portfolio(ref, CAPITAL, SLOTS, LEV)
    j = mine.set_index(["sym", "t_ms"])[["net"]].join(ref.set_index(["sym", "t_ms"])[["net"]], rsuffix="_ref",
                                                      how="outer")
    print(f"Ventana {WIN0:%Y-%m-%d} → entradas hasta {pd.to_datetime(entry_end, unit='ms'):%Y-%m-%d}, "
          f"fin {pd.to_datetime(end_ms, unit='ms'):%Y-%m-%d %H:%M}")
    print(f"operaciones: script {len(mine)} / profit_study {len(ref)}; solo en una: {int(j.isna().any(axis=1).sum())}; "
          f"|Δ net| máx {float((j.net - j.net_ref).abs().max()):.2e}")
    for name, p in (("script", a), ("profit_study", b)):
        print(f"  portfolio {name}: media {p['mean_m']:+.3f} %/mes p10 {p['p10_m']:+.3f} trades {p['trades']} "
              f"util {p['util']:.1f} % DD {p['dd']:.2f}")
    ok = a["trades"] == b["trades"] and abs(a["mean_m"] - b["mean_m"]) < 1e-9
    sel = p0_select(mine)
    s = stats(sel, t0, entry_end)
    print(f"  p0_select + stats (mismos meses): media {s['mean_m']:+.3f} trades {s['n']} → "
          f"{'OK' if abs(s['mean_m'] - a['mean_m']) < 1e-9 and s['n'] == a['trades'] else 'DIFIERE'}")
    print("VERIFICACIÓN:", "OK" if ok else "DIFIERE")
    return mine


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--verify", action="store_true")
    ap.add_argument("--arm", choices=["P0d", "RND", "LLM", "LLM+ML"])
    ap.add_argument("--model", default="claude-opus-5-5")
    ap.add_argument("--effort", default=None)
    ap.add_argument("--rep", type=int, default=1)
    ap.add_argument("--seed", type=int, default=0)
    ap.add_argument("--days", type=int, default=None, help="piloto: solo N días")
    ap.add_argument("--report", action="store_true")
    a = ap.parse_args()
    sys.stdout.reconfigure(encoding="utf-8")
    rows, S_all, end_ms = setup()
    RUNS.mkdir(parents=True, exist_ok=True)
    if a.verify:
        mine = verify(rows, S_all, end_ms)
        mine.to_pickle(RUNS / "P0.pkl")
        return
    if a.arm:
        tag = f"{a.arm}_{a.model}_r{a.rep}" if a.arm.startswith("LLM") else f"{a.arm}_s{a.seed}"
        if a.days:
            tag += f"_d{a.days}"
        try:
            tr, log = simulate(a.arm, rows, S_all, end_ms, model=a.model, rep=a.rep, seed=a.seed,
                               max_days=a.days, effort=a.effort)
        except LimitHit as e:
            print("LÍMITE / SESIÓN:", e, "— reanudar con el mismo comando (las respuestas quedan en caché).")
            sys.exit(2)
        with open(RUNS / f"{tag}.pkl", "wb") as f:
            pickle.dump({"trades": tr, "log": log, "end_ms": end_ms}, f)
        t0 = int(WIN0.value // 1_000_000)
        s = stats(tr, t0, end_ms)
        print(f"{tag}: {s['n']} operaciones, {s['usd']:+.1f} USD, {s['norm_m']:+.2f} %/mes, p5 op {s['p5_t']:+.2f}, "
              f"SL {s['sl']}, util {s['util']:.0f} %")
        return
    if a.report:
        report(end_ms, cut=a.days)


def _fmt(x, d=2, sign=True):
    if x is None or (isinstance(x, float) and not np.isfinite(x)):
        return "—"
    return f"{x:+.{d}f}" if sign else f"{x:.{d}f}"


def report(end_ms, cut=None):
    """cut = N: compara solo entradas de los primeros N días (corridas `--days N`); el resto sale por la regla B."""
    t0 = int(WIN0.value // 1_000_000)
    entry_end = end_ms - CAP_MIN * MIN_MS
    cut_ms = t0 + cut * DAY_MS + DEC_MIN * MIN_MS if cut else None
    arms = {}
    p0 = pd.read_pickle(RUNS / "P0.pkl")
    if cut:
        p0 = p0[p0.t_ms <= cut_ms]
    arms["P0"] = {"trades": p0_select(p0), "log": []}
    for f in sorted(RUNS.glob("*.pkl")):
        last = f.stem.split("_")[-1]
        if f.stem == "P0" or (last != f"d{cut}" if cut else last.startswith("d")):
            continue
        with open(f, "rb") as fh:
            arms[f.stem[: -len(last) - 1] if cut else f.stem] = pickle.load(fh)
    t1 = cut_ms or end_ms
    st = {k: stats(v["trades"], t0, t1, end_ms) for k, v in arms.items()}
    if cut:  # sin meses completos suficientes: criterio sobre %/mes normalizado y p10 semanal
        for s in st.values():
            s["mean_m"], s["p10_m"] = s["norm_m"], s["p10_w"]
    months = list(st["P0"]["monthly"].index)
    p10l = "p10 semana" if cut else "p10 mes"
    today = datetime.now(timezone.utc).strftime("%Y%m%d")
    L = [f"# Piloto: Claude como decisor vs modelo ML (P0) — {today}", "",
         f"Ventana {WIN0:%Y-%m-%d} → {pd.to_datetime(end_ms, unit='ms'):%Y-%m-%d} (posterior al corte de "
         f"conocimiento del LLM); entradas hasta {pd.to_datetime(entry_end, unit='ms'):%Y-%m-%d}. Cartera 10,000 USD, "
         f"5 cupos, 1x (1,000 USD de nocional por cupo). Universo: candidatos del modelo (`p_prod` > 0) en "
         f"{len({s for v in arms.values() for s in v['trades'].sym})} tokens solo-Alpha con velas 1m. Motor "
         "`profit_study.lifecycle` con la política `variants()[0]` (espera de basis +0.25/−0.15, tramos cada 15 min, "
         "SL 80 %, salida B = 2 pagos ≤ 0, tope 30 d).", "",
         "**Criterio (fijado antes de correr):** el LLM mejora si su rentabilidad mensual media supera a P0 sin empeorar "
         "el p10 mensual ni el p5 por operación. Con ~4 meses y ~100 decisiones el resultado es **direccional**, no "
         "concluyente.", "",
         *([f"**Corte:** se detuvo por límites de uso de la suscripción tras {cut} días de decisiones; todos los brazos "
            f"se comparan con las entradas hasta {pd.to_datetime(cut_ms, unit='ms'):%Y-%m-%d} (lo abierto al corte sale "
            "por la regla B, igual que P0d). %/mes = PnL de esas entradas / días de la ventana de entradas; el p10 del "
            "criterio es el de la semana (en %/mes) porque no hay meses completos suficientes.", ""] if cut else []),
         "Brazos: **P0** = `profit_study.portfolio()` (decide en cada liquidación); **P0d** = mismo modelo y salida B "
         "pero decide una vez al día a las 00:04 UTC (mismo reloj que el LLM); **RND** = azar entre los candidatos del "
         "día, salida B (5 semillas); **LLM** = Claude decide entradas, tamaño (0.5–1.5 cupos) y salidas sin la "
         "predicción; **LLM+ML** = igual con `p_prod` visible. El SL, el tope de 30 d y la ejecución los manda el motor.",
         "", "## 1. Cartera (rentabilidad sobre capital total)", "",
         "| brazo | ops | %/mes (normalizado) | " + " | ".join(months) + f" | {p10l} | USD | DD % | utilización | tamaño medio |",
         "|---|---|---|" + "---|" * len(months) + "---|---|---|---|---|"]
    order = ["P0", "P0d_s0"] + sorted(k for k in st if k.startswith("RND")) + sorted(k for k in st if k.startswith("LLM"))
    order = [k for k in order if k in st]
    for k in order:
        s = st[k]
        L.append(f"| {k} | {s['n']} | {s['norm_m']:+.2f} % | " + " | ".join(f"{v:+.2f}" for v in s["monthly"])
                 + f" | {s['p10_m']:+.2f} | {s['usd']:+.0f} | {s['dd']:.1f} | {s['util']:.0f} % | {_fmt(s['size_mean'], 2, False)} |")
    rnd = [st[k] for k in st if k.startswith("RND")]
    if rnd:
        L.append(f"| RND (media de {len(rnd)}) | {np.mean([s['n'] for s in rnd]):.0f} | "
                 f"{np.mean([s['norm_m'] for s in rnd]):+.2f} % | "
                 + " | ".join(f"{np.mean([s['monthly'].iloc[i] for s in rnd]):+.2f}" for i in range(len(months)))
                 + f" | {np.mean([s['p10_m'] for s in rnd]):+.2f} | {np.mean([s['usd'] for s in rnd]):+.0f} | "
                 f"{np.mean([s['dd'] for s in rnd]):.1f} | {np.mean([s['util'] for s in rnd]):.0f} % | 1.00 |")
    L += ["", "## 2. Por operación (% del nocional de cada operación)", "",
          "| brazo | entradas pedidas | llenadas | media | mediana | p5 | eventos SL | SL % | días (mediana) |",
          "|---|---|---|---|---|---|---|---|---|"]
    for k in order:
        s = st[k]
        L.append(f"| {k} | {s['tried']} | {s['n']} | {_fmt(s['mean_t'])} | {_fmt(s['med_t'])} | {_fmt(s['p5_t'])} | "
                 f"{s['sl']} | {s['sl_pct']:.1f} | {_fmt(s['days_med'], 1, False)} |")
    L += ["", "## 3. Contra P0: bootstrap por semana (PnL semanal por cierre, 5,000 remuestreos)", "",
          f"| brazo | Δ %/mes | IC 95 % | P(Δ > 0) | Δ {p10l} | Δ p5 op | ¿mejora? (criterio) |", "|---|---|---|---|---|---|---|"]
    b = st["P0"]
    verdict = {}
    for k in order[1:]:
        s = st[k]
        d, lo, hi, pr = boot_weekly(s["weekly"], b["weekly"])
        better = s["mean_m"] > b["mean_m"] and s["p10_m"] >= b["p10_m"] and s["p5_t"] >= b["p5_t"]
        verdict[k] = better
        L.append(f"| {k} | {d:+.2f} | [{lo:+.2f}, {hi:+.2f}] | {pr:.0f} % | {s['p10_m'] - b['p10_m']:+.2f} | "
                 f"{s['p5_t'] - b['p5_t']:+.2f} | {'sí' if better else 'no'} |")
    # Uso del LLM
    L += ["", "## 4. Uso del LLM (suscripción vía `claude -p`; costo = equivalente API que reporta el CLI)", "",
          "| brazo | llamadas | s/llamada | tokens entrada (incl. caché) | tokens salida | USD equiv. API / llamada | USD equiv. total |",
          "|---|---|---|---|---|---|---|"]
    per_call = []
    for k in order:
        lg = arms[k].get("log") or []
        if not lg:
            continue
        cost = [x["cost"] or 0 for x in lg]
        tin = [sum((x["usage"] or {}).get(f, 0) or 0 for f in ("input_tokens", "cache_creation_input_tokens",
                                                                "cache_read_input_tokens")) for x in lg]
        tout = [(x["usage"] or {}).get("output_tokens", 0) or 0 for x in lg]
        per_call.append(np.mean(cost))
        L.append(f"| {k} | {len(lg)} | {np.mean([x['secs'] or 0 for x in lg]):.0f} | {np.mean(tin):,.0f} | "
                 f"{np.mean(tout):,.0f} | {np.mean(cost):.4f} | {sum(cost):.2f} |")
    with open(RUNS / "_report_state.json", "w", encoding="utf-8") as fh:
        json.dump({"verdict": verdict, "per_call": float(np.mean(per_call)) if per_call else None,
                   "norm": {k: st[k]["norm_m"] for k in order}}, fh)
    # Muestras de razonamiento
    for k in order:
        lg = arms[k].get("log") or []
        if lg:
            L += ["", f"### Ejemplos de decisiones — {k}", ""]
            for x in [lg[i] for i in np.linspace(0, len(lg) - 1, min(6, len(lg))).astype(int)]:
                L.append(f"- día {x['dia']}: entra {[e for e, _ in x['enter']]} sale {x['exit']} — {x['why'][:220]}")
    path = ROOT / "reports" / f"llm_policy_pilot_{today}.md"
    path.write_text("\n".join(L) + "\n", encoding="utf-8")
    print("\n".join(L))
    print("Reporte (tablas):", path)


if __name__ == "__main__":
    main()
