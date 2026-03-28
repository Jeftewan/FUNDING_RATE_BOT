"""Flask API routes — v10.0 unified."""
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

    # DB persistence helper (lazy init)
    _db_persist = None
    def get_db_persist():
        nonlocal _db_persist
        if _db_persist is None and db_enabled:
            from core.db_persistence import DBPersistence
            _db_persist = DBPersistence()
        return _db_persist

    def get_current_user_id():
        """Get logged-in user ID, or None."""
        if not db_enabled:
            return None
        try:
            from flask_login import current_user
            if current_user.is_authenticated:
                return current_user.id
        except Exception:
            pass
        return None

    # Auth decorator: only enforced if DB/auth is enabled
    def auth_required(f):
        @wraps(f)
        def decorated(*args, **kwargs):
            if db_enabled:
                from flask_login import current_user
                if not current_user.is_authenticated:
                    if flask_req.path.startswith("/api/"):
                        return jsonify({"ok": False, "msg": "No autenticado"}), 401
                    return redirect("/auth/page")
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
            "status": s["status"], "version": "10.0",
        })

    # ── Config ─────────────────────────────────────────────────
    @app.route("/api/config", methods=["GET", "POST"])
    @auth_required
    def api_config():
        uid = get_current_user_id()
        if flask_req.method == "GET":
            if uid and get_db_persist():
                us = get_db_persist().load_user_state(uid)
                return jsonify({
                    "total_capital": us.get("total_capital", 1000),
                    "scan_minutes": us.get("scan_interval", 300) // 60,
                    "min_volume": us.get("min_volume", 1000000),
                    "min_apr": us.get("min_apr", 10),
                    "min_score": us.get("min_score", 40),
                    "min_stability_days": us.get("min_stability_days", 3),
                    "max_positions": us.get("max_positions", 5),
                    "alert_minutes_before": us.get("alert_minutes_before", 5),
                    "email_enabled": us.get("email_enabled", False),
                    "wa_phone": us.get("wa_phone", ""),
                    "wa_apikey": us.get("wa_apikey", ""),
                })
            # Fallback defaults if no DB
            return jsonify({
                "total_capital": 1000, "scan_minutes": 5,
                "min_volume": 1000000, "min_apr": 10, "min_score": 40,
                "min_stability_days": 3, "max_positions": 5,
                "alert_minutes_before": 5, "email_enabled": False,
                "wa_phone": "", "wa_apikey": "",
            })

        # POST — save config to DB
        data = flask_req.json or {}
        if uid and get_db_persist():
            db_data = {}
            if "total_capital" in data:
                db_data["total_capital"] = float(data["total_capital"])
            if "scan_minutes" in data:
                db_data["scan_interval"] = max(1, int(data["scan_minutes"])) * 60
            if "min_volume" in data:
                db_data["min_volume"] = float(data["min_volume"])
            if "min_apr" in data:
                db_data["min_apr"] = float(data["min_apr"])
            if "min_score" in data:
                db_data["min_score"] = int(data["min_score"])
            if "min_stability_days" in data:
                db_data["min_stability_days"] = int(data["min_stability_days"])
            if "max_positions" in data:
                db_data["max_positions"] = int(data["max_positions"])
            if "alert_minutes_before" in data:
                db_data["alert_minutes_before"] = int(data["alert_minutes_before"])
            if "email_enabled" in data:
                db_data["email_enabled"] = bool(data["email_enabled"])
            if "wa_phone" in data:
                db_data["wa_phone"] = str(data["wa_phone"]).strip()
            if "wa_apikey" in data:
                db_data["wa_apikey"] = str(data["wa_apikey"]).strip()

            get_db_persist().save_user_config(uid, db_data)

            # Sync WhatsApp settings to notifier state for current session
            with state_manager.lock:
                s = state_manager.state
                for k in ("email_enabled", "wa_phone", "wa_apikey"):
                    if k in db_data:
                        s[k] = db_data[k]
                    elif k == "wa_apikey" and "wa_apikey" in data:
                        s[k] = str(data["wa_apikey"]).strip()
            if scanner_worker.email_notifier:
                scanner_worker.email_notifier._sync_from_state()

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

        uid = get_current_user_id()

        with state_manager.lock:
            s = state_manager.state
            opps = s.get("opportunities", [])
            defi_opps = s.get("defi_opportunities", [])
            opp = next((o for o in opps if o.get("_id") == opp_id), None)
            if not opp:
                opp = next((o for o in defi_opps if o.get("_id") == opp_id), None)
            if not opp:
                return jsonify({"ok": False, "msg": "Oportunidad no encontrada"})

            # Load user state from DB for capital checks
            if uid and get_db_persist():
                user_state = get_db_persist().load_user_state(uid)
                merged = {
                    "total_capital": user_state.get("total_capital", 1000),
                    "max_positions": user_state.get("max_positions", 5),
                    "positions": user_state.get("positions", []),
                    "history": user_state.get("history", []),
                    "total_earned": user_state.get("total_earned", 0),
                }
            else:
                merged = s

            ok, result = open_position(merged, opp, capital)
            if ok:
                # Save to DB
                if uid and get_db_persist():
                    pos_dict = result["position"]
                    db_id = get_db_persist().save_position(uid, pos_dict)
                    result["position"]["db_id"] = db_id
                    log.info(f"Position saved to DB: id={db_id}, user={uid}")
                return jsonify({"ok": True, **result})
            else:
                return jsonify({"ok": False, "msg": result})

    # ── Positions ──────────────────────────────────────────────
    @app.route("/api/positions")
    @auth_required
    def api_positions():
        """Active positions with real-time data."""
        uid = get_current_user_id()
        with state_manager.lock:
            s = state_manager.state
            all_data = s.get("all_data", [])

            if uid and get_db_persist():
                user_state = get_db_persist().load_user_state(uid)
                positions_list = user_state.get("positions", [])
                merged = {
                    "positions": positions_list,
                    "total_capital": user_state.get("total_capital", 1000),
                    "max_positions": user_state.get("max_positions", 5),
                }
                summary = get_capital_summary(merged)
            else:
                positions_list = []
                summary = {"total": 1000, "used": 0, "available": 1000, "count": 0, "max_positions": 5}

            pdata = []
            now = time.time()

            for pos in positions_list:
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

            # Calculate total_earned from active positions
            total_earned = sum(p.get("earned_real", 0) for p in positions_list)

            return jsonify({
                "positions": pdata,
                "summary": summary,
                "total_earned": total_earned,
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

        uid = get_current_user_id()

        # Close in DB
        if uid and get_db_persist():
            try:
                db_pos_id = int(pos_id)
            except (ValueError, TypeError):
                return jsonify({"ok": False, "msg": "Posicion no encontrada"})

            from core.db_models import UserPosition
            pos = UserPosition.query.filter_by(id=db_pos_id, user_id=uid, status="active").first()
            if not pos:
                return jsonify({"ok": False, "msg": "Posicion no encontrada"})

            ih = pos.ih or 8
            el_h = (time.time() - pos.entry_time / 1000) / 3600
            earned = pos.earned_real or 0
            fees = (pos.entry_fees or 0) * 2
            net_earned = earned - fees

            result_data = {
                "reason": reason,
                "hours": el_h,
                "fees": fees,
                "net_earned": net_earned,
            }
            get_db_persist().close_position(db_pos_id, result_data)

            result = {
                "symbol": pos.symbol,
                "earned": earned,
                "fees": fees,
                "net_earned": net_earned,
                "hours": el_h,
                "payments": pos.payment_count or 0,
            }
        else:
            return jsonify({"ok": False, "msg": "DB no disponible"})

        # Clear any notified alerts for this symbol
        closed_sym = result["symbol"]
        stale_keys = {k for k in scanner_worker._notified_alerts if closed_sym in k}
        scanner_worker._notified_alerts -= stale_keys

        # Send WhatsApp notification
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
        uid = get_current_user_id()
        if uid and get_db_persist():
            user_state = get_db_persist().load_user_state(uid)
            return jsonify({
                "history": user_state.get("history", []),
                "total_earned": user_state.get("total_earned", 0),
            })
        return jsonify({"history": [], "total_earned": 0})

    @app.route("/api/clear_history", methods=["POST"])
    @auth_required
    def api_clear_history():
        """Clear all history and optionally reset positions."""
        data = flask_req.json or {}
        reset_all = data.get("reset_all", False)
        uid = get_current_user_id()

        if uid and get_db_persist():
            from core.database import db as _db
            from core.db_models import UserHistory, UserPosition

            UserHistory.query.filter_by(user_id=uid).delete()
            if reset_all:
                UserPosition.query.filter_by(user_id=uid, status="active").update(
                    {"status": "closed", "close_reason": "reset"})
            _db.session.commit()

        what = "todo (historial + posiciones)" if reset_all else "historial"
        log.info(f"Cleared: {what} for user {uid}")
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
        uid = get_current_user_id()

        with state_manager.lock:
            s = state_manager.state
            all_data = s.get("all_data", [])
            defi_data = s.get("defi_data", [])
            combined = all_data + defi_data
            stored_alerts = s.get("alerts", [])

        # Load positions from DB
        positions = []
        if uid and get_db_persist():
            user_state = get_db_persist().load_user_state(uid)
            positions = user_state.get("positions", [])

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

    # ── Account & Exchange Keys (SaaS mode) ─────────────────
    if db_enabled:
        @app.route("/api/account")
        @auth_required
        def api_account():
            from flask_login import current_user
            from core.db_models import UserExchangeKey
            keys = UserExchangeKey.query.filter_by(user_id=current_user.id).all()
            return jsonify({
                "ok": True,
                "user": {
                    "email": current_user.email,
                    "is_admin": current_user.is_admin,
                    "created_at": current_user.created_at.isoformat() if current_user.created_at else None,
                },
                "exchange_keys": [
                    {"exchange": k.exchange_name, "has_key": bool(k.api_key_encrypted)}
                    for k in keys
                ],
            })

        @app.route("/api/account/exchange_keys", methods=["POST"])
        @auth_required
        def api_save_exchange_keys():
            from flask_login import current_user
            from core.database import db as _db
            from core.db_models import UserExchangeKey
            from core.encryption import encrypt_value

            data = flask_req.get_json() or {}
            exchange = data.get("exchange", "").strip()
            api_key = data.get("api_key", "").strip()
            api_secret = data.get("api_secret", "").strip()
            passphrase = data.get("passphrase", "").strip()

            if not exchange:
                return jsonify({"ok": False, "msg": "Exchange requerido"}), 400

            existing = UserExchangeKey.query.filter_by(
                user_id=current_user.id, exchange_name=exchange).first()

            if not api_key and not api_secret:
                # Delete keys
                if existing:
                    _db.session.delete(existing)
                    _db.session.commit()
                return jsonify({"ok": True, "msg": f"Keys de {exchange} eliminadas"})

            if not existing:
                existing = UserExchangeKey(user_id=current_user.id, exchange_name=exchange)
                _db.session.add(existing)

            existing.api_key_encrypted = encrypt_value(api_key) if api_key else ""
            existing.api_secret_encrypted = encrypt_value(api_secret) if api_secret else ""
            existing.passphrase_encrypted = encrypt_value(passphrase) if passphrase else ""
            _db.session.commit()

            return jsonify({"ok": True, "msg": f"Keys de {exchange} guardadas"})

        @app.route("/api/account", methods=["DELETE"])
        @auth_required
        def api_delete_account():
            from flask_login import current_user, logout_user
            from core.database import db as _db
            _db.session.delete(current_user)
            _db.session.commit()
            logout_user()
            return jsonify({"ok": True, "msg": "Cuenta eliminada"})

    # ── Index ──────────────────────────────────────────────────
    @app.route("/")
    @auth_required
    def index():
        user_email = ""
        if db_enabled:
            from flask_login import current_user
            user_email = current_user.email if current_user.is_authenticated else ""
        return render_template("index.html", db_enabled=db_enabled, user_email=user_email)
