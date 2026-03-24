"""Flask API routes — v9.0 unified."""
import time
import threading
import logging
from functools import wraps
from flask import Blueprint, jsonify, request as flask_req, render_template, redirect
from portfolio.manager import get_capital_summary, open_position, close_position
from portfolio.actions import calculate_position_estimate

log = logging.getLogger("bot")

api = Blueprint("api", __name__)


def init_routes(app, state_manager, scanner_worker, config, defi_manager=None, db_enabled=False):
    """Register all routes on the Flask app."""

    # Auth decorator: only enforced if DB/auth is enabled
    def auth_required(f):
        @wraps(f)
        def decorated(*args, **kwargs):
            if db_enabled:
                from flask_login import current_user
                if not current_user.is_authenticated:
                    if flask_req.path.startswith("/api/"):
                        return jsonify({"ok": False, "msg": "No autenticado"}), 401
                    return redirect("/auth/login")
            return f(*args, **kwargs)
        return decorated

    @app.before_request
    def _before():
        scanner_worker.start()

    @app.route("/health")
    def health():
        s = state_manager.state
        return jsonify({
            "ok": True, "scans": s["scan_count"],
            "status": s["status"], "version": "9.0",
        })

    # ── Config ─────────────────────────────────────────────────
    @app.route("/api/config", methods=["GET", "POST"])
    @auth_required
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
            # WhatsApp notification settings
            if "email_enabled" in data:
                s["email_enabled"] = bool(data["email_enabled"])
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
    @auth_required
    def api_opportunities():
        """Unified opportunity list sorted by score."""
        with state_manager.lock:
            s = state_manager.state
            min_apr = s.get("min_apr", 10)
            min_score = s.get("min_score", 40)
            now = time.time()

            opps = s.get("opportunities", [])
            filtered = []
            for o in opps:
                if o.get("apr", 0) >= min_apr and o.get("score", 0) >= min_score:
                    # Recalculate mins_to_next live
                    nts = o.get("next_funding_ts", 0)
                    if nts and nts > 0:
                        o["mins_to_next"] = max(0, (nts / 1000 - now) / 60)
                    filtered.append(o)

            return jsonify({
                "opportunities": filtered,
                "total_unfiltered": len(opps),
                "coinglass": s.get("coinglass_data", []),
                "last_scan": s.get("last_scan_time", "—"),
                "scan_count": s.get("scan_count", 0),
                "scanning": s.get("scanning", False),
            })

    # ── DeFi Opportunities ──────────────────────────────────────
    @app.route("/api/defi_opportunities")
    @auth_required
    def api_defi_opportunities():
        """DeFi opportunity list sorted by score."""
        with state_manager.lock:
            s = state_manager.state
            now = time.time()

            opps = s.get("defi_opportunities", [])
            for o in opps:
                nts = o.get("next_funding_ts", 0)
                if nts and nts > 0:
                    o["mins_to_next"] = max(0, (nts / 1000 - now) / 60)

            return jsonify({
                "opportunities": opps,
                "total_unfiltered": len(opps),
                "last_scan": s.get("last_scan_time", "—"),
                "scan_count": s.get("scan_count", 0),
                "scanning": s.get("scanning", False),
            })

    # ── Calculate (preview before opening) ─────────────────────
    @app.route("/api/calculate", methods=["POST"])
    @auth_required
    def api_calculate():
        """Calculate estimated returns + SL/TP for an opportunity."""
        data = flask_req.json or {}
        opp_id = data.get("opportunity_id", "")
        capital = float(data.get("capital", 0))
        leverage = max(1, int(data.get("leverage", 1)))

        if capital <= 0:
            return jsonify({"ok": False, "msg": "Capital debe ser mayor a 0"})

        with state_manager.lock:
            opps = state_manager.get("opportunities", [])
            defi_opps = state_manager.get("defi_opportunities", [])
            opp = next((o for o in opps if o.get("_id") == opp_id), None)
            if not opp:
                opp = next((o for o in defi_opps if o.get("_id") == opp_id), None)
            if not opp:
                return jsonify({"ok": False, "msg": "Oportunidad no encontrada"})

            estimate = calculate_position_estimate(opp, capital, leverage)
            return jsonify({"ok": True, "estimate": estimate})

    # ── Open Position ──────────────────────────────────────────
    @app.route("/api/open_position", methods=["POST"])
    @auth_required
    def api_open_position():
        """Open a new position from an opportunity."""
        data = flask_req.json or {}
        opp_id = data.get("opportunity_id", "")
        capital = float(data.get("capital", 0))

        with state_manager.lock:
            s = state_manager.state
            opps = s.get("opportunities", [])
            defi_opps = s.get("defi_opportunities", [])
            opp = next((o for o in opps if o.get("_id") == opp_id), None)
            if not opp:
                opp = next((o for o in defi_opps if o.get("_id") == opp_id), None)
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
    @auth_required
    def api_positions():
        """Active positions with real-time data."""
        with state_manager.lock:
            s = state_manager.state
            all_data = s.get("all_data", [])
            summary = get_capital_summary(s)
            pdata = []
            now = time.time()

            for pos in s["positions"]:
                is_cross = pos.get("mode") == "cross_exchange"

                if is_cross:
                    # Cross-exchange: look up BOTH sides and compute differential
                    long_ex = pos.get("long_exchange", "")
                    short_ex = pos.get("short_exchange", pos.get("exchange", ""))
                    long_d = next(
                        (d for d in all_data
                         if d["symbol"] == pos["symbol"] and d["exchange"] == long_ex),
                        None,
                    )
                    short_d = next(
                        (d for d in all_data
                         if d["symbol"] == pos["symbol"] and d["exchange"] == short_ex),
                        None,
                    )
                    if long_d and short_d:
                        cfr = short_d["fr"] - long_d["fr"]
                    else:
                        cfr = pos["entry_fr"]
                    cp = short_d.get("price", pos.get("entry_price", 0)) if short_d else pos.get("entry_price", 0)

                    # mins_next: earliest of the two sides
                    mins_next = -1
                    candidates = [d for d in (long_d, short_d) if d]
                    for d in candidates:
                        nts = d.get("next_funding_ts", 0)
                        if nts and nts > 0:
                            mn = max(0, (nts / 1000 - now) / 60)
                        else:
                            mn = d.get("mins_next", -1)
                        if mn >= 0 and (mins_next < 0 or mn < mins_next):
                            mins_next = mn
                else:
                    # Spot-perp: single exchange lookup
                    cur = next(
                        (d for d in all_data
                         if d["symbol"] == pos["symbol"] and d["exchange"] == pos["exchange"]),
                        None,
                    )
                    cfr = cur["fr"] if cur else pos["entry_fr"]
                    cp = cur.get("price", pos.get("entry_price", 0)) if cur else pos.get("entry_price", 0)

                    mins_next = -1
                    if cur:
                        nts = cur.get("next_funding_ts", 0)
                        if nts and nts > 0:
                            mins_next = max(0, (nts / 1000 - now) / 60)
                        else:
                            mins_next = cur.get("mins_next", -1)

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
    @auth_required
    def api_close_position():
        """Close a position manually."""
        data = flask_req.json or {}
        pos_id = data.get("position_id", "")
        reason = data.get("reason", "manual")

        with state_manager.lock:
            s = state_manager.state
            ok, result = close_position(s, pos_id, reason)
            state_manager.save()

        if not ok:
            return jsonify({"ok": False, "msg": result})

        # Clear any notified alerts for this symbol (so new positions get fresh alerts)
        closed_sym = result["symbol"]
        stale_keys = {k for k in scanner_worker._notified_alerts if closed_sym in k}
        scanner_worker._notified_alerts -= stale_keys

        # Send WhatsApp notification OUTSIDE lock (HTTP call can take seconds)
        if scanner_worker.email_notifier:
            try:
                scanner_worker.email_notifier.send_alert({
                    "type": "POSITION_CLOSED",
                    "severity": "INFO",
                    "symbol": closed_sym,
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
            except Exception as e:
                log.warning(f"WhatsApp close notification failed: {e}")

        return jsonify({"ok": True, "result": result})

    # ── History ────────────────────────────────────────────────
    @app.route("/api/history")
    @auth_required
    def api_history():
        with state_manager.lock:
            s = state_manager.state
            return jsonify({
                "history": s.get("history", []),
                "total_earned": s.get("total_earned", 0),
            })

    @app.route("/api/clear_history", methods=["POST"])
    @auth_required
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

    # ── Test WhatsApp ─────────────────────────────────────────
    @app.route("/api/test_email", methods=["POST"])
    def api_test_whatsapp():
        """Send a test WhatsApp message via CallMeBot.

        This tests the FULL alert pipeline (send_alerts → send_alert)
        using a simulated RATE_REVERSAL alert, not just the raw HTTP call.
        This way we verify: enabled check, cooldown, formatting, and delivery.
        """
        n = scanner_worker.email_notifier
        if not n:
            return jsonify({"ok": False, "msg": "Notifier no disponible"})

        n._sync_from_state()
        if not all([n.wa_phone, n.wa_apikey]):
            return jsonify({"ok": False, "msg": "Configura telefono y API key primero"})

        # Clear cooldown for test alerts so they always send
        test_keys = [k for k in n._sent_cache if k.startswith("TEST_")]
        for k in test_keys:
            del n._sent_cache[k]

        # Simulate a real alert through the full pipeline
        test_alert = {
            "type": "TEST_ALERT",
            "severity": "CRITICAL",
            "symbol": "TEST",
            "exchange": "Bot",
            "message": "Prueba de alerta automatica — pipeline completo OK",
        }

        # Use send_alerts (the same function the monitor uses)
        sent = n.send_alerts([test_alert])
        if sent > 0:
            return jsonify({"ok": True, "msg": f"WhatsApp enviado a {n.wa_phone} (pipeline completo)"})

        # If send_alerts failed, diagnose why
        diag = []
        if not n.enabled:
            diag.append(f"Notificaciones deshabilitadas (email_enabled={n.enabled})")
        if not n.wa_phone:
            diag.append("Telefono vacio")
        if not n.wa_apikey:
            diag.append("API key vacia")

        if not diag:
            # send_alerts returned 0 but config looks OK — try raw send
            try:
                n._send_whatsapp("✅ Funding Bot — Prueba de WhatsApp OK")
                return jsonify({"ok": True, "msg": f"WhatsApp enviado (fallback directo) a {n.wa_phone}"})
            except Exception as e:
                diag.append(f"Error HTTP: {str(e)[:200]}")

        return jsonify({"ok": False, "msg": " | ".join(diag) if diag else "Error desconocido"})

    # ── Alerts ─────────────────────────────────────────────────
    @app.route("/api/alerts")
    def api_alerts():
        with state_manager.lock:
            return jsonify({"alerts": state_manager.get("alerts", [])})

    @app.route("/api/alert_diagnostics")
    def api_alert_diagnostics():
        """Diagnostic endpoint to check the full alert pipeline status."""
        n = scanner_worker.email_notifier
        with state_manager.lock:
            s = state_manager.state
            positions = s.get("positions", [])
            all_data = s.get("all_data", [])
            defi_data = s.get("defi_data", [])
            combined = all_data + defi_data
            stored_alerts = s.get("alerts", [])

        diag = {
            "whatsapp": {
                "notifier_exists": n is not None,
                "enabled": n.enabled if n else False,
                "phone_set": bool(n.wa_phone) if n else False,
                "apikey_set": bool(n.wa_apikey) if n else False,
                "email_enabled_in_state": s.get("email_enabled", False),
                "cooldown_cache": {k: f"{time.time() - v:.0f}s ago"
                                   for k, v in (n._sent_cache if n else {}).items()},
            },
            "data": {
                "all_data_count": len(all_data),
                "defi_data_count": len(defi_data),
                "combined_count": len(combined),
                "positions_count": len(positions),
            },
            "stored_alerts": stored_alerts,
            "positions_detail": [],
        }

        # Check each position for alert conditions
        for pos in positions:
            is_cross = pos.get("mode") == "cross_exchange"
            p_diag = {
                "symbol": pos["symbol"],
                "mode": pos.get("mode", "spot_perp"),
                "entry_fr": pos["entry_fr"],
            }

            if is_cross:
                long_ex = pos.get("long_exchange", "")
                short_ex = pos.get("short_exchange", "")
                long_d = next((d for d in combined
                               if d["symbol"] == pos["symbol"] and d["exchange"] == long_ex), None)
                short_d = next((d for d in combined
                                if d["symbol"] == pos["symbol"] and d["exchange"] == short_ex), None)
                p_diag["long_exchange"] = long_ex
                p_diag["short_exchange"] = short_ex
                p_diag["long_data_found"] = long_d is not None
                p_diag["short_data_found"] = short_d is not None
                if long_d and short_d:
                    cfr = short_d["fr"] - long_d["fr"]
                    p_diag["current_differential"] = cfr
                    p_diag["short_fr"] = short_d["fr"]
                    p_diag["long_fr"] = long_d["fr"]
                    p_diag["would_trigger_reversal"] = (
                        (pos["entry_fr"] > 0 and cfr < 0) or
                        (pos["entry_fr"] < 0 and cfr > 0)
                    )
                else:
                    p_diag["issue"] = "Missing data for one or both sides"
            else:
                cur = next((d for d in combined
                            if d["symbol"] == pos["symbol"] and d["exchange"] == pos["exchange"]), None)
                p_diag["data_found"] = cur is not None
                if cur:
                    p_diag["current_fr"] = cur["fr"]
                    p_diag["would_trigger_reversal"] = (
                        (pos["entry_fr"] > 0 and cur["fr"] < 0) or
                        (pos["entry_fr"] < 0 and cur["fr"] > 0)
                    )

            diag["positions_detail"].append(p_diag)

        return jsonify(diag)

    # ── Exchanges Status ───────────────────────────────────────
    @app.route("/api/exchanges/status")
    def api_exchanges_status():
        status = scanner_worker.exchange_manager.get_exchange_status()
        return jsonify({"exchanges": status})

    # ── Funding History (for mini-charts) ─────────────────────
    @app.route("/api/funding_history/<symbol>/<exchange>")
    def api_funding_history(symbol, exchange):
        try:
            history = scanner_worker.exchange_manager.fetch_funding_history(
                symbol, exchange, limit=30)
            return jsonify({
                "rates": history.rates,
                "timestamps": history.timestamps,
                "avg": history.avg,
            })
        except Exception as e:
            return jsonify({"rates": [], "timestamps": [], "avg": 0, "error": str(e)})

    # ── Index ──────────────────────────────────────────────────
    @app.route("/")
    @auth_required
    def index():
        user_email = ""
        if db_enabled:
            from flask_login import current_user
            user_email = current_user.email if current_user.is_authenticated else ""
        return render_template("index.html", db_enabled=db_enabled, user_email=user_email)
