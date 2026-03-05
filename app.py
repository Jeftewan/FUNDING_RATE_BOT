#!/usr/bin/env python3
"""
Funding Rate Portfolio Manager v5.0 — Railway Edition
Changes:
  - Scoring: heavier weight on payment frequency + rate stability (low stddev)
  - Aggressive: RSI-14 daily <= 30 filter + auto SL = max 24h funding %
  - Manual close button on all active positions
"""

import requests as req
import time, json, os, threading, logging, math
from datetime import datetime
from flask import Flask, jsonify, request as flask_req, Response

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("bot")
app = Flask(__name__)

# ═══════════════════════════════════════════════════════════════
#  CONFIG
# ═══════════════════════════════════════════════════════════════
CAPITAL      = float(os.environ.get("CAPITAL", "1000"))
SCAN_MIN     = int(os.environ.get("SCAN_MINUTES", "5"))
MIN_VOL      = float(os.environ.get("MIN_VOLUME", "5000000"))
SAFE_PCT     = float(os.environ.get("SAFE_PCT", "80"))
AGGR_PCT     = float(os.environ.get("AGGR_PCT", "20"))
BOT_PASSWORD = os.environ.get("BOT_PASSWORD", "")

DATA_DIR = os.environ.get("DATA_DIR", "/app/data")
if not os.path.exists(DATA_DIR):
    try:
        os.makedirs(DATA_DIR, exist_ok=True)
    except:
        DATA_DIR = "."
STATE_FILE = os.path.join(DATA_DIR, "portfolio_state.json")
log.info(f"State: {STATE_FILE}")

LOCK = threading.Lock()
_scanner_started = False

STATE = {
    "total_capital": CAPITAL, "scan_interval": SCAN_MIN * 60,
    "min_volume": MIN_VOL, "safe_pct": SAFE_PCT, "aggr_pct": AGGR_PCT,
    "reserve_pct": 10, "max_pos_safe": 2, "max_pos_aggr": 1,
    "min_apr_safe": 5, "min_apr_aggr": 15, "min_score": 40,
    "positions": [], "history": [], "total_earned": 0,
    "last_scan": 0, "scan_count": 0,
    "safe_top": [], "aggr_top": [], "all_data": [],
    "actions": [], "last_scan_time": "—",
    "status": "Iniciando...", "last_error": "",
}
FEES = {"Binance": {"spot": 0.10, "fut": 0.05}, "Bybit": {"spot": 0.10, "fut": 0.06}}

def save_state():
    saveable = {k: v for k, v in STATE.items() if k not in ["all_data"]}
    try:
        tmp = STATE_FILE + ".tmp"
        with open(tmp, "w") as f: json.dump(saveable, f, indent=2, default=str)
        os.replace(tmp, STATE_FILE)
    except Exception as e: log.error(f"Save error: {e}")

def load_state():
    try:
        with open(STATE_FILE, "r") as f: saved = json.load(f)
        for k, v in saved.items():
            if k in STATE: STATE[k] = v
        log.info(f"State loaded: {len(STATE['positions'])} pos, ${STATE['total_earned']:.2f} earned")
    except FileNotFoundError: log.info("No prior state")
    except Exception as e: log.error(f"Load error: {e}")


# ═══════════════════════════════════════════════════════════════
#  HTTP
# ═══════════════════════════════════════════════════════════════
def _get(url, params=None, retries=2):
    for attempt in range(retries + 1):
        try:
            r = req.get(url, params=params, timeout=20)
            r.raise_for_status()
            return r.json()
        except req.exceptions.Timeout:
            if attempt < retries: time.sleep(2)
        except req.exceptions.ConnectionError:
            if attempt < retries: time.sleep(3)
        except Exception as e:
            log.warning(f"Error {url}: {e}")
            break
    return None


# ═══════════════════════════════════════════════════════════════
#  DATA FETCHING
# ═══════════════════════════════════════════════════════════════
_bn_iv = {}

def fetch_bn_intervals():
    global _bn_iv
    if _bn_iv: return _bn_iv
    d = _get("https://fapi.binance.com/fapi/v1/fundingInfo")
    if d:
        for x in d: _bn_iv[x.get("symbol", "")] = x.get("fundingIntervalHours", 8)
    return _bn_iv

def fetch_binance():
    fd = _get("https://fapi.binance.com/fapi/v1/premiumIndex")
    vd = _get("https://fapi.binance.com/fapi/v1/ticker/24hr")
    if not fd:
        log.error("Binance premiumIndex failed")
        return []
    ivs = fetch_bn_intervals()
    vm = {}
    if vd:
        for v in vd:
            s = v.get("symbol", "")
            if s.endswith("USDT"): vm[s] = float(v.get("quoteVolume", 0))
    out = []
    for x in fd:
        s = x.get("symbol", "")
        if not s.endswith("USDT"): continue
        ih = ivs.get(s, 8)
        nxt = int(x.get("nextFundingTime", 0))
        mn = max(0, (nxt / 1000 - time.time()) / 60) if nxt > 0 else -1
        out.append({"symbol": s.replace("USDT", ""), "pair": s, "exchange": "Binance",
            "fr": float(x.get("lastFundingRate", 0)), "price": float(x.get("markPrice", 0)),
            "vol24h": vm.get(s, 0), "ih": ih, "ipd": 24 / ih, "mins_next": mn})
    log.info(f"Binance: {len(out)} pairs")
    return out

def fetch_bybit():
    d = _get("https://api.bybit.com/v5/market/tickers", params={"category": "linear"})
    if not d:
        log.error("Bybit tickers failed")
        return []
    out = []
    for x in d.get("result", {}).get("list", []):
        s = x.get("symbol", "")
        if not s.endswith("USDT"): continue
        out.append({"symbol": s.replace("USDT", ""), "pair": s, "exchange": "Bybit",
            "fr": float(x.get("fundingRate", 0)), "price": float(x.get("markPrice", 0)),
            "vol24h": float(x.get("turnover24h", 0)), "ih": 8, "ipd": 3, "mins_next": -1})
    log.info(f"Bybit: {len(out)} pairs")
    return out

def fetch_hist(sym, exch, lim=15):
    if exch == "Binance":
        d = _get("https://fapi.binance.com/fapi/v1/fundingRate",
                 params={"symbol": f"{sym}USDT", "limit": lim}, retries=1)
        if not d: return [], []
        return [float(x["fundingRate"]) for x in d], [int(x.get("fundingTime", 0)) for x in d]
    else:
        d = _get("https://api.bybit.com/v5/market/funding/history",
                 params={"category": "linear", "symbol": f"{sym}USDT", "limit": lim}, retries=1)
        if not d: return [], []
        items = d.get("result", {}).get("list", [])
        return [float(x["fundingRate"]) for x in items], [int(x.get("fundingRateTimestamp", 0)) for x in items]

def detect_bb_iv(tss):
    if len(tss) < 2: return 8
    diffs = [abs(tss[i] - tss[i + 1]) / (1000 * 3600) for i in range(min(3, len(tss) - 1))]
    avg = sum(diffs) / len(diffs) if diffs else 8
    for iv in [1, 2, 4, 8]:
        if abs(avg - iv) < 1: return iv
    return 8


# ═══════════════════════════════════════════════════════════════
#  RSI CALCULATION (daily, 14 periods)
# ═══════════════════════════════════════════════════════════════
def fetch_rsi(sym, exch):
    """Calcula RSI-14 en velas diarias. Retorna RSI o -1 si no se puede."""
    try:
        if exch == "Binance":
            d = _get("https://fapi.binance.com/fapi/v1/klines",
                     params={"symbol": f"{sym}USDT", "interval": "1d", "limit": 16}, retries=1)
            if not d or len(d) < 15: return -1
            closes = [float(k[4]) for k in d]
        else:
            d = _get("https://api.bybit.com/v5/market/kline",
                     params={"category": "linear", "symbol": f"{sym}USDT", "interval": "D", "limit": 16}, retries=1)
            if not d: return -1
            klines = d.get("result", {}).get("list", [])
            if len(klines) < 15: return -1
            klines.reverse()  # Bybit returns newest first
            closes = [float(k[4]) for k in klines]

        # RSI-14 calculation
        changes = [closes[i + 1] - closes[i] for i in range(len(closes) - 1)]
        gains = [max(c, 0) for c in changes]
        losses = [abs(min(c, 0)) for c in changes]
        avg_gain = sum(gains[:14]) / 14
        avg_loss = sum(losses[:14]) / 14
        # Smooth with remaining data
        for i in range(14, len(gains)):
            avg_gain = (avg_gain * 13 + gains[i]) / 14
            avg_loss = (avg_loss * 13 + losses[i]) / 14
        if avg_loss == 0: return 100
        rs = avg_gain / avg_loss
        return 100 - (100 / (1 + rs))
    except Exception as e:
        log.warning(f"RSI error {sym}: {e}")
        return -1


# ═══════════════════════════════════════════════════════════════
#  ANALYSIS v5 — Frequency + Stability weighted
# ═══════════════════════════════════════════════════════════════
def analyze_consist(hist, fr_sign):
    if not hist:
        return {"avg": 0, "pct": 0, "streak": 0, "ok": False, "stddev": 999}
    fav = sum(1 for r in hist if (fr_sign > 0 and r > 0) or (fr_sign < 0 and r < 0))
    pct = fav / len(hist) * 100
    streak = 0
    for r in hist:
        if (fr_sign > 0 and r > 0) or (fr_sign < 0 and r < 0): streak += 1
        else: break
    avg = sum(hist) / len(hist)
    # Standard deviation — lower = more stable/uniform
    variance = sum((r - avg) ** 2 for r in hist) / len(hist)
    stddev = math.sqrt(variance)
    return {"avg": avg, "pct": pct, "streak": streak, "ok": pct >= 70 and streak >= 3, "stddev": stddev, "_rates": hist}

def est_slippage(vol, size):
    if vol <= 0: return 0.5
    r = size / vol
    if r < 0.00001: return 0.01
    if r < 0.0001:  return 0.03
    if r < 0.001:   return 0.05
    if r < 0.01:    return 0.10
    return 0.20

def calc_returns(token, capital):
    is_pos = token["fr"] > 0
    ih = token.get("ih", 8); ipd = token.get("ipd", 24 / ih)
    # Sin reserva: 50/50 para seguras, 100% futuros para agresivas
    spot = capital / 2 if is_pos else 0
    fut = capital / 2 if is_pos else capital
    fi = FEES.get(token["exchange"], FEES["Binance"])
    fee_in = spot * (fi["spot"] / 100) + fut * (fi["fut"] / 100)
    total_fees = fee_in * 2
    slip = est_slippage(token["vol24h"], spot + fut)
    slip_cost = (spot + fut) * (slip / 100) * 2
    total_cost = total_fees + slip_cost
    afr = abs(token["fr"]); fpi = fut * afr; fd = fpi * ipd; fa = fd * 365
    apr = (fa / capital) * 100 if capital > 0 else 0
    be = total_cost / fd if fd > 0 else 999
    carry = "Positive" if is_pos else "Reverse"
    mdp = (fd / fut * 100) if (not is_pos and fut > 0) else 0
    sl_pct = (fd / capital * 100) if capital > 0 else 0
    return {"spot": spot, "fut": fut,
        "total_fees": total_fees, "slip_cost": slip_cost, "total_cost": total_cost,
        "slip_pct": slip, "fpi": fpi, "fd": fd, "apr": apr, "be": be,
        "carry": carry, "ih": ih, "ipd": ipd, "mdp": mdp, "sl_pct": sl_pct,
        "worthwhile": be < 5 and apr > 5}

def risk_score(token, hist, is_aggressive=False):
    """
    v6.1 scoring — optimizado con datos de investigación:
    - Yield Diario:     30pts — freq × magnitud (ganancia REAL/día), penaliza extremos
    - Estabilidad:      25pts — CV + rate mínimo + uniformidad 
    - Consistencia:     15pts — streak actual + % favorable
    - Liquidez:         15pts — volumen 24h
    - Tendencia:        10pts — rate subiendo/estable/bajando
    - Breakeven speed:   5pts — días para recuperar costos
    """
    sc = 0
    afr = abs(token["fr"])
    ipd = token.get("ipd", 3)

    # 1. YIELD DIARIO EFECTIVO (30pts) — freq × magnitud
    # Penaliza rates extremos (>0.15% por intervalo) porque revierten rápido
    yield_day_pct = afr * ipd * 100  # % del futuro que ganas por día
    rate_per_iv = afr * 100  # % por intervalo individual

    if rate_per_iv > 0.15:
        # Rate extremo: probablemente temporal, penalizar
        if yield_day_pct >= 0.15:   sc += 22  # bueno pero temporal
        elif yield_day_pct >= 0.10: sc += 18
        else:                       sc += 12
    else:
        # Rate en rango sostenible
        if yield_day_pct >= 0.15:    sc += 30   # ~55% APR, sostenible
        elif yield_day_pct >= 0.10:  sc += 27   # ~36% APR
        elif yield_day_pct >= 0.06:  sc += 23   # ~22% APR
        elif yield_day_pct >= 0.03:  sc += 17   # ~11% APR
        elif yield_day_pct >= 0.01:  sc += 10   # ~3.6% APR
        else:                        sc += 3

    # 2. ESTABILIDAD (25pts) — CV + tasa mínima del historial
    stddev = hist.get("stddev", 999)
    avg = abs(hist.get("avg", 0))
    rates = hist.get("_rates", [])

    if avg > 0 and rates:
        cv = stddev / avg  # coeficiente de variación
        # Tasa mínima: ¿cuál fue el peor cobro?
        favorable = [abs(r) for r in rates if (token["fr"] > 0 and r > 0) or (token["fr"] < 0 and r < 0)]
        min_rate_ratio = min(favorable) / avg if favorable and avg > 0 else 0

        # CV bajo + min_rate alto = muy predecible
        if cv < 0.2 and min_rate_ratio > 0.5:   sc += 25  # excelente
        elif cv < 0.3 and min_rate_ratio > 0.3:  sc += 22
        elif cv < 0.3:                            sc += 19
        elif cv < 0.5:                            sc += 15
        elif cv < 0.8:                            sc += 10
        elif cv < 1.2:                            sc += 5
        else:                                     sc += 1
    else:
        sc += 1

    # 3. CONSISTENCIA (15pts) — streak > % total
    streak = hist.get("streak", 0)
    pct = hist.get("pct", 0)

    if streak >= 12:         sc += 15   # 12+ seguidos = perfecto
    elif streak >= 8:        sc += 13
    elif streak >= 5 and pct > 80: sc += 11
    elif streak >= 3 and pct > 70: sc += 9
    elif pct > 60:           sc += 6
    else:                    sc += 2

    # 4. LIQUIDEZ (15pts)
    vol = token["vol24h"]
    if vol >= 100e6:  sc += 15
    elif vol >= 50e6: sc += 12
    elif vol >= 20e6: sc += 10
    elif vol >= 10e6: sc += 7
    elif vol >= 5e6:  sc += 4
    else:             sc += 1

    # 5. TENDENCIA (10pts) — rate subiendo/estable vs bajando
    if len(rates) >= 8:
        recent = rates[:4]
        older = rates[4:8]
        avg_rec = sum(abs(r) for r in recent) / len(recent)
        avg_old = sum(abs(r) for r in older) / len(older)
        if avg_old > 0:
            trend = avg_rec / avg_old
            if trend >= 1.3:    sc += 10  # subiendo fuerte
            elif trend >= 1.0:  sc += 8   # estable o subiendo
            elif trend >= 0.7:  sc += 4   # bajando un poco
            else:               sc += 1   # bajando fuerte
        else:
            sc += 5
    else:
        sc += 5

    # 6. BREAKEVEN SPEED (5pts)
    capital_ref = 100
    c = calc_returns(token, capital_ref)
    be = c.get("be", 999)
    if be <= 1:    sc += 5
    elif be <= 2:  sc += 4
    elif be <= 3:  sc += 3
    elif be <= 5:  sc += 2
    else:          sc += 0

    return min(sc, 100)


# ═══════════════════════════════════════════════════════════════
#  PORTFOLIO
# ═══════════════════════════════════════════════════════════════
def get_bd():
    t = STATE["total_capital"]; sb = t * (STATE["safe_pct"] / 100); ab = t * (STATE["aggr_pct"] / 100)
    su = sum(p["capital_used"] for p in STATE["positions"] if p["carry"] == "Positive")
    au = sum(p["capital_used"] for p in STATE["positions"] if p["carry"] == "Reverse")
    sc = sum(1 for p in STATE["positions"] if p["carry"] == "Positive")
    ac = sum(1 for p in STATE["positions"] if p["carry"] == "Reverse")
    return {"total": t, "sb": sb, "ab": ab, "su": su, "au": au,
            "sa": max(0, sb - su), "aa": max(0, ab - au), "sc": sc, "ac": ac}

def gen_actions():
    actions = []; bd = get_bd()
    positions = STATE["positions"]; all_data = STATE["all_data"]
    safe_top = STATE["safe_top"]; aggr_top = STATE["aggr_top"]

    for i, pos in enumerate(positions):
        cur = next((d for d in all_data if d["symbol"] == pos["symbol"] and d["exchange"] == pos["exchange"]), None)
        if not cur: continue
        cfr = cur["fr"]
        fr_rev = (pos["entry_fr"] > 0 and cfr < 0) or (pos["entry_fr"] < 0 and cfr > 0)
        fr_drop = abs(cfr) < abs(pos["entry_fr"]) * 0.25 and not fr_rev
        cc = calc_returns(cur, pos["capital_used"])

        # Check SL for aggressive positions
        if pos["carry"] == "Reverse" and pos.get("sl_pct", 0) > 0:
            cp = cur["price"]; ep = pos["entry_price"]
            if ep > 0:
                price_drop = ((ep - cp) / ep) * 100
                if price_drop >= pos["sl_pct"]:
                    actions.append({"pri": 0, "type": "EXIT", "idx": i, "critical": True,
                        "title": f"⛔ SL AGRESIVA: {pos['symbol']} — Precio cayó {price_drop:.2f}% (SL: {pos['sl_pct']:.2f}%)",
                        "detail": f"Entrada: ${ep:.4f} → Ahora: ${cp:.4f} │ Pérdida supera ganancia máxima de 24h en funding",
                        "steps": [], "costs": "", "warning": "", "countdown": ""})
                    continue

        if fr_rev:
            actions.append({"pri": 0, "type": "EXIT", "idx": i, "critical": True,
                "title": f"⛔ CERRAR {pos['symbol']} ({pos['exchange']}) — Funding cambió de signo",
                "detail": f"Entrada: {pos['entry_fr']*100:.4f}% → Ahora: {cfr*100:.4f}%",
                "steps": [], "costs": "", "warning": "", "countdown": ""})
        elif fr_drop:
            better = None; pool = safe_top if pos["carry"] == "Positive" else aggr_top
            for opp in pool:
                if opp["token"]["symbol"] != pos["symbol"]:
                    oc = calc_returns(opp["token"], pos["capital_used"])
                    if oc["apr"] > cc["apr"] * 2: better = opp; break
            if better:
                bc = calc_returns(better["token"], pos["capital_used"])
                actions.append({"pri": 1, "type": "ROTATE", "idx": i, "critical": False,
                    "title": f"🔄 ROTAR: {pos['symbol']} → {better['token']['symbol']} ({better['token']['exchange']})",
                    "detail": f"APR: {cc['apr']:.1f}% → {bc['apr']:.1f}%",
                    "new_sym": better["token"]["symbol"], "new_exch": better["token"]["exchange"],
                    "steps": [], "costs": "", "warning": "", "countdown": ""})

    def add_open(pool, slots, cap_avail, carry_label, min_apr, pri):
        if slots <= 0 or cap_avail <= 20: return
        cpp = cap_avail / slots
        for opp in pool[:slots]:
            c = calc_returns(opp["token"], cpp)
            if not c["worthwhile"] or c["apr"] < min_apr or opp["score"] < STATE["min_score"]: continue
            if any(p["symbol"] == opp["token"]["symbol"] and p["exchange"] == opp["token"]["exchange"] for p in positions): continue
            t = opp["token"]; emoji = "🛡️" if carry_label == "safe" else "⚡"
            if c["carry"] == "Positive":
                steps = [f"1. COMPRA {t['symbol']} en SPOT por ${c['spot']:.2f}",
                    f"2. Abre SHORT {t['symbol']}USDT PERPETUO por ${c['fut']:.2f}",
                    f"   → Leverage: 1x │ Cross Margin"]
            else:
                steps = [f"1. Abre LONG {t['symbol']}USDT PERPETUO por ${c['fut']:.2f}",
                    f"   → Leverage: 1x │ Cross Margin",
                    f"2. STOP LOSS: -{c['sl_pct']:.2f}% (ganancia máx 24h en funding)"]
            rsi_info = ""
            if carry_label == "aggr":
                rsi_val = opp.get("rsi", -1)
                rsi_info = f" │ RSI: {rsi_val:.0f}" if rsi_val >= 0 else ""
            actions.append({"pri": pri, "type": "OPEN", "carry": carry_label, "critical": False,
                "title": f"{emoji} ABRIR: {t['symbol']}/USDT en {t['exchange']}",
                "detail": f"APR: {c['apr']:.1f}% │ ${c['fd']:.2f}/día │ BE: {c['be']:.1f}d │ Score: {opp['score']}/100{rsi_info}",
                "steps": steps,
                "costs": f"Fees: ${c['total_fees']:.2f} │ Slip: ~${c['slip_cost']:.2f} ({c['slip_pct']:.2f}%) │ Total: ${c['total_cost']:.2f}",
                "countdown": f"⏱ Próximo cobro en {int(t['mins_next'])}min" if t.get("mins_next", 0) > 0 else "",
                "warning": f"⚠ SL: -{c['sl_pct']:.2f}% │ Compensa caídas ~{c['mdp']:.2f}%/día" if c["carry"] == "Reverse" else "",
                "symbol": t["symbol"], "exchange": t["exchange"],
                "capital": cpp, "fr": t["fr"], "price": t["price"],
                "ih": c["ih"], "carry_type": c["carry"], "sl_pct": c["sl_pct"]})

    add_open(safe_top, STATE["max_pos_safe"] - bd["sc"], bd["sa"], "safe", STATE["min_apr_safe"], 3)
    add_open(aggr_top, STATE["max_pos_aggr"] - bd["ac"], bd["aa"], "aggr", STATE["min_apr_aggr"], 4)

    if not actions and not positions:
        actions.append({"pri": 9, "type": "WAIT", "critical": False,
            "title": "⏳ Sin oportunidades — Esperando mejor mercado",
            "detail": f"Mín: Safe APR>{STATE['min_apr_safe']}% │ Aggr APR>{STATE['min_apr_aggr']}%",
            "steps": [], "costs": "", "warning": "", "countdown": ""})
    actions.sort(key=lambda x: x["pri"])
    return actions


# ═══════════════════════════════════════════════════════════════
#  SCANNER v5.1 — real earnings + RSI
# ═══════════════════════════════════════════════════════════════
def update_position_earnings(all_data):
    """Acumula ganancias REALES usando la tasa actual en cada scan."""
    now = time.time()
    for pos in STATE["positions"]:
        cur = next((d for d in all_data if d["symbol"] == pos["symbol"] and d["exchange"] == pos["exchange"]), None)
        if not cur: continue
        ih = pos.get("ih", 8)
        last_up = pos.get("last_earn_update", pos["entry_time"] / 1000)
        elapsed_h = (now - last_up) / 3600
        full_ivs = int(elapsed_h / ih)
        if full_ivs < 1: continue
        cfr = cur["fr"]
        is_pos_carry = pos["carry"] == "Positive"
        if is_pos_carry and cfr > 0:
            fut_size = pos["capital_used"] / 2
            earn_per_iv = fut_size * cfr
        elif not is_pos_carry and cfr < 0:
            fut_size = pos["capital_used"]
            earn_per_iv = fut_size * abs(cfr)
        else:
            earn_per_iv = 0
        earned_now = earn_per_iv * full_ivs
        pos["earned_real"] = pos.get("earned_real", 0) + earned_now
        pos["last_earn_update"] = now
        pos["last_fr_used"] = cfr
        if earned_now > 0:
            log.info(f"  +${earned_now:.4f} {pos['symbol']} ({full_ivs}ivs @ {cfr*100:.4f}%)")

def run_scan():
    log.info("Scan starting...")
    with LOCK: STATE["status"] = "Escaneando..."
    bn = fetch_binance(); bb = fetch_bybit(); all_data = bn + bb
    if not all_data:
        with LOCK: STATE["status"] = "Error: sin conexión"; STATE["last_error"] = "Sin conexión"
        return
    mv = STATE["min_volume"]
    pos_l = sorted([t for t in all_data if t["fr"] > 0.0001 and t["vol24h"] >= mv], key=lambda x: x["fr"], reverse=True)
    neg_l = sorted([t for t in all_data if t["fr"] < -0.0001 and t["vol24h"] >= mv], key=lambda x: x["fr"])

    def analyze_safe(tokens, lim=8):
        scored = []
        for t in tokens[:lim]:
            rates, tss = fetch_hist(t["symbol"], t["exchange"])
            time.sleep(0.08)
            if t["exchange"] == "Bybit" and tss:
                t["ih"] = detect_bb_iv(tss); t["ipd"] = 24 / t["ih"]
            h = analyze_consist(rates, t["fr"])
            sc = risk_score(t, h, is_aggressive=False)
            scored.append({"token": t, "hist": h, "score": sc})
        scored.sort(key=lambda x: x["score"], reverse=True)
        return scored[:3]

    def analyze_aggr(tokens, lim=10):
        scored = []
        for t in tokens[:lim]:
            rates, tss = fetch_hist(t["symbol"], t["exchange"])
            time.sleep(0.08)
            if t["exchange"] == "Bybit" and tss:
                t["ih"] = detect_bb_iv(tss); t["ipd"] = 24 / t["ih"]
            h = analyze_consist(rates, t["fr"])
            rsi = fetch_rsi(t["symbol"], t["exchange"])
            time.sleep(0.08)
            if rsi < 0: continue
            if rsi > 30: continue
            sc = risk_score(t, h, is_aggressive=True)
            scored.append({"token": t, "hist": h, "score": sc, "rsi": rsi})
        scored.sort(key=lambda x: x["score"], reverse=True)
        return scored[:3]

    safe = analyze_safe(pos_l)
    aggr = analyze_aggr(neg_l)

    with LOCK:
        STATE["all_data"] = all_data
        # Actualizar ganancias reales ANTES de generar acciones
        update_position_earnings(all_data)
        STATE["safe_top"] = safe; STATE["aggr_top"] = aggr
        STATE["last_scan"] = time.time(); STATE["scan_count"] += 1
        STATE["last_scan_time"] = datetime.now().strftime("%H:%M:%S")
        STATE["actions"] = gen_actions()
        n_rsi = len(aggr)
        STATE["status"] = f"OK — {len(bn)}BN+{len(bb)}BB │ {len(pos_l)}pos {len(neg_l)}neg │ {n_rsi} aggr RSI≤30"
        STATE["last_error"] = ""; save_state()
    log.info(f"Scan #{STATE['scan_count']}: {len(pos_l)}pos {len(neg_l)}neg {n_rsi}aggr_rsi")

def scanner_loop():
    log.info("Scanner thread started")
    time.sleep(5)
    while True:
        try: run_scan()
        except Exception as e:
            log.exception(f"Scan error: {e}")
            with LOCK: STATE["status"] = f"Error: {str(e)[:80]}"; STATE["last_error"] = str(e)
        time.sleep(STATE["scan_interval"])

def ensure_scanner():
    global _scanner_started
    if not _scanner_started:
        _scanner_started = True
        threading.Thread(target=scanner_loop, daemon=True).start()
        log.info("Scanner launched")


# ═══════════════════════════════════════════════════════════════
#  ROUTES
# ═══════════════════════════════════════════════════════════════
@app.before_request
def _before(): ensure_scanner()

@app.route("/health")
def health():
    return jsonify({"ok": True, "scans": STATE["scan_count"], "status": STATE["status"]})

@app.route("/api/state")
def api_state():
    with LOCK:
        bd = get_bd(); pdata = []
        for pos in STATE["positions"]:
            cur = next((d for d in STATE["all_data"] if d["symbol"] == pos["symbol"] and d["exchange"] == pos["exchange"]), None)
            cfr = cur["fr"] if cur else pos["entry_fr"]; cp = cur["price"] if cur else pos["entry_price"]
            pc = ((cp - pos["entry_price"]) / pos["entry_price"] * 100) if pos["entry_price"] > 0 else 0
            ih = pos.get("ih", cur.get("ih", 8) if cur else 8)
            el_h = (time.time() - pos["entry_time"] / 1000) / 3600; ivs = int(el_h / ih)
            c = calc_returns({"fr": cfr, "price": cp, "symbol": pos["symbol"],
                "exchange": pos["exchange"], "vol24h": 0, "ih": ih, "ipd": 24 / ih}, pos["capital_used"])
            est = pos.get("earned_real", 0)  # Ganancias REALES acumuladas
            pp = c["fut"] * (pc / 100) if pos["carry"] == "Reverse" else 0
            fr_rev = (pos["entry_fr"] > 0 and cfr < 0) or (pos["entry_fr"] < 0 and cfr > 0)
            # SL check for display
            sl_hit = False
            if pos["carry"] == "Reverse" and pos.get("sl_pct", 0) > 0 and pos["entry_price"] > 0:
                price_drop = ((pos["entry_price"] - cp) / pos["entry_price"]) * 100
                sl_hit = price_drop >= pos["sl_pct"]
            pdata.append({**pos, "current_fr": cfr, "current_price": cp, "price_change": pc,
                "elapsed_h": el_h, "intervals": ivs, "est_earned": est, "price_pnl": pp,
                "total_pnl": est + pp, "current_apr": c["apr"], "fr_reversed": fr_rev,
                "mins_next": cur.get("mins_next", -1) if cur else -1, "sl_hit": sl_hit})
        return jsonify({"capital": STATE["total_capital"], "earned": STATE.get("total_earned", 0),
            "breakdown": bd, "positions": pdata, "actions": STATE["actions"],
            "safe_top": [{"token": o["token"], "hist": o["hist"], "score": o["score"],
                "calc": calc_returns(o["token"], max(bd["sa"] / max(1, STATE["max_pos_safe"] - bd["sc"]), 50))}
                for o in STATE["safe_top"]],
            "aggr_top": [{"token": o["token"], "hist": o["hist"], "score": o["score"],
                "rsi": o.get("rsi", -1),
                "calc": calc_returns(o["token"], max(bd["aa"] / max(1, STATE["max_pos_aggr"] - bd["ac"]), 50))}
                for o in STATE["aggr_top"]],
            "status": STATE["status"], "scan_count": STATE["scan_count"],
            "last_scan": STATE["last_scan_time"], "scan_interval": STATE["scan_interval"],
            "last_error": STATE.get("last_error", "")})


def _close_position(i):
    """Helper to close position at index i, returns (ok, msg)."""
    if i < 0 or i >= len(STATE["positions"]):
        return False, "Posición inválida"
    pos = STATE["positions"][i]; ih = pos.get("ih", 8)
    el_h = (time.time() - pos["entry_time"] / 1000) / 3600; ivs = int(el_h / ih)
    est = pos.get("earned_real", 0)  # Ganancias reales acumuladas
    STATE["history"].append({"symbol": pos["symbol"], "exchange": pos["exchange"],
        "carry": pos["carry"], "hours": el_h, "intervals": ivs, "earned": est,
        "time": datetime.now().isoformat()})
    STATE["total_earned"] = STATE.get("total_earned", 0) + est
    sym = pos["symbol"]
    STATE["positions"].pop(i); save_state(); STATE["actions"] = gen_actions()
    log.info(f"Closed: {sym} earned ${est:.4f}")
    return True, f"✅ {sym} cerrada. Ganado: ${est:.2f}"


@app.route("/api/confirm", methods=["POST"])
def api_confirm():
    data = flask_req.json or {}
    if BOT_PASSWORD and data.get("password") != BOT_PASSWORD:
        return jsonify({"ok": False, "msg": "Contraseña incorrecta"})
    idx = data.get("action_idx", -1)
    with LOCK:
        if idx < 0 or idx >= len(STATE["actions"]):
            return jsonify({"ok": False, "msg": "Acción inválida"})
        act = STATE["actions"][idx]
        if act["type"] == "OPEN":
            STATE["positions"].append({"symbol": act["symbol"], "exchange": act["exchange"],
                "entry_fr": act["fr"], "entry_price": act["price"],
                "entry_time": int(time.time() * 1000), "carry": act["carry_type"],
                "capital_used": act["capital"], "ih": act.get("ih", 8),
                "sl_pct": act.get("sl_pct", 0)})
            save_state(); STATE["actions"] = gen_actions()
            return jsonify({"ok": True, "msg": f"✅ {act['symbol']} registrada"})
        elif act["type"] == "EXIT":
            ok, msg = _close_position(act["idx"])
            return jsonify({"ok": ok, "msg": msg})
        elif act["type"] == "ROTATE":
            i = act["idx"]
            if i < len(STATE["positions"]):
                ok, msg = _close_position(i)
                ns = act.get("new_sym"); ne = act.get("new_exch")
                cur = next((d for d in STATE["all_data"] if d["symbol"] == ns and d["exchange"] == ne), None)
                if cur:
                    nc = calc_returns(cur, act.get("capital", STATE["positions"][i]["capital_used"] if i < len(STATE["positions"]) else 100))
                    STATE["positions"].append({"symbol": ns, "exchange": ne,
                        "entry_fr": cur["fr"], "entry_price": cur["price"],
                        "entry_time": int(time.time() * 1000), "carry": nc["carry"],
                        "capital_used": nc.get("fut", 50) * 2 + nc.get("reserve", 10),
                        "ih": nc["ih"], "sl_pct": nc.get("sl_pct", 0)})
                    save_state(); STATE["actions"] = gen_actions()
                return jsonify({"ok": True, "msg": f"✅ Rotación → {ns}"})
    return jsonify({"ok": False, "msg": "Error"})


@app.route("/api/manual_close", methods=["POST"])
def api_manual_close():
    """Cierre manual de cualquier posición."""
    data = flask_req.json or {}
    if BOT_PASSWORD and data.get("password") != BOT_PASSWORD:
        return jsonify({"ok": False, "msg": "Contraseña incorrecta"})
    idx = data.get("position_idx", -1)
    with LOCK:
        ok, msg = _close_position(idx)
        return jsonify({"ok": ok, "msg": msg})


@app.route("/api/config", methods=["GET", "POST"])
def api_config():
    if flask_req.method == "GET":
        with LOCK:
            return jsonify({"capital": STATE["total_capital"], "scan_minutes": STATE["scan_interval"] // 60,
                "safe_pct": STATE["safe_pct"], "aggr_pct": STATE["aggr_pct"],
                "min_volume": STATE["min_volume"], "min_apr_safe": STATE["min_apr_safe"],
                "min_apr_aggr": STATE["min_apr_aggr"], "min_score": STATE["min_score"],
                "max_pos_safe": STATE["max_pos_safe"], "max_pos_aggr": STATE["max_pos_aggr"]})
    data = flask_req.json or {}
    if BOT_PASSWORD and data.get("password") != BOT_PASSWORD:
        return jsonify({"ok": False, "msg": "Contraseña incorrecta"})
    with LOCK:
        for k in ["capital", "min_volume", "min_apr_safe", "min_apr_aggr"]:
            if k in data: STATE[{"capital": "total_capital", "min_volume": "min_volume",
                "min_apr_safe": "min_apr_safe", "min_apr_aggr": "min_apr_aggr"}[k]] = float(data[k])
        if "scan_minutes" in data: STATE["scan_interval"] = int(data["scan_minutes"]) * 60
        if "safe_pct" in data: STATE["safe_pct"] = float(data["safe_pct"]); STATE["aggr_pct"] = 100 - STATE["safe_pct"]
        if "min_score" in data: STATE["min_score"] = int(data["min_score"])
        if "max_pos_safe" in data: STATE["max_pos_safe"] = int(data["max_pos_safe"])
        if "max_pos_aggr" in data: STATE["max_pos_aggr"] = int(data["max_pos_aggr"])
        STATE["actions"] = gen_actions(); save_state()
        return jsonify({"ok": True, "msg": f"✅ Config guardada"})


@app.route("/api/force_scan", methods=["POST"])
def api_force():
    threading.Thread(target=run_scan, daemon=True).start()
    return jsonify({"ok": True})


# ═══════════════════════════════════════════════════════════════
#  HTML
# ═══════════════════════════════════════════════════════════════
@app.route("/")
def index():
    return Response(HTML_PAGE, content_type="text/html; charset=utf-8")

HTML_PAGE = r"""<!DOCTYPE html>
<html lang="es"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Funding Bot v5</title>
<style>
@import url('https://fonts.googleapis.com/css2?family=JetBrains+Mono:wght@300;400;500;700&display=swap');
*{margin:0;padding:0;box-sizing:border-box}
body{font-family:'JetBrains Mono',monospace;background:#0a0b0d;color:#c8ccd0;min-height:100vh}
.c{max-width:900px;margin:0 auto;padding:12px}
.hdr{text-align:center;padding:14px 0 6px;border-bottom:1px solid #1a1d23}
.hdr h1{font-size:1.2em;color:#fff}.hdr .s{font-size:.6em;color:#555;margin-top:2px}
.cap{display:grid;grid-template-columns:1fr 1fr 1fr;gap:10px;padding:10px 0;border-bottom:1px solid #1a1d23}
.cap .i{text-align:center}.cap .l{font-size:.58em;color:#666;text-transform:uppercase;letter-spacing:1px}
.cap .v{font-size:1.05em;font-weight:700;color:#fff}.cap .v.g{color:#34d399}
.bd{display:grid;grid-template-columns:1fr 1fr;gap:8px;padding:8px 0;border-bottom:1px solid #1a1d23}
.bdi{background:#111318;border-radius:8px;padding:7px 10px;border-left:3px solid}
.bdi.sf{border-color:#34d399}.bdi.ag{border-color:#fbbf24}
.bdi .t{font-size:.62em;color:#888}.bdi .vl{font-size:.8em;color:#fff;font-weight:600}.bdi .sb{font-size:.58em;color:#555}
.st{font-size:.72em;font-weight:700;color:#fff;padding:12px 0 5px;text-transform:uppercase;letter-spacing:1px}
.ac{border-radius:10px;padding:11px;margin-bottom:7px;border:1px solid #1a1d23;background:#111318}
.ac.cr{border-color:#ef4444;background:#1a0a0a;animation:p 1.5s infinite}
.ac.so{border-color:#34d39944}.ac.ao{border-color:#fbbf2444}
.ac .at{font-size:.8em;font-weight:700;color:#fff}.ac .ad{font-size:.66em;color:#888;margin-top:2px}
.ac .as{margin:6px 0;padding:7px;background:#0a0b0d;border-radius:6px;font-size:.68em;white-space:pre-line}
.ac .acs{font-size:.6em;color:#666}.ac .aw{font-size:.66em;color:#fbbf24;margin-top:2px}
.ac .acd{font-size:.66em;color:#22d3ee;margin-top:2px}
.btn{display:inline-block;padding:7px 16px;border-radius:6px;font-family:inherit;font-size:.7em;font-weight:600;cursor:pointer;border:none;margin-top:5px;transition:.2s}
.bg{background:#059669;color:#fff}.bg:hover{background:#34d399}
.br{background:#dc2626;color:#fff}.br:hover{background:#ef4444}
.by{background:#d97706;color:#fff}.by:hover{background:#fbbf24;color:#000}
.bgy{background:#374151;color:#ccc}.bgy:hover{background:#4b5563}
.btn:disabled{opacity:.5;cursor:not-allowed}
.pc{border-radius:10px;padding:9px;margin-bottom:5px;border:1px solid #1a1d23;background:#111318}
.pc.al{border-color:#ef4444;background:#1a0a0a}
.ph{display:flex;align-items:center;gap:5px;margin-bottom:3px;flex-wrap:wrap}
.ps{font-weight:700;color:#fff}
.badge{font-size:.56em;padding:2px 6px;border-radius:99px;border:1px solid}
.bsf{color:#34d399;border-color:#34d39944;background:#34d39911}
.bag{color:#fbbf24;border-color:#fbbf2444;background:#fbbf2411}
.bbn{color:#f0b90b;border-color:#f0b90b44;background:#f0b90b11}
.bbb{color:#a78bfa;border-color:#a78bfa44;background:#a78bfa11}
.pg{display:grid;grid-template-columns:repeat(auto-fit,minmax(110px,1fr));gap:3px}
.pm .pl{font-size:.55em;color:#555;text-transform:uppercase}.pm .pv{font-size:.75em;font-weight:600;color:#fff}
.pv.g{color:#34d399}.pv.r{color:#ef4444}.pv.y{color:#fbbf24}
.sbar{text-align:center;padding:6px 0;font-size:.58em;color:#444;border-top:1px solid #1a1d23;margin-top:10px}
.err{text-align:center;padding:5px;font-size:.62em;color:#ef4444;background:#1a0a0a;border-radius:6px;margin:6px 0}
.tr{display:grid;grid-template-columns:1fr 1fr;gap:5px}
.om{background:#111318;border-radius:6px;padding:7px;font-size:.66em;border:1px solid #1a1d23;margin-bottom:3px}
.om .os{font-weight:700;color:#fff}.om .of{color:#34d399}.om .of.n{color:#ef4444}
.scr{text-align:center;padding:7px 0}
.alrt{background:#dc2626;color:#fff;padding:5px 8px;border-radius:6px;font-size:.7em;font-weight:700;margin-bottom:5px}
.ld{text-align:center;padding:50px 20px;color:#555}
.ld .sp{display:inline-block;width:22px;height:22px;border:2px solid #333;border-top-color:#34d399;border-radius:50%;animation:spin 1s linear infinite;margin-bottom:10px}
@keyframes p{0%,100%{opacity:1}50%{opacity:.85}}
@keyframes spin{to{transform:rotate(360deg)}}
@media(max-width:600px){.cap{grid-template-columns:1fr}.tr{grid-template-columns:1fr}.pg{grid-template-columns:1fr 1fr}}
</style></head><body>
<div class="c">
<div class="hdr"><h1>💰 Funding Bot v6.0</h1>
<div class="s">Yield Diario + Consistencia + Tendencia │ RSI │ Auto SL</div></div>
<div id="ct"><div class="ld"><div class="sp"></div><br>Esperando scan...<br><span style="font-size:.8em">~15 segundos</span></div></div>
</div>
<script>
let lastErr='';
async function ld(){
try{const r=await fetch('/api/state');if(!r.ok)throw new Error('HTTP '+r.status);
const s=await r.json();rn(s);lastErr=''}
catch(e){if(lastErr!==e.message){lastErr=e.message;
document.getElementById('ct').innerHTML=`<div class="err">Error: ${e.message}</div>`}}}

function rn(s){
const bd=s.breakdown;let h='';
h+=`<div class="cap"><div class="i"><div class="l">Capital</div><div class="v">$${s.capital.toLocaleString()}</div></div>
<div class="i"><div class="l">Ganado</div><div class="v g">$${s.earned.toFixed(2)}</div></div>
<div class="i"><div class="l">En Uso</div><div class="v">$${(bd.su+bd.au).toFixed(0)}</div></div></div>`;
h+=`<div class="bd"><div class="bdi sf"><div class="t">🛡️ Seguro (${bd.sc} pos)</div><div class="vl">$${bd.sb.toFixed(0)}</div><div class="sb">Libre: $${bd.sa.toFixed(0)}</div></div>
<div class="bdi ag"><div class="t">⚡ Agresivo (${bd.ac} pos)</div><div class="vl">$${bd.ab.toFixed(0)}</div><div class="sb">Libre: $${bd.aa.toFixed(0)}</div></div></div>`;
if(s.last_error)h+=`<div class="err">${s.last_error}</div>`;

// POSITIONS with manual close
if(s.positions.length>0){
h+='<div class="st">📊 Posiciones Activas</div>';
s.positions.forEach((p,pi)=>{
const e=p.carry==='Positive'?'🛡️':'⚡';const bc=p.carry==='Positive'?'bsf':'bag';
const ec=p.exchange==='Binance'?'bbn':'bbb';const cd=p.mins_next>0?`⏱${Math.floor(p.mins_next)}m`:'';
const slInfo=p.sl_pct>0?` │ SL:-${p.sl_pct.toFixed(2)}%`:'';
h+=`<div class="pc ${p.fr_reversed||p.sl_hit?'al':''}">
<div class="ph"><span>${e}</span><span class="ps">${p.symbol}</span>
<span class="badge ${bc}">${p.carry}</span><span class="badge ${ec}">${p.exchange}</span>
<span style="font-size:.57em;color:#555;margin-left:auto">$${p.capital_used.toFixed(0)} │ ${p.elapsed_h.toFixed(1)}h │ ${p.intervals}cobros ${cd}${slInfo}</span></div>
${p.fr_reversed?'<div class="alrt">⛔ FUNDING CAMBIÓ — CERRAR</div>':''}
${p.sl_hit?'<div class="alrt">⛔ STOP LOSS ALCANZADO — CERRAR</div>':''}
<div class="pg">
<div class="pm"><div class="pl">FR</div><div class="pv ${p.current_fr>0?'g':'r'}">${(p.current_fr*100).toFixed(4)}%</div></div>
<div class="pm"><div class="pl">APR</div><div class="pv ${p.current_apr>10?'g':'y'}">${p.current_apr.toFixed(1)}%</div></div>
<div class="pm"><div class="pl">Ganado</div><div class="pv g">$${p.est_earned.toFixed(2)}</div></div>
${p.carry==='Reverse'?`<div class="pm"><div class="pl">P&L Precio</div><div class="pv ${p.price_pnl>=0?'g':'r'}">$${p.price_pnl.toFixed(2)}</div></div>`:''}
<div class="pm"><div class="pl">Total</div><div class="pv ${p.total_pnl>=0?'g':'r'}">$${p.total_pnl.toFixed(2)}</div></div>
</div>
<button class="btn br" style="margin-top:6px;font-size:.62em" onclick="mc(${pi})">✖ Cerrar manual</button>
</div>`})}

// ACTIONS
if(s.actions.length>0){
h+='<div class="st">🎯 Acciones</div>';
s.actions.forEach((a,i)=>{
let cc=a.critical?'cr':(a.carry==='safe'?'so':(a.carry==='aggr'?'ao':''));
h+=`<div class="ac ${cc}"><div class="at">${a.title}</div><div class="ad">${a.detail}</div>`;
if(a.steps&&a.steps.length)h+=`<div class="as">${a.steps.join('\n')}</div>`;
if(a.costs)h+=`<div class="acs">${a.costs}</div>`;
if(a.warning)h+=`<div class="aw">${a.warning}</div>`;
if(a.countdown)h+=`<div class="acd">${a.countdown}</div>`;
if(a.type==='OPEN')h+=`<button class="btn bg" onclick="cf(${i})">✅ Ya ejecuté</button>`;
else if(a.type==='EXIT')h+=`<button class="btn br" onclick="cf(${i})">⛔ Ya cerré</button>`;
else if(a.type==='ROTATE')h+=`<button class="btn by" onclick="cf(${i})">🔄 Ya roté</button>`;
h+='</div>'})}

// TOP
if(s.safe_top.length||s.aggr_top.length){
h+='<div class="st">📈 Top Mercado</div><div class="tr">';
if(s.safe_top.length){h+='<div><div style="font-size:.62em;color:#34d399;margin-bottom:3px">🛡️ Seguras (frec+estab)</div>';
s.safe_top.forEach(o=>{const t=o.token;
h+=`<div class="om"><span class="os">${t.symbol}</span> <span class="badge ${t.exchange==='Binance'?'bbn':'bbb'}">${t.exchange}</span>
<span class="of">+${(t.fr*100).toFixed(4)}%/${t.ih}h</span> <span style="color:#666">S:${o.score} APR:${o.calc.apr.toFixed(1)}%</span></div>`});h+='</div>'}
if(s.aggr_top.length){h+='<div><div style="font-size:.62em;color:#fbbf24;margin-bottom:3px">⚡ Agresivas (RSI≤30)</div>';
s.aggr_top.forEach(o=>{const t=o.token;const rsi=o.rsi>=0?` RSI:${o.rsi.toFixed(0)}`:'';
h+=`<div class="om"><span class="os">${t.symbol}</span> <span class="badge ${t.exchange==='Binance'?'bbn':'bbb'}">${t.exchange}</span>
<span class="of n">${(t.fr*100).toFixed(4)}%/${t.ih}h</span> <span style="color:#666">S:${o.score} APR:${o.calc.apr.toFixed(1)}%${rsi}</span></div>`});h+='</div>'}
h+='</div>'}

h+=`<div class="scr"><button class="btn bgy" onclick="fs()">🔍 Escanear</button> <button class="btn bgy" onclick="showCfg()">⚙️ Config</button></div>`;
h+=`<div id="cfgPanel" style="display:none"></div>`;
h+=`<div class="sbar">${s.status} │ #${s.scan_count} │ ${s.last_scan} │ Cada ${Math.floor(s.scan_interval/60)}min │ Auto 30s</div>`;
document.getElementById('ct').innerHTML=h;
if(s.actions.some(a=>a.critical)){try{new Audio('data:audio/wav;base64,UklGRl9vAABXQVZFZm10IBAAAAABAAEAQB8AAEAfAAABAAgAZGF0YQ==').play()}catch(e){}}}

async function cf(i){const b=event.target;b.disabled=true;b.textContent='⏳...';
try{const r=await fetch('/api/confirm',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({action_idx:i})});
const d=await r.json();alert(d.msg);ld()}catch(e){alert('Error: '+e.message)}b.disabled=false}

async function mc(i){
if(!confirm('¿Cerrar esta posición manualmente?'))return;
try{const r=await fetch('/api/manual_close',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({position_idx:i})});
const d=await r.json();alert(d.msg);ld()}catch(e){alert('Error: '+e.message)}}

async function fs(){try{await fetch('/api/force_scan',{method:'POST'});document.getElementById('ct').innerHTML='<div class="ld"><div class="sp"></div><br>Escaneando...</div>';setTimeout(ld,6000)}catch(e){}}

async function showCfg(){
const p=document.getElementById('cfgPanel');
if(p&&p.style.display!=='none'){p.style.display='none';return}
try{const r=await fetch('/api/config');const c=await r.json();
if(!p)return;
p.innerHTML=`<div class="ac" style="margin-top:6px">
<div class="at">⚙️ Configuración</div>
<div style="display:grid;grid-template-columns:1fr 1fr;gap:6px;margin-top:8px;font-size:.68em">
<label>Capital USD<br><input id="cc" type="number" value="${c.capital}" style="width:100%;background:#0a0b0d;border:1px solid #333;color:#fff;padding:5px;border-radius:4px;font-family:inherit"></label>
<label>Scan min<br><input id="csm" type="number" value="${c.scan_minutes}" style="width:100%;background:#0a0b0d;border:1px solid #333;color:#fff;padding:5px;border-radius:4px;font-family:inherit"></label>
<label>% Seguro<br><input id="csp" type="number" value="${c.safe_pct}" style="width:100%;background:#0a0b0d;border:1px solid #333;color:#fff;padding:5px;border-radius:4px;font-family:inherit"></label>
<label>Vol mín<br><input id="cmv" type="number" value="${c.min_volume}" style="width:100%;background:#0a0b0d;border:1px solid #333;color:#fff;padding:5px;border-radius:4px;font-family:inherit"></label>
<label>APR mín safe<br><input id="cas" type="number" value="${c.min_apr_safe}" style="width:100%;background:#0a0b0d;border:1px solid #333;color:#fff;padding:5px;border-radius:4px;font-family:inherit"></label>
<label>APR mín aggr<br><input id="caa" type="number" value="${c.min_apr_aggr}" style="width:100%;background:#0a0b0d;border:1px solid #333;color:#fff;padding:5px;border-radius:4px;font-family:inherit"></label>
<label>Score mín<br><input id="cms" type="number" value="${c.min_score}" style="width:100%;background:#0a0b0d;border:1px solid #333;color:#fff;padding:5px;border-radius:4px;font-family:inherit"></label>
<label>Máx pos safe<br><input id="cps" type="number" value="${c.max_pos_safe}" style="width:100%;background:#0a0b0d;border:1px solid #333;color:#fff;padding:5px;border-radius:4px;font-family:inherit"></label>
</div>
<div style="margin-top:8px"><button class="btn bg" onclick="saveCfg()">💾 Guardar</button> <button class="btn bgy" onclick="document.getElementById('cfgPanel').style.display='none'">Cerrar</button></div></div>`;
p.style.display='block'}catch(e){alert('Error: '+e.message)}}

async function saveCfg(){
const body={capital:+document.getElementById('cc').value,scan_minutes:+document.getElementById('csm').value,
safe_pct:+document.getElementById('csp').value,min_volume:+document.getElementById('cmv').value,
min_apr_safe:+document.getElementById('cas').value,min_apr_aggr:+document.getElementById('caa').value,
min_score:+document.getElementById('cms').value,max_pos_safe:+document.getElementById('cps').value};
try{const r=await fetch('/api/config',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});
const d=await r.json();alert(d.msg);document.getElementById('cfgPanel').style.display='none';ld()}catch(e){alert('Error: '+e.message)}}

ld();setInterval(ld,30000);
</script></body></html>"""


# ═══════════════════════════════════════════════════════════════
#  STARTUP
# ═══════════════════════════════════════════════════════════════
load_state()
if STATE["scan_count"] == 0:
    STATE["total_capital"] = CAPITAL; STATE["scan_interval"] = SCAN_MIN * 60
    STATE["min_volume"] = MIN_VOL; STATE["safe_pct"] = SAFE_PCT; STATE["aggr_pct"] = AGGR_PCT
log.info(f"Bot v6.0: ${STATE['total_capital']:,.0f} │ {STATE['safe_pct']}/{STATE['aggr_pct']} │ {STATE['scan_interval']//60}min │ {DATA_DIR}")

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False)
