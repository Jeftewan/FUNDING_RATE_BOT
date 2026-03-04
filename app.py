#!/usr/bin/env python3
"""
Funding Rate Portfolio Manager v4.1 — Railway Edition
Fixed: Volume persistence, gunicorn thread startup, API error handling
"""

import requests as req
import time, json, os, threading, logging
from datetime import datetime
from flask import Flask, jsonify, request as flask_req, Response

# ═══════════════════════════════════════════════════════════════
#  LOGGING
# ═══════════════════════════════════════════════════════════════
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

# Railway Volume se monta en /app/data
# Si corre local, usa el directorio actual
DATA_DIR = os.environ.get("DATA_DIR", "/app/data")
if not os.path.exists(DATA_DIR):
    try:
        os.makedirs(DATA_DIR, exist_ok=True)
        log.info(f"Directorio creado: {DATA_DIR}")
    except:
        DATA_DIR = "."
        log.warning(f"No se pudo crear /app/data, usando directorio actual")

STATE_FILE = os.path.join(DATA_DIR, "portfolio_state.json")
log.info(f"Archivo de estado: {STATE_FILE}")

LOCK = threading.Lock()
_scanner_started = False

STATE = {
    "total_capital": CAPITAL,
    "scan_interval": SCAN_MIN * 60,
    "min_volume": MIN_VOL,
    "safe_pct": SAFE_PCT,
    "aggr_pct": AGGR_PCT,
    "reserve_pct": 10,
    "max_pos_safe": 2,
    "max_pos_aggr": 1,
    "min_apr_safe": 5,
    "min_apr_aggr": 15,
    "min_score": 40,
    "positions": [],
    "history": [],
    "total_earned": 0,
    "last_scan": 0,
    "scan_count": 0,
    "safe_top": [],
    "aggr_top": [],
    "all_data": [],
    "actions": [],
    "last_scan_time": "—",
    "status": "Iniciando...",
    "last_error": "",
}

FEES = {
    "Binance": {"spot": 0.10, "fut": 0.05},
    "Bybit":   {"spot": 0.10, "fut": 0.06},
}


# ═══════════════════════════════════════════════════════════════
#  PERSISTENCE — writes to Railway Volume
# ═══════════════════════════════════════════════════════════════
def save_state():
    saveable = {k: v for k, v in STATE.items() if k not in ["all_data"]}
    try:
        tmp = STATE_FILE + ".tmp"
        with open(tmp, "w") as f:
            json.dump(saveable, f, indent=2, default=str)
        os.replace(tmp, STATE_FILE)
    except Exception as e:
        log.error(f"Error guardando estado: {e}")

def load_state():
    try:
        with open(STATE_FILE, "r") as f:
            saved = json.load(f)
        for k, v in saved.items():
            if k in STATE:
                STATE[k] = v
        log.info(f"Estado cargado: {len(STATE['positions'])} posiciones, ${STATE['total_earned']:.2f} ganado")
    except FileNotFoundError:
        log.info("Sin estado previo, iniciando nuevo")
    except Exception as e:
        log.error(f"Error cargando estado: {e}")


# ═══════════════════════════════════════════════════════════════
#  HTTP HELPER — with retries
# ═══════════════════════════════════════════════════════════════
def _get(url, params=None, retries=2):
    for attempt in range(retries + 1):
        try:
            r = req.get(url, params=params, timeout=20)
            r.raise_for_status()
            return r.json()
        except req.exceptions.Timeout:
            log.warning(f"Timeout {url} (intento {attempt+1})")
            if attempt < retries:
                time.sleep(2)
        except req.exceptions.ConnectionError:
            log.warning(f"ConnectionError {url} (intento {attempt+1})")
            if attempt < retries:
                time.sleep(3)
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
    if _bn_iv:
        return _bn_iv
    d = _get("https://fapi.binance.com/fapi/v1/fundingInfo")
    if d:
        for x in d:
            _bn_iv[x.get("symbol", "")] = x.get("fundingIntervalHours", 8)
        log.info(f"Intervalos Binance cargados: {len(_bn_iv)} pares")
    return _bn_iv

def fetch_binance():
    fd = _get("https://fapi.binance.com/fapi/v1/premiumIndex")
    vd = _get("https://fapi.binance.com/fapi/v1/ticker/24hr")
    if not fd:
        log.error("Binance premiumIndex falló")
        return []
    ivs = fetch_bn_intervals()
    vm = {}
    if vd:
        for v in vd:
            s = v.get("symbol", "")
            if s.endswith("USDT"):
                vm[s] = float(v.get("quoteVolume", 0))
    out = []
    for x in fd:
        s = x.get("symbol", "")
        if not s.endswith("USDT"):
            continue
        ih = ivs.get(s, 8)
        nxt = int(x.get("nextFundingTime", 0))
        mn = max(0, (nxt / 1000 - time.time()) / 60) if nxt > 0 else -1
        out.append({
            "symbol": s.replace("USDT", ""), "pair": s, "exchange": "Binance",
            "fr": float(x.get("lastFundingRate", 0)),
            "price": float(x.get("markPrice", 0)),
            "vol24h": vm.get(s, 0), "ih": ih, "ipd": 24 / ih, "mins_next": mn,
        })
    log.info(f"Binance: {len(out)} pares")
    return out

def fetch_bybit():
    d = _get("https://api.bybit.com/v5/market/tickers", params={"category": "linear"})
    if not d:
        log.error("Bybit tickers falló")
        return []
    out = []
    for x in d.get("result", {}).get("list", []):
        s = x.get("symbol", "")
        if not s.endswith("USDT"):
            continue
        out.append({
            "symbol": s.replace("USDT", ""), "pair": s, "exchange": "Bybit",
            "fr": float(x.get("fundingRate", 0)),
            "price": float(x.get("markPrice", 0)),
            "vol24h": float(x.get("turnover24h", 0)),
            "ih": 8, "ipd": 3, "mins_next": -1,
        })
    log.info(f"Bybit: {len(out)} pares")
    return out

def fetch_hist(sym, exch, lim=15):
    if exch == "Binance":
        d = _get(f"https://fapi.binance.com/fapi/v1/fundingRate",
                 params={"symbol": f"{sym}USDT", "limit": lim}, retries=1)
        if not d:
            return [], []
        return [float(x["fundingRate"]) for x in d], [int(x.get("fundingTime", 0)) for x in d]
    else:
        d = _get("https://api.bybit.com/v5/market/funding/history",
                 params={"category": "linear", "symbol": f"{sym}USDT", "limit": lim}, retries=1)
        if not d:
            return [], []
        items = d.get("result", {}).get("list", [])
        return [float(x["fundingRate"]) for x in items], [int(x.get("fundingRateTimestamp", 0)) for x in items]

def detect_bb_iv(tss):
    if len(tss) < 2:
        return 8
    diffs = [abs(tss[i] - tss[i + 1]) / (1000 * 3600) for i in range(min(3, len(tss) - 1))]
    avg = sum(diffs) / len(diffs) if diffs else 8
    for iv in [1, 2, 4, 8]:
        if abs(avg - iv) < 1:
            return iv
    return 8


# ═══════════════════════════════════════════════════════════════
#  ANALYSIS
# ═══════════════════════════════════════════════════════════════
def analyze_consist(hist, fr_sign):
    if not hist:
        return {"avg": 0, "pct": 0, "streak": 0, "ok": False}
    fav = sum(1 for r in hist if (fr_sign > 0 and r > 0) or (fr_sign < 0 and r < 0))
    pct = fav / len(hist) * 100
    streak = 0
    for r in hist:
        if (fr_sign > 0 and r > 0) or (fr_sign < 0 and r < 0):
            streak += 1
        else:
            break
    return {"avg": sum(hist) / len(hist), "pct": pct, "streak": streak, "ok": pct >= 70 and streak >= 3}

def est_slippage(vol, size):
    if vol <= 0:
        return 0.5
    r = size / vol
    if r < 0.00001: return 0.01
    if r < 0.0001:  return 0.03
    if r < 0.001:   return 0.05
    if r < 0.01:    return 0.10
    return 0.20

def calc_returns(token, capital):
    is_pos = token["fr"] > 0
    ih = token.get("ih", 8)
    ipd = token.get("ipd", 24 / ih)
    reserve = capital * 0.10
    working = capital - reserve
    spot = working / 2 if is_pos else 0
    fut = working / 2
    fi = FEES.get(token["exchange"], FEES["Binance"])
    fee_in = spot * (fi["spot"] / 100) + fut * (fi["fut"] / 100)
    total_fees = fee_in * 2
    slip = est_slippage(token["vol24h"], spot + fut)
    slip_cost = (spot + fut) * (slip / 100) * 2
    total_cost = total_fees + slip_cost
    afr = abs(token["fr"])
    fpi = fut * afr
    fd = fpi * ipd
    fa = fd * 365
    apr = (fa / capital) * 100 if capital > 0 else 0
    p7 = fd * 7 - total_cost
    be = total_cost / fd if fd > 0 else 999
    carry = "Positive" if is_pos else "Reverse"
    mdp = (fd / fut * 100) if (not is_pos and fut > 0) else 0
    return {
        "spot": spot, "fut": fut, "reserve": reserve,
        "total_fees": total_fees, "slip_cost": slip_cost,
        "total_cost": total_cost, "slip_pct": slip,
        "fpi": fpi, "fd": fd, "apr": apr, "p7": p7, "be": be,
        "carry": carry, "ih": ih, "ipd": ipd, "mdp": mdp,
        "worthwhile": be < 5 and apr > 5,
    }

def risk_score(token, hist):
    sc = 0
    afr = abs(token["fr"]) * 100
    if afr > 0.05:   sc += 30
    elif afr > 0.02: sc += 20
    elif afr > 0.01: sc += 10
    else:            sc += 5
    if hist["ok"]:         sc += 30
    elif hist["pct"] > 60: sc += 20
    elif hist["pct"] > 40: sc += 10
    if token["vol24h"] > 100e6:  sc += 20
    elif token["vol24h"] > 20e6: sc += 15
    elif token["vol24h"] > 5e6:  sc += 10
    if token.get("ipd", 3) >= 6:  sc += 10
    elif token.get("ipd", 3) >= 4: sc += 5
    return min(sc, 100)


# ═══════════════════════════════════════════════════════════════
#  PORTFOLIO
# ═══════════════════════════════════════════════════════════════
def get_bd():
    t = STATE["total_capital"]
    sb = t * (STATE["safe_pct"] / 100)
    ab = t * (STATE["aggr_pct"] / 100)
    su = sum(p["capital_used"] for p in STATE["positions"] if p["carry"] == "Positive")
    au = sum(p["capital_used"] for p in STATE["positions"] if p["carry"] == "Reverse")
    sc = sum(1 for p in STATE["positions"] if p["carry"] == "Positive")
    ac = sum(1 for p in STATE["positions"] if p["carry"] == "Reverse")
    return {"total": t, "sb": sb, "ab": ab, "su": su, "au": au,
            "sa": max(0, sb - su), "aa": max(0, ab - au), "sc": sc, "ac": ac}

def gen_actions():
    actions = []
    bd = get_bd()
    positions = STATE["positions"]
    all_data = STATE["all_data"]
    safe_top = STATE["safe_top"]
    aggr_top = STATE["aggr_top"]

    for i, pos in enumerate(positions):
        cur = next((d for d in all_data if d["symbol"] == pos["symbol"] and d["exchange"] == pos["exchange"]), None)
        if not cur:
            continue
        cfr = cur["fr"]
        fr_rev = (pos["entry_fr"] > 0 and cfr < 0) or (pos["entry_fr"] < 0 and cfr > 0)
        fr_drop = abs(cfr) < abs(pos["entry_fr"]) * 0.25 and not fr_rev
        cc = calc_returns(cur, pos["capital_used"])

        if fr_rev:
            actions.append({"pri": 0, "type": "EXIT", "idx": i, "critical": True,
                "title": f"⛔ CERRAR {pos['symbol']} ({pos['exchange']}) — Funding cambió de signo",
                "detail": f"Entrada: {pos['entry_fr']*100:.4f}% → Ahora: {cfr*100:.4f}%",
                "steps": [], "costs": "", "warning": "", "countdown": ""})
        elif fr_drop:
            better = None
            pool = safe_top if pos["carry"] == "Positive" else aggr_top
            for opp in pool:
                if opp["token"]["symbol"] != pos["symbol"]:
                    oc = calc_returns(opp["token"], pos["capital_used"])
                    if oc["apr"] > cc["apr"] * 2:
                        better = opp
                        break
            if better:
                bc = calc_returns(better["token"], pos["capital_used"])
                actions.append({"pri": 1, "type": "ROTATE", "idx": i, "critical": False,
                    "title": f"🔄 ROTAR: {pos['symbol']} → {better['token']['symbol']} ({better['token']['exchange']})",
                    "detail": f"APR: {cc['apr']:.1f}% → {bc['apr']:.1f}%",
                    "new_sym": better["token"]["symbol"], "new_exch": better["token"]["exchange"],
                    "steps": [], "costs": "", "warning": "", "countdown": ""})

    def add_open(pool, slots, cap_avail, carry_label, min_apr, pri):
        if slots <= 0 or cap_avail <= 20:
            return
        cpp = cap_avail / slots
        for opp in pool[:slots]:
            c = calc_returns(opp["token"], cpp)
            if not c["worthwhile"] or c["apr"] < min_apr or opp["score"] < STATE["min_score"]:
                continue
            if any(p["symbol"] == opp["token"]["symbol"] and p["exchange"] == opp["token"]["exchange"] for p in positions):
                continue
            t = opp["token"]
            emoji = "🛡️" if carry_label == "safe" else "⚡"
            if c["carry"] == "Positive":
                steps = [
                    f"1. COMPRA {t['symbol']} en SPOT por ${c['spot']:.2f}",
                    f"2. Abre SHORT {t['symbol']}USDT PERPETUO por ${c['fut']:.2f}",
                    f"   → Leverage: 1x │ Cross Margin",
                    f"3. Reserva: ${c['reserve']:.2f}",
                ]
            else:
                steps = [
                    f"1. Abre LONG {t['symbol']}USDT PERPETUO por ${c['fut']:.2f}",
                    f"   → Leverage: 1x │ Cross Margin",
                    f"2. NO comprar en spot",
                    f"3. Mantener ${c['reserve'] + cpp / 2:.2f} como margen",
                ]
            actions.append({
                "pri": pri, "type": "OPEN", "carry": carry_label, "critical": False,
                "title": f"{emoji} ABRIR: {t['symbol']}/USDT en {t['exchange']}",
                "detail": f"APR: {c['apr']:.1f}% │ ${c['fd']:.2f}/día │ Breakeven: {c['be']:.1f}d │ Score: {opp['score']}/100",
                "steps": steps,
                "costs": f"Fees: ${c['total_fees']:.2f} │ Slippage: ~${c['slip_cost']:.2f} ({c['slip_pct']:.2f}%) │ Total: ${c['total_cost']:.2f}",
                "countdown": f"⏱ Próximo cobro en {int(t['mins_next'])}min" if t.get("mins_next", 0) > 0 else "",
                "warning": f"⚠ Compensa caídas hasta ~{c['mdp']:.2f}%/día" if c["carry"] == "Reverse" else "",
                "symbol": t["symbol"], "exchange": t["exchange"],
                "capital": cpp, "fr": t["fr"], "price": t["price"],
                "ih": c["ih"], "carry_type": c["carry"],
            })

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
#  SCANNER
# ═══════════════════════════════════════════════════════════════
def run_scan():
    log.info("Scan iniciando...")
    with LOCK:
        STATE["status"] = "Escaneando..."

    bn = fetch_binance()
    bb = fetch_bybit()
    all_data = bn + bb

    if not all_data:
        msg = "Error: no se pudo conectar a Binance ni Bybit"
        log.error(msg)
        with LOCK:
            STATE["status"] = msg
            STATE["last_error"] = msg
        return

    mv = STATE["min_volume"]
    pos_l = sorted([t for t in all_data if t["fr"] > 0.0001 and t["vol24h"] >= mv],
                   key=lambda x: x["fr"], reverse=True)
    neg_l = sorted([t for t in all_data if t["fr"] < -0.0001 and t["vol24h"] >= mv],
                   key=lambda x: x["fr"])

    def analyze(tokens, lim=6):
        scored = []
        for t in tokens[:lim]:
            rates, tss = fetch_hist(t["symbol"], t["exchange"])
            time.sleep(0.1)
            if t["exchange"] == "Bybit" and tss:
                t["ih"] = detect_bb_iv(tss)
                t["ipd"] = 24 / t["ih"]
            h = analyze_consist(rates, t["fr"])
            sc = risk_score(t, h)
            scored.append({"token": t, "hist": h, "score": sc})
        scored.sort(key=lambda x: x["score"], reverse=True)
        return scored[:3]

    safe = analyze(pos_l)
    aggr = analyze(neg_l)

    with LOCK:
        STATE["all_data"] = all_data
        STATE["safe_top"] = safe
        STATE["aggr_top"] = aggr
        STATE["last_scan"] = time.time()
        STATE["scan_count"] += 1
        STATE["last_scan_time"] = datetime.now().strftime("%H:%M:%S")
        STATE["actions"] = gen_actions()
        STATE["status"] = f"OK — {len(bn)} Binance + {len(bb)} Bybit │ {len(pos_l)} pos │ {len(neg_l)} neg"
        STATE["last_error"] = ""
        save_state()

    log.info(f"Scan #{STATE['scan_count']} completo: {len(pos_l)} positivos, {len(neg_l)} negativos")

def scanner_loop():
    log.info("Scanner thread iniciado")
    time.sleep(5)  # Esperar a que gunicorn esté listo
    while True:
        try:
            run_scan()
        except Exception as e:
            log.exception(f"Error en scan: {e}")
            with LOCK:
                STATE["status"] = f"Error: {str(e)[:80]}"
                STATE["last_error"] = str(e)
        time.sleep(STATE["scan_interval"])

def ensure_scanner():
    """Inicia el scanner thread una sola vez, seguro con gunicorn."""
    global _scanner_started
    if not _scanner_started:
        _scanner_started = True
        t = threading.Thread(target=scanner_loop, daemon=True)
        t.start()
        log.info("Scanner thread lanzado")


# ═══════════════════════════════════════════════════════════════
#  ROUTES
# ═══════════════════════════════════════════════════════════════
@app.before_request
def _before():
    ensure_scanner()

@app.route("/health")
def health():
    return jsonify({"ok": True, "scan_count": STATE["scan_count"],
                    "status": STATE["status"], "positions": len(STATE["positions"])})

@app.route("/api/state")
def api_state():
    with LOCK:
        bd = get_bd()
        pdata = []
        for pos in STATE["positions"]:
            cur = next((d for d in STATE["all_data"]
                        if d["symbol"] == pos["symbol"] and d["exchange"] == pos["exchange"]), None)
            cfr = cur["fr"] if cur else pos["entry_fr"]
            cp = cur["price"] if cur else pos["entry_price"]
            pc = ((cp - pos["entry_price"]) / pos["entry_price"] * 100) if pos["entry_price"] > 0 else 0
            ih = pos.get("ih", cur.get("ih", 8) if cur else 8)
            el_h = (time.time() - pos["entry_time"] / 1000) / 3600
            ivs = int(el_h / ih)
            c = calc_returns({"fr": cfr, "price": cp, "symbol": pos["symbol"],
                "exchange": pos["exchange"], "vol24h": 0, "ih": ih, "ipd": 24 / ih}, pos["capital_used"])
            est = c["fpi"] * ivs
            pp = c["fut"] * (pc / 100) if pos["carry"] == "Reverse" else 0
            fr_rev = (pos["entry_fr"] > 0 and cfr < 0) or (pos["entry_fr"] < 0 and cfr > 0)
            pdata.append({**pos, "current_fr": cfr, "current_price": cp, "price_change": pc,
                "elapsed_h": el_h, "intervals": ivs, "est_earned": est, "price_pnl": pp,
                "total_pnl": est + pp, "current_apr": c["apr"], "fr_reversed": fr_rev,
                "mins_next": cur.get("mins_next", -1) if cur else -1})

        return jsonify({
            "capital": STATE["total_capital"], "earned": STATE.get("total_earned", 0),
            "breakdown": bd, "positions": pdata, "actions": STATE["actions"],
            "safe_top": [{"token": o["token"], "hist": o["hist"], "score": o["score"],
                "calc": calc_returns(o["token"], max(bd["sa"] / max(1, STATE["max_pos_safe"] - bd["sc"]), 50))}
                for o in STATE["safe_top"]],
            "aggr_top": [{"token": o["token"], "hist": o["hist"], "score": o["score"],
                "calc": calc_returns(o["token"], max(bd["aa"] / max(1, STATE["max_pos_aggr"] - bd["ac"]), 50))}
                for o in STATE["aggr_top"]],
            "status": STATE["status"], "scan_count": STATE["scan_count"],
            "last_scan": STATE["last_scan_time"], "scan_interval": STATE["scan_interval"],
            "last_error": STATE.get("last_error", ""),
        })


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
            STATE["positions"].append({
                "symbol": act["symbol"], "exchange": act["exchange"],
                "entry_fr": act["fr"], "entry_price": act["price"],
                "entry_time": int(time.time() * 1000),
                "carry": act["carry_type"], "capital_used": act["capital"],
                "ih": act.get("ih", 8),
            })
            save_state()
            STATE["actions"] = gen_actions()
            log.info(f"Posición abierta: {act['symbol']} {act['carry_type']}")
            return jsonify({"ok": True, "msg": f"✅ {act['symbol']} registrada"})

        elif act["type"] == "EXIT":
            i = act["idx"]
            if i < len(STATE["positions"]):
                pos = STATE["positions"][i]
                ih = pos.get("ih", 8)
                el_h = (time.time() - pos["entry_time"] / 1000) / 3600
                ivs = int(el_h / ih)
                c = calc_returns({"fr": pos["entry_fr"], "price": pos["entry_price"],
                    "symbol": pos["symbol"], "exchange": pos["exchange"],
                    "vol24h": 0, "ih": ih, "ipd": 24 / ih}, pos["capital_used"])
                est = c["fpi"] * ivs
                STATE["history"].append({"symbol": pos["symbol"], "exchange": pos["exchange"],
                    "carry": pos["carry"], "hours": el_h, "intervals": ivs, "earned": est,
                    "time": datetime.now().isoformat()})
                STATE["total_earned"] = STATE.get("total_earned", 0) + est
                STATE["positions"].pop(i)
                save_state()
                STATE["actions"] = gen_actions()
                log.info(f"Posición cerrada: {pos['symbol']} ganó ${est:.2f}")
                return jsonify({"ok": True, "msg": f"✅ {pos['symbol']} cerrada. Ganado: ${est:.2f}"})

        elif act["type"] == "ROTATE":
            i = act["idx"]
            if i < len(STATE["positions"]):
                pos = STATE["positions"][i]
                cap = pos["capital_used"]
                ih = pos.get("ih", 8)
                el_h = (time.time() - pos["entry_time"] / 1000) / 3600
                ivs = int(el_h / ih)
                c = calc_returns({"fr": pos["entry_fr"], "price": pos["entry_price"],
                    "symbol": pos["symbol"], "exchange": pos["exchange"],
                    "vol24h": 0, "ih": ih, "ipd": 24 / ih}, cap)
                est = c["fpi"] * ivs
                STATE["history"].append({"symbol": pos["symbol"], "exchange": pos["exchange"],
                    "carry": pos["carry"], "hours": el_h, "intervals": ivs, "earned": est,
                    "time": datetime.now().isoformat()})
                STATE["total_earned"] = STATE.get("total_earned", 0) + est
                STATE["positions"].pop(i)
                ns = act.get("new_sym")
                ne = act.get("new_exch")
                cur = next((d for d in STATE["all_data"] if d["symbol"] == ns and d["exchange"] == ne), None)
                if cur:
                    nc = calc_returns(cur, cap)
                    STATE["positions"].append({"symbol": ns, "exchange": ne,
                        "entry_fr": cur["fr"], "entry_price": cur["price"],
                        "entry_time": int(time.time() * 1000),
                        "carry": nc["carry"], "capital_used": cap, "ih": nc["ih"]})
                save_state()
                STATE["actions"] = gen_actions()
                log.info(f"Rotación: {pos['symbol']} → {ns}")
                return jsonify({"ok": True, "msg": f"✅ Rotación: {pos['symbol']} → {ns}"})

    return jsonify({"ok": False, "msg": "Error procesando"})


@app.route("/api/force_scan", methods=["POST"])
def api_force():
    threading.Thread(target=run_scan, daemon=True).start()
    return jsonify({"ok": True, "msg": "Scan iniciado"})


# ═══════════════════════════════════════════════════════════════
#  HTML
# ═══════════════════════════════════════════════════════════════
@app.route("/")
def index():
    return Response(HTML_PAGE, content_type="text/html; charset=utf-8")

HTML_PAGE = r"""<!DOCTYPE html>
<html lang="es">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Funding Rate Bot</title>
<style>
@import url('https://fonts.googleapis.com/css2?family=JetBrains+Mono:wght@300;400;500;700&display=swap');
*{margin:0;padding:0;box-sizing:border-box}
body{font-family:'JetBrains Mono',monospace;background:#0a0b0d;color:#c8ccd0;min-height:100vh}
.c{max-width:900px;margin:0 auto;padding:12px}
.hdr{text-align:center;padding:16px 0 8px;border-bottom:1px solid #1a1d23}
.hdr h1{font-size:1.2em;color:#fff}.hdr .s{font-size:.62em;color:#555;margin-top:2px}
.cap{display:grid;grid-template-columns:1fr 1fr 1fr;gap:10px;padding:12px 0;border-bottom:1px solid #1a1d23}
.cap .i{text-align:center}.cap .l{font-size:.6em;color:#666;text-transform:uppercase;letter-spacing:1px}
.cap .v{font-size:1.1em;font-weight:700;color:#fff}.cap .v.g{color:#34d399}
.bd{display:grid;grid-template-columns:1fr 1fr;gap:8px;padding:10px 0;border-bottom:1px solid #1a1d23}
.bdi{background:#111318;border-radius:8px;padding:8px 12px;border-left:3px solid}
.bdi.sf{border-color:#34d399}.bdi.ag{border-color:#fbbf24}
.bdi .t{font-size:.65em;color:#888}.bdi .vl{font-size:.85em;color:#fff;font-weight:600}.bdi .sb{font-size:.6em;color:#555}
.st{font-size:.75em;font-weight:700;color:#fff;padding:14px 0 6px;text-transform:uppercase;letter-spacing:1px}
.ac{border-radius:10px;padding:12px;margin-bottom:8px;border:1px solid #1a1d23;background:#111318}
.ac.cr{border-color:#ef4444;background:#1a0a0a;animation:p 1.5s infinite}
.ac.so{border-color:#34d39944}.ac.ao{border-color:#fbbf2444}
.ac .at{font-size:.82em;font-weight:700;color:#fff}.ac .ad{font-size:.68em;color:#888;margin-top:3px}
.ac .as{margin:8px 0;padding:8px;background:#0a0b0d;border-radius:6px;font-size:.7em;white-space:pre-line}
.ac .acs{font-size:.62em;color:#666}.ac .aw{font-size:.68em;color:#fbbf24;margin-top:3px}
.ac .acd{font-size:.68em;color:#22d3ee;margin-top:3px}
.btn{display:inline-block;padding:8px 18px;border-radius:6px;font-family:inherit;font-size:.72em;font-weight:600;cursor:pointer;border:none;margin-top:6px;transition:.2s}
.bg{background:#059669;color:#fff}.bg:hover{background:#34d399}
.br{background:#dc2626;color:#fff}.br:hover{background:#ef4444}
.by{background:#d97706;color:#fff}.by:hover{background:#fbbf24;color:#000}
.bgy{background:#374151;color:#ccc}.bgy:hover{background:#4b5563}
.btn:disabled{opacity:.5;cursor:not-allowed}
.pc{border-radius:10px;padding:10px;margin-bottom:6px;border:1px solid #1a1d23;background:#111318}
.pc.al{border-color:#ef4444;background:#1a0a0a}
.ph{display:flex;align-items:center;gap:6px;margin-bottom:4px;flex-wrap:wrap}
.ps{font-weight:700;color:#fff}
.badge{font-size:.58em;padding:2px 7px;border-radius:99px;border:1px solid}
.bsf{color:#34d399;border-color:#34d39944;background:#34d39911}
.bag{color:#fbbf24;border-color:#fbbf2444;background:#fbbf2411}
.bbn{color:#f0b90b;border-color:#f0b90b44;background:#f0b90b11}
.bbb{color:#a78bfa;border-color:#a78bfa44;background:#a78bfa11}
.pg{display:grid;grid-template-columns:repeat(auto-fit,minmax(120px,1fr));gap:4px}
.pm .pl{font-size:.58em;color:#555;text-transform:uppercase}.pm .pv{font-size:.78em;font-weight:600;color:#fff}
.pv.g{color:#34d399}.pv.r{color:#ef4444}.pv.y{color:#fbbf24}
.sbar{text-align:center;padding:8px 0;font-size:.6em;color:#444;border-top:1px solid #1a1d23;margin-top:12px}
.err{text-align:center;padding:6px;font-size:.65em;color:#ef4444;background:#1a0a0a;border-radius:6px;margin:8px 0}
.tr{display:grid;grid-template-columns:1fr 1fr;gap:6px}
.om{background:#111318;border-radius:6px;padding:8px;font-size:.68em;border:1px solid #1a1d23;margin-bottom:4px}
.om .os{font-weight:700;color:#fff}.om .of{color:#34d399}.om .of.n{color:#ef4444}
.scr{text-align:center;padding:8px 0}
.alrt{background:#dc2626;color:#fff;padding:6px 10px;border-radius:6px;font-size:.72em;font-weight:700;margin-bottom:6px}
.ld{text-align:center;padding:60px 20px;color:#555}
.ld .sp{display:inline-block;width:24px;height:24px;border:2px solid #333;border-top-color:#34d399;border-radius:50%;animation:spin 1s linear infinite;margin-bottom:12px}
@keyframes p{0%,100%{opacity:1}50%{opacity:.85}}
@keyframes spin{to{transform:rotate(360deg)}}
@media(max-width:600px){.cap{grid-template-columns:1fr}.tr{grid-template-columns:1fr}.pg{grid-template-columns:1fr 1fr}}
</style>
</head>
<body>
<div class="c">
<div class="hdr"><h1>💰 Funding Rate Portfolio Manager</h1>
<div class="s">Spot + Futuros │ Delta Neutral │ 24/7</div></div>
<div id="ct"><div class="ld"><div class="sp"></div><br>Esperando primer scan...<br><span style="font-size:.8em">Esto toma ~15 segundos</span></div></div>
</div>
<script>
let lastErr='';
async function ld(){
try{
const r=await fetch('/api/state');
if(!r.ok)throw new Error('HTTP '+r.status);
const s=await r.json();rn(s);lastErr='';
}catch(e){
if(lastErr!==e.message){lastErr=e.message;
document.getElementById('ct').innerHTML=`<div class="err">Error: ${e.message}. Reintentando cada 30s...</div>`}}}

function rn(s){
const bd=s.breakdown;let h='';
h+=`<div class="cap">
<div class="i"><div class="l">Capital</div><div class="v">$${s.capital.toLocaleString()}</div></div>
<div class="i"><div class="l">Ganado</div><div class="v g">$${s.earned.toFixed(2)}</div></div>
<div class="i"><div class="l">En Uso</div><div class="v">$${(bd.su+bd.au).toFixed(0)}</div></div></div>`;
h+=`<div class="bd">
<div class="bdi sf"><div class="t">🛡️ Seguro (${bd.sc} pos)</div><div class="vl">$${bd.sb.toFixed(0)}</div><div class="sb">Libre: $${bd.sa.toFixed(0)}</div></div>
<div class="bdi ag"><div class="t">⚡ Agresivo (${bd.ac} pos)</div><div class="vl">$${bd.ab.toFixed(0)}</div><div class="sb">Libre: $${bd.aa.toFixed(0)}</div></div></div>`;
if(s.last_error)h+=`<div class="err">${s.last_error}</div>`;
if(s.positions.length>0){
h+='<div class="st">📊 Posiciones Activas</div>';
s.positions.forEach(p=>{
const e=p.carry==='Positive'?'🛡️':'⚡';const bc=p.carry==='Positive'?'bsf':'bag';
const ec=p.exchange==='Binance'?'bbn':'bbb';const cd=p.mins_next>0?`⏱${Math.floor(p.mins_next)}m`:'';
h+=`<div class="pc ${p.fr_reversed?'al':''}">
<div class="ph"><span>${e}</span><span class="ps">${p.symbol}</span>
<span class="badge ${bc}">${p.carry}</span><span class="badge ${ec}">${p.exchange}</span>
<span style="font-size:.6em;color:#555;margin-left:auto">$${p.capital_used.toFixed(0)} │ ${p.elapsed_h.toFixed(1)}h │ ${p.intervals} cobros ${cd}</span></div>
${p.fr_reversed?'<div class="alrt">⛔ FUNDING CAMBIÓ — CERRAR AHORA</div>':''}
<div class="pg">
<div class="pm"><div class="pl">FR</div><div class="pv ${p.current_fr>0?'g':'r'}">${(p.current_fr*100).toFixed(4)}%</div></div>
<div class="pm"><div class="pl">APR</div><div class="pv ${p.current_apr>10?'g':'y'}">${p.current_apr.toFixed(1)}%</div></div>
<div class="pm"><div class="pl">Ganado</div><div class="pv g">$${p.est_earned.toFixed(2)}</div></div>
${p.carry==='Reverse'?`<div class="pm"><div class="pl">P&L Precio</div><div class="pv ${p.price_pnl>=0?'g':'r'}">$${p.price_pnl.toFixed(2)}</div></div>`:''}
<div class="pm"><div class="pl">Total</div><div class="pv ${p.total_pnl>=0?'g':'r'}">$${p.total_pnl.toFixed(2)}</div></div>
</div></div>`})}
if(s.actions.length>0){
h+='<div class="st">🎯 Acciones — Haz Esto</div>';
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
if(s.safe_top.length||s.aggr_top.length){
h+='<div class="st">📈 Top del Mercado</div><div class="tr">';
if(s.safe_top.length){h+='<div><div style="font-size:.65em;color:#34d399;margin-bottom:4px">🛡️ Seguras</div>';
s.safe_top.forEach(o=>{const t=o.token;
h+=`<div class="om"><span class="os">${t.symbol}</span> <span class="badge ${t.exchange==='Binance'?'bbn':'bbb'}">${t.exchange}</span>
<span class="of">+${(t.fr*100).toFixed(4)}%/${t.ih}h</span> <span style="color:#666">S:${o.score} APR:${o.calc.apr.toFixed(1)}%</span></div>`});h+='</div>'}
if(s.aggr_top.length){h+='<div><div style="font-size:.65em;color:#fbbf24;margin-bottom:4px">⚡ Agresivas</div>';
s.aggr_top.forEach(o=>{const t=o.token;
h+=`<div class="om"><span class="os">${t.symbol}</span> <span class="badge ${t.exchange==='Binance'?'bbn':'bbb'}">${t.exchange}</span>
<span class="of n">${(t.fr*100).toFixed(4)}%/${t.ih}h</span> <span style="color:#666">S:${o.score} APR:${o.calc.apr.toFixed(1)}%</span></div>`});h+='</div>'}
h+='</div>'}
h+=`<div class="scr"><button class="btn bgy" onclick="fs()">🔍 Escanear Ahora</button></div>`;
h+=`<div class="sbar">${s.status} │ Scan #${s.scan_count} │ ${s.last_scan} │ Cada ${Math.floor(s.scan_interval/60)}min │ Auto 30s</div>`;
document.getElementById('ct').innerHTML=h;
if(s.actions.some(a=>a.critical)){try{new Audio('data:audio/wav;base64,UklGRl9vAABXQVZFZm10IBAAAAABAAEAQB8AAEAfAAABAAgAZGF0YQ==').play()}catch(e){}}}

async function cf(i){const b=event.target;b.disabled=true;b.textContent='⏳...';
try{const r=await fetch('/api/confirm',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({action_idx:i})});
const d=await r.json();alert(d.msg);ld()}catch(e){alert('Error: '+e.message)}b.disabled=false}
async function fs(){try{await fetch('/api/force_scan',{method:'POST'});document.getElementById('ct').innerHTML='<div class="ld"><div class="sp"></div><br>Escaneando...</div>';setTimeout(ld,5000)}catch(e){}}
ld();setInterval(ld,30000);
</script>
</body></html>"""


# ═══════════════════════════════════════════════════════════════
#  STARTUP
# ═══════════════════════════════════════════════════════════════
load_state()
STATE["total_capital"] = CAPITAL
STATE["scan_interval"] = SCAN_MIN * 60
STATE["min_volume"] = MIN_VOL
STATE["safe_pct"] = SAFE_PCT
STATE["aggr_pct"] = AGGR_PCT

log.info(f"Bot iniciado: ${CAPITAL:,.0f} │ Scan cada {SCAN_MIN}min │ Data: {DATA_DIR}")

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False)
