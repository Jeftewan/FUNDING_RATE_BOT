"""Flask API routes — v8.0 unified."""
import time
import threading
import logging
from flask import Blueprint, jsonify, request as flask_req, render_template
from portfolio.manager import get_capital_summary, open_position, close_position
from portfolio.actions import calculate_position_estimate

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
            "status": s["status"], "version": "8.0",
        })

    # ── Config ─────────────────────────────────────────────────
    @app.route("/api/config", methods=["GET", "POST"])
    def api_config():
        if flask_req.method == "GET":
            with state_manager.lock:
                s = state_manager.state
                return jsonify({
                    "total_capital": s["total_capital"],
                    "scan_minutes": s["scan_interval"] // 60,
                    "min_volume": s["min_volume"],
                    "min_apr": s.get("min_apr", 10),
                    "min_score": s.get("min_score", 40),
                    "min_stability_days": s.get("min_stability_days", 3),
                    "max_positions": s.get("max_positions", 5),
                    "alert_minutes_before": s.get("alert_minutes_before", 5),
                    "email_enabled": s.get("email_enabled", False),
                    "notify_method": s.get("notify_method", "whatsapp"),
                    "smtp_host": s.get("smtp_host", "smtp.gmail.com"),
                    "smtp_port": s.get("smtp_port", 587),
                    "smtp_user": s.get("smtp_user", ""),
                    "smtp_password": "***" if s.get("smtp_password") else "",
                    "email_to": s.get("email_to", ""),
                    "wa_phone": s.get("wa_phone", ""),
                    "wa_apikey": s.get("wa_apikey", ""),
                })
        # POST — update config
        data = flask_req.json or {}
        with state_manager.lock:
            s = state_manager.state
            if "total_capital" in data:
                s["total_capital"] = float(data["total_capital"])
            if "scan_minutes" in data:
                s["scan_interval"] = max(1, int(data["scan_minutes"])) * 60
            if "min_volume" in data:
                s["min_volume"] = float(data["min_volume"])
            if "min_apr" in data:
                s["min_apr"] = float(data["min_apr"])
            if "min_score" in data:
                s["min_score"] = int(data["min_score"])
            if "min_stability_days" in data:
                s["min_stability_days"] = int(data["min_stability_days"])
            if "max_positions" in data:
                s["max_positions"] = int(data["max_positions"])
            if "alert_minutes_before" in data:
                s["alert_minutes_before"] = int(data["alert_minutes_before"])
            # Notification settings
            if "email_enabled" in data:
                s["email_enabled"] = bool(data["email_enabled"])
            if "notify_method" in data:
                s["notify_method"] = str(data["notify_method"])
            if "smtp_host" in data:
                s["smtp_host"] = str(data["smtp_host"])
            if "smtp_port" in data:
                s["smtp_port"] = int(data["smtp_port"])
            if "smtp_user" in data:
                s["smtp_user"] = str(data["smtp_user"])
            if "smtp_password" in data and data["smtp_password"] != "***":
                s["smtp_password"] = str(data["smtp_password"])
            if "email_to" in data:
                s["email_to"] = str(data["email_to"])
            if "wa_phone" in data:
                s["wa_phone"] = str(data["wa_phone"]).strip()
            if "wa_apikey" in data:
                s["wa_apikey"] = str(data["wa_apikey"]).strip()

            # Sync notifier
            if scanner_worker.email_notifier:
                scanner_worker.email_notifier._sync_from_state()

            state_manager.save()
            return jsonify({"ok": True, "msg": "Configuracion guardada"})

    # ── Opportunities ──────────────────────────────────────────
    @app.route("/api/opportunities")
    def api_opportunities():
        """Unified opportunity list sorted by score."""
        with state_manager.lock:
            s = state_manager.state
            min_apr = s.get("min_apr", 10)
            min_score = s.get("min_score", 40)

            opps = s.get("opportunities", [])
            filtered = [
                o for o in opps
                if o.get("apr", 0) >= min_apr and o.get("score", 0) >= min_score
            ]

            return jsonify({
                "opportunities": filtered,
                "total_unfiltered": len(opps),
                "coinglass": s.get("coinglass_data", []),
                "last_scan": s.get("last_scan_time", "—"),
                "scan_count": s.get("scan_count", 0),
            })

    # ── Calculate (preview before opening) ─────────────────────
    @app.route("/api/calculate", methods=["POST"])
    def api_calculate():
        """Calculate estimated returns for an opportunity with given capital."""
        data = flask_req.json or {}
        opp_id = data.get("opportunity_id", "")
        capital = float(data.get("capital", 0))

        if capital <= 0:
            return jsonify({"ok": False, "msg": "Capital debe ser mayor a 0"})

        with state_manager.lock:
            opps = state_manager.get("opportunities", [])
            opp = next((o for o in opps if o.get("_id") == opp_id), None)
            if not opp:
                return jsonify({"ok": False, "msg": "Oportunidad no encontrada"})

            estimate = calculate_position_estimate(opp, capital)
            return jsonify({"ok": True, "estimate": estimate})

    # ── Open Position ──────────────────────────────────────────
    @app.route("/api/open_position", methods=["POST"])
    def api_open_position():
        """Open a new position from an opportunity."""
        data = flask_req.json or {}
        opp_id = data.get("opportunity_id", "")
        capital = float(data.get("capital", 0))

        with state_manager.lock:
            s = state_manager.state
            opps = s.get("opportunities", [])
            opp = next((o for o in opps if o.get("_id") == opp_id), None)
            if not opp:
                return jsonify({"ok": False, "msg": "Oportunidad no encontrada"})

            ok, result = open_position(s, opp, capital)
            if ok:
                state_manager.save()
                return jsonify({"ok": True, **result})
            else:
                return jsonify({"ok": False, "msg": result})

    # ── Positions ──────────────────────────────────────────────
    @app.route("/api/positions")
    def api_positions():
        """Active positions with real-time data."""
        with state_manager.lock:
            s = state_manager.state
            all_data = s.get("all_data", [])
            summary = get_capital_summary(s)
            pdata = []

            for pos in s["positions"]:
                cur = next(
                    (d for d in all_data
                     if d["symbol"] == pos["symbol"] and d["exchange"] == pos["exchange"]),
                    None,
                )
                cfr = cur["fr"] if cur else pos["entry_fr"]
                cp = cur.get("price", pos.get("entry_price", 0)) if cur else pos.get("entry_price", 0)
                mins_next = cur.get("mins_next", -1) if cur else -1

                ih = pos.get("ih", 8)
                el_h = (time.time() - pos["entry_time"] / 1000) / 3600

                earned = pos.get("earned_real", 0)
                entry_fees = pos.get("entry_fees", 0)
                est_fees = entry_fees * 2  # entry + exit
                net_earned = earned - est_fees

                fr_reversed = ((pos["entry_fr"] > 0 and cfr < 0) or
                               (pos["entry_fr"] < 0 and cfr > 0))

                ipd = 24 / ih
                fut_size = pos["capital_used"] / 2
                daily = fut_size * abs(cfr) * ipd
                current_apr = (daily * 365 / pos["capital_used"] * 100) if pos["capital_used"] > 0 else 0

                pdata.append({
                    **pos,
                    "current_fr": cfr,
                    "current_price": cp,
                    "elapsed_h": el_h,
                    "intervals": int(el_h / ih),
                    "est_earned": earned,
                    "est_fees_total": est_fees,
                    "net_earned": net_earned,
                    "current_apr": current_apr,
                    "fr_reversed": fr_reversed,
                    "mins_next": mins_next,
                })

            return jsonify({
                "positions": pdata,
                "summary": summary,
                "total_earned": s.get("total_earned", 0),
                "alerts": s.get("alerts", []),
            })

    # ── Close Position ─────────────────────────────────────────
    @app.route("/api/close_position", methods=["POST"])
    def api_close_position():
        """Close a position manually."""
        data = flask_req.json or {}
        pos_id = data.get("position_id", "")
        reason = data.get("reason", "manual")

        with state_manager.lock:
            s = state_manager.state
            ok, result = close_position(s, pos_id, reason)
            state_manager.save()
            if ok:
                # Send close summary email
                if scanner_worker.email_notifier and s.get("email_enabled"):
                    try:
                        scanner_worker.email_notifier.send_alert({
                            "type": "POSITION_CLOSED",
                            "severity": "INFO",
                            "symbol": result["symbol"],
                            "exchange": "",
                            "message": (
                                f"Posicion cerrada ({reason}). "
                                f"Ganancia: ${result['earned']:.2f} | "
                                f"Fees: ${result['fees']:.2f} | "
                                f"Neto: ${result['net_earned']:.2f} | "
                                f"Duracion: {result['hours']:.1f}h | "
                                f"Pagos: {result['payments']}"
                            ),
                        })
                    except Exception:
                        pass
                return jsonify({"ok": True, "result": result})
            else:
                return jsonify({"ok": False, "msg": result})

    # ── History ────────────────────────────────────────────────
    @app.route("/api/history")
    def api_history():
        with state_manager.lock:
            s = state_manager.state
            return jsonify({
                "history": s.get("history", []),
                "total_earned": s.get("total_earned", 0),
            })

    @app.route("/api/clear_history", methods=["POST"])
    def api_clear_history():
        """Clear all history and optionally reset positions."""
        data = flask_req.json or {}
        reset_all = data.get("reset_all", False)

        with state_manager.lock:
            s = state_manager.state
            s["history"] = []
            s["total_earned"] = 0
            if reset_all:
                s["positions"] = []
                s["alerts"] = []
            state_manager.save()

        what = "todo (historial + posiciones)" if reset_all else "historial"
        log.info(f"Cleared: {what}")
        return jsonify({"ok": True, "msg": f"{what} borrado"})

    # ── Force Scan ─────────────────────────────────────────────
    @app.route("/api/force_scan", methods=["POST"])
    def api_force():
        threading.Thread(target=scanner_worker._run_scan, daemon=True).start()
        return jsonify({"ok": True})

    # ── Test Notification ────────────────────────────────────
    @app.route("/api/test_email", methods=["POST"])
    def api_test_notification():
        """Send a test notification (WhatsApp or Email)."""
        n = scanner_worker.email_notifier
        if not n:
            return jsonify({"ok": False, "msg": "Notifier no disponible"})

        # Sync latest config and force enabled for test
        n._sync_from_state()
        method = n.notify_method
        prev_enabled = n.enabled
        n.enabled = True

        # Test connection first
        conn = n.test_connection()
        if not conn["ok"]:
            n.enabled = prev_enabled
            return jsonify({"ok": False, "msg": conn["error"]})

        # Send test alert
        test_alert = {
            "type": "TEST",
            "severity": "INFO",
            "symbol": "TEST",
            "exchange": "Test",
            "message": "Prueba de notificacion del Funding Bot v8.0",
        }
        sent = n.send_alert(test_alert)
        n.enabled = prev_enabled

        if sent:
            dest = n.wa_phone if method == "whatsapp" else n.email_to
            return jsonify({"ok": True, "msg": f"Enviado via {method} a {dest}"})
        return jsonify({"ok": False, "msg": "No enviado (posible cooldown de 5min)"})

    # ── Alerts ─────────────────────────────────────────────────
    @app.route("/api/alerts")
    def api_alerts():
        with state_manager.lock:
            return jsonify({"alerts": state_manager.get("alerts", [])})

    # ── Exchanges Status ───────────────────────────────────────
    @app.route("/api/exchanges/status")
    def api_exchanges_status():
        status = scanner_worker.exchange_manager.get_exchange_status()
        return jsonify({"exchanges": status})

    # ── Index ──────────────────────────────────────────────────
    @app.route("/")
    def index():
        return render_template("index.html")
