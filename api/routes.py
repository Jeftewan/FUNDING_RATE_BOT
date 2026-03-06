"""Flask API routes."""
import time
import threading
import logging
from flask import Blueprint, jsonify, request as flask_req, Response, render_template
from analysis.fees import calculate_returns
from portfolio.manager import get_budget_breakdown, close_position
from portfolio.actions import generate_actions

log = logging.getLogger("bot")

api = Blueprint("api", __name__)


def init_routes(app, state_manager, scanner_worker, config):
    """Register all routes on the Flask app."""

    @app.before_request
    def _before():
        scanner_worker.start()

    @app.route("/health")
    def health():
        s = state_manager.state
        return jsonify({
            "ok": True, "scans": s["scan_count"],
            "status": s["status"], "version": "7.0",
        })

    @app.route("/api/state")
    def api_state():
        with state_manager.lock:
            s = state_manager.state
            bd = get_budget_breakdown(s)
            pdata = []
            for pos in s["positions"]:
                cur = next(
                    (d for d in s["all_data"]
                     if d["symbol"] == pos["symbol"] and d["exchange"] == pos["exchange"]),
                    None,
                )
                cfr = cur["fr"] if cur else pos["entry_fr"]
                cp = cur["price"] if cur else pos["entry_price"]
                pc = ((cp - pos["entry_price"]) / pos["entry_price"] * 100
                      if pos["entry_price"] > 0 else 0)
                ih = pos.get("ih", cur.get("ih", 8) if cur else 8)
                el_h = (time.time() - pos["entry_time"] / 1000) / 3600
                ivs = int(el_h / ih)
                c = calculate_returns({
                    "fr": cfr, "price": cp, "symbol": pos["symbol"],
                    "exchange": pos["exchange"], "vol24h": 0,
                    "ih": ih, "ipd": 24 / ih,
                }, pos["capital_used"])
                est = pos.get("earned_real", 0)
                pp = c["fut"] * (pc / 100) if pos["carry"] == "Reverse" else 0
                fr_rev = ((pos["entry_fr"] > 0 and cfr < 0) or
                          (pos["entry_fr"] < 0 and cfr > 0))
                sl_hit = False
                if pos["carry"] == "Reverse" and pos.get("sl_pct", 0) > 0 and pos["entry_price"] > 0:
                    price_drop = ((pos["entry_price"] - cp) / pos["entry_price"]) * 100
                    sl_hit = price_drop >= pos["sl_pct"]
                pdata.append({
                    **pos, "current_fr": cfr, "current_price": cp,
                    "price_change": pc, "elapsed_h": el_h, "intervals": ivs,
                    "est_earned": est, "price_pnl": pp,
                    "total_pnl": est + pp, "current_apr": c["apr"],
                    "fr_reversed": fr_rev,
                    "mins_next": cur.get("mins_next", -1) if cur else -1,
                    "sl_hit": sl_hit,
                })

            return jsonify({
                "capital": s["total_capital"],
                "earned": s.get("total_earned", 0),
                "breakdown": bd, "positions": pdata,
                "actions": s["actions"],
                "safe_top": [
                    {"token": o["token"], "hist": o["hist"], "score": o["score"],
                     "calc": calculate_returns(o["token"],
                        max(bd["sa"] / max(1, s["max_pos_safe"] - bd["sc"]), 50))}
                    for o in s["safe_top"]
                ],
                "aggr_top": [
                    {"token": o["token"], "hist": o["hist"], "score": o["score"],
                     "rsi": o.get("rsi", -1),
                     "calc": calculate_returns(o["token"],
                        max(bd["aa"] / max(1, s["max_pos_aggr"] - bd["ac"]), 50))}
                    for o in s["aggr_top"]
                ],
                "status": s["status"], "scan_count": s["scan_count"],
                "last_scan": s["last_scan_time"],
                "scan_interval": s["scan_interval"],
                "last_error": s.get("last_error", ""),
            })

    @app.route("/api/opportunities")
    def api_opportunities():
        """Coinglass-style arbitrage table."""
        with state_manager.lock:
            s = state_manager.state
            return jsonify({
                "spot_perp": s.get("spot_perp_opportunities", []),
                "cross_exchange": s.get("cross_exchange_opportunities", []),
                "coinglass": s.get("coinglass_data", []),
                "last_scan": s.get("last_scan_time", "—"),
                "scan_count": s.get("scan_count", 0),
            })

    @app.route("/api/rates")
    def api_rates():
        """Raw funding rates across all exchanges."""
        with state_manager.lock:
            return jsonify({"rates": state_manager.get("all_data", [])})

    @app.route("/api/alerts")
    def api_alerts():
        with state_manager.lock:
            email_status = "disabled"
            if hasattr(scanner_worker, 'email_notifier') and scanner_worker.email_notifier:
                email_status = "enabled" if scanner_worker.email_notifier.enabled else "disabled"
            return jsonify({
                "alerts": state_manager.get("alerts", []),
                "email_notifications": email_status,
            })

    @app.route("/api/alerts/test_email", methods=["POST"])
    def api_test_email():
        """Send a test alert email to verify SMTP configuration."""
        if not hasattr(scanner_worker, 'email_notifier') or not scanner_worker.email_notifier:
            return jsonify({"ok": False, "msg": "Email notifier not configured"})
        notifier = scanner_worker.email_notifier
        if not notifier.enabled:
            return jsonify({"ok": False, "msg": "Email alerts not enabled. Set ALERT_EMAIL_ENABLED=true"})

        # Test connection first
        conn_test = notifier.test_connection()
        if not conn_test["ok"]:
            return jsonify({"ok": False, "msg": f"SMTP connection failed: {conn_test['error']}"})

        # Send test alert
        test_alert = {
            "type": "TEST",
            "severity": "CRITICAL",
            "symbol": "BTC",
            "exchange": "Test",
            "message": "Este es un email de prueba del Funding Bot v7.0",
        }
        sent = notifier.send_alert(test_alert)
        if sent:
            return jsonify({"ok": True, "msg": f"✅ Email de prueba enviado a {notifier.email_to}"})
        return jsonify({"ok": False, "msg": "Email no enviado (posible cooldown o error)"})

    @app.route("/api/exchanges/status")
    def api_exchanges_status():
        status = scanner_worker.exchange_manager.get_exchange_status()
        return jsonify({"exchanges": status})

    @app.route("/api/confirm", methods=["POST"])
    def api_confirm():
        data = flask_req.json or {}
        if config.BOT_PASSWORD and data.get("password") != config.BOT_PASSWORD:
            return jsonify({"ok": False, "msg": "Contrasena incorrecta"})
        idx = data.get("action_idx", -1)
        with state_manager.lock:
            s = state_manager.state
            if idx < 0 or idx >= len(s["actions"]):
                return jsonify({"ok": False, "msg": "Accion invalida"})
            act = s["actions"][idx]
            if act["type"] == "OPEN":
                s["positions"].append({
                    "symbol": act["symbol"], "exchange": act["exchange"],
                    "entry_fr": act["fr"], "entry_price": act["price"],
                    "entry_time": int(time.time() * 1000),
                    "carry": act["carry_type"],
                    "capital_used": act["capital"],
                    "ih": act.get("ih", 8),
                    "sl_pct": act.get("sl_pct", 0),
                })
                state_manager.save()
                s["actions"] = generate_actions(s)
                return jsonify({"ok": True, "msg": f"✅ {act['symbol']} registrada"})
            elif act["type"] == "EXIT":
                ok, msg = close_position(s, act["idx"])
                state_manager.save()
                s["actions"] = generate_actions(s)
                return jsonify({"ok": ok, "msg": msg})
            elif act["type"] == "ROTATE":
                i = act["idx"]
                if i < len(s["positions"]):
                    ok, msg = close_position(s, i)
                    ns = act.get("new_sym")
                    ne = act.get("new_exch")
                    cur = next(
                        (d for d in s["all_data"]
                         if d["symbol"] == ns and d["exchange"] == ne),
                        None,
                    )
                    if cur:
                        nc = calculate_returns(cur, act.get("capital", 100))
                        s["positions"].append({
                            "symbol": ns, "exchange": ne,
                            "entry_fr": cur["fr"], "entry_price": cur["price"],
                            "entry_time": int(time.time() * 1000),
                            "carry": nc["carry"],
                            "capital_used": nc.get("fut", 50) * 2,
                            "ih": nc["ih"], "sl_pct": nc.get("sl_pct", 0),
                        })
                    state_manager.save()
                    s["actions"] = generate_actions(s)
                    return jsonify({"ok": True, "msg": f"✅ Rotacion → {ns}"})
        return jsonify({"ok": False, "msg": "Error"})

    @app.route("/api/manual_close", methods=["POST"])
    def api_manual_close():
        data = flask_req.json or {}
        if config.BOT_PASSWORD and data.get("password") != config.BOT_PASSWORD:
            return jsonify({"ok": False, "msg": "Contrasena incorrecta"})
        idx = data.get("position_idx", -1)
        with state_manager.lock:
            s = state_manager.state
            ok, msg = close_position(s, idx)
            state_manager.save()
            s["actions"] = generate_actions(s)
            return jsonify({"ok": ok, "msg": msg})

    @app.route("/api/skip", methods=["POST"])
    def api_skip():
        data = flask_req.json or {}
        if config.BOT_PASSWORD and data.get("password") != config.BOT_PASSWORD:
            return jsonify({"ok": False, "msg": "Contrasena incorrecta"})
        sym = data.get("symbol", "")
        exch = data.get("exchange", "")
        if not sym:
            return jsonify({"ok": False, "msg": "Token no especificado"})
        skip_key = f"{sym}_{exch}"
        with state_manager.lock:
            s = state_manager.state
            if "skipped_tokens" not in s:
                s["skipped_tokens"] = []
            if skip_key not in s["skipped_tokens"]:
                s["skipped_tokens"].append(skip_key)
            s["actions"] = generate_actions(s)
            state_manager.save()
            return jsonify({"ok": True, "msg": f"⏭ {sym} descartado"})

    @app.route("/api/clear_skips", methods=["POST"])
    def api_clear_skips():
        with state_manager.lock:
            s = state_manager.state
            s["skipped_tokens"] = []
            s["actions"] = generate_actions(s)
            state_manager.save()
            return jsonify({"ok": True, "msg": "✅ Descartados limpiados"})

    @app.route("/api/config", methods=["GET", "POST"])
    def api_config():
        if flask_req.method == "GET":
            with state_manager.lock:
                s = state_manager.state
                return jsonify({
                    "capital": s["total_capital"],
                    "scan_minutes": s["scan_interval"] // 60,
                    "safe_pct": s["safe_pct"], "aggr_pct": s["aggr_pct"],
                    "min_volume": s["min_volume"],
                    "min_apr_safe": s["min_apr_safe"],
                    "min_apr_aggr": s["min_apr_aggr"],
                    "min_score": s["min_score"],
                    "max_pos_safe": s["max_pos_safe"],
                    "max_pos_aggr": s["max_pos_aggr"],
                })
        data = flask_req.json or {}
        if config.BOT_PASSWORD and data.get("password") != config.BOT_PASSWORD:
            return jsonify({"ok": False, "msg": "Contrasena incorrecta"})
        with state_manager.lock:
            s = state_manager.state
            mapping = {
                "capital": "total_capital", "min_volume": "min_volume",
                "min_apr_safe": "min_apr_safe", "min_apr_aggr": "min_apr_aggr",
            }
            for k, sk in mapping.items():
                if k in data:
                    s[sk] = float(data[k])
            if "scan_minutes" in data:
                s["scan_interval"] = int(data["scan_minutes"]) * 60
            if "safe_pct" in data:
                s["safe_pct"] = float(data["safe_pct"])
                s["aggr_pct"] = 100 - s["safe_pct"]
            if "min_score" in data:
                s["min_score"] = int(data["min_score"])
            if "max_pos_safe" in data:
                s["max_pos_safe"] = int(data["max_pos_safe"])
            if "max_pos_aggr" in data:
                s["max_pos_aggr"] = int(data["max_pos_aggr"])
            s["actions"] = generate_actions(s)
            state_manager.save()
            return jsonify({"ok": True, "msg": "✅ Config guardada"})

    @app.route("/api/force_scan", methods=["POST"])
    def api_force():
        threading.Thread(target=scanner_worker._run_scan, daemon=True).start()
        return jsonify({"ok": True})

    @app.route("/")
    def index():
        return render_template("index.html")
