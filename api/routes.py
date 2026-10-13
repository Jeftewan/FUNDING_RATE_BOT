"""Flask API routes — v10.0 unified."""
import time
import threading
import logging
from datetime import datetime
from functools import wraps
from flask import Blueprint, jsonify, request as flask_req, redirect
from portfolio.manager import get_capital_summary, open_position, close_position
from portfolio.actions import calculate_position_estimate

log = logging.getLogger("bot")

api = Blueprint("api", __name__)


def init_routes(app, state_manager, scanner_worker, config, defi_manager=None, db_enabled=False,
                job_runner=None, account_syncer=None):
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

    def _exec_alert(uid, symbol, message, severity="CRITICAL"):
        """Push a critical auto-execution alert to the user's Telegram."""
        if not scanner_worker.email_notifier:
            return
        try:
            import time as _t
            aid = int(_t.time())
            scanner_worker._dispatch_alerts_per_user([{
                "type": "EXEC_FAILURE",
                "severity": severity,
                "symbol": symbol,
                "exchange": "",
                "user_id": uid,
                "funding_ts": aid,
                "dedup_key": f"EXEC_FAILURE_{symbol}_{uid}_{aid}",
                "message": message,
            }])
        except Exception as e:
            log.warning(f"exec alert dispatch failed: {e}")

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
        if job_runner:
            job_runner.start()
        if account_syncer:
            account_syncer.start()

    @app.route("/health")
    def health():
        s = state_manager.state
        # DB requerida pero no inicializada → 503 para que el healthcheck de
        # Railway no promueva un deploy sin login (caso SQLAlchemy 2.1/psycopg).
        db_ok = db_enabled or not config.USE_DB
        return jsonify({
            "ok": db_ok, "scans": s["scan_count"],
            "status": s["status"], "version": "10.0",
            "db": "connected" if db_enabled else "not configured",
        }), (200 if db_ok else 503)

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
                    "min_volume": us.get("min_volume", 1000000),
                    "min_apr": us.get("min_apr", 10),
                    "min_score": us.get("min_score", 40),
                    "min_stability_days": us.get("min_stability_days", 3),
                    "max_positions": us.get("max_positions", 5),
                    "alert_minutes_before": us.get("alert_minutes_before", 5),
                    "email_enabled": us.get("email_enabled", False),
                    "tg_chat_id": us.get("tg_chat_id", ""),
                    "tg_bot_token": us.get("tg_bot_token", ""),
                    "allowed_exchanges": us.get("allowed_exchanges", ""),
                    "capital_source": us.get("capital_source", "manual"),
                    "real_balances": us.get("real_balances"),
                    "auto_transfer": us.get("auto_transfer", False),
                })
            # Fallback defaults if no DB
            return jsonify({
                "total_capital": 1000,
                "min_volume": 1000000, "min_apr": 10, "min_score": 40,
                "min_stability_days": 3, "max_positions": 5,
                "alert_minutes_before": 5, "email_enabled": False,
                "tg_chat_id": "", "tg_bot_token": "",
                "allowed_exchanges": "",
            })

        # POST — save config to DB
        data = flask_req.json or {}
        if uid and get_db_persist():
            db_data = {}
            # Con cuenta conectada el capital es REAL (sync); el manual se ignora.
            if "total_capital" in data and get_db_persist().real_free_usdt(uid) is None:
                db_data["total_capital"] = float(data["total_capital"])
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
            if "tg_chat_id" in data:
                db_data["tg_chat_id"] = str(data["tg_chat_id"]).strip()
            if "tg_bot_token" in data:
                db_data["tg_bot_token"] = str(data["tg_bot_token"]).strip()
            if "allowed_exchanges" in data:
                db_data["allowed_exchanges"] = str(data["allowed_exchanges"]).strip()

            get_db_persist().save_user_config(uid, db_data)

            # Sync Telegram settings to notifier state for current session
            with state_manager.lock:
                s = state_manager.state
                for k in ("email_enabled", "tg_chat_id", "tg_bot_token"):
                    if k in db_data:
                        s[k] = db_data[k]
                    elif k == "tg_bot_token" and "tg_bot_token" in data:
                        s[k] = str(data["tg_bot_token"]).strip()
            if scanner_worker.email_notifier:
                scanner_worker.email_notifier._sync_from_state()

        return jsonify({"ok": True, "msg": "Configuracion guardada"})

    # ── Opportunities ──────────────────────────────────────────
    @app.route("/api/opportunities")
    @auth_required
    def api_opportunities():
        """Unified opportunity list sorted by score.

        Display filters (APR / score / stability days / volume / exchange) are
        applied client-side from the unified Filtros panel; this endpoint
        returns the full list so the panel is the single source of truth.
        """
        with state_manager.lock:
            s = state_manager.state
            now = time.time()

            opps = s.get("opportunities", [])
            filtered = []
            for o in opps:
                # Recalculate mins_to_next live
                nts = o.get("next_funding_ts", 0)
                if nts and nts > 0:
                    o["mins_to_next"] = max(0, (nts / 1000 - now) / 60)
                filtered.append(o)

            # Enrich with score trends if DB is available
            if db_enabled and filtered:
                try:
                    pairs = [
                        (o.get("symbol", ""),
                         o.get("exchange", o.get("short_exchange", "")))
                        for o in filtered
                    ]
                    trends = db_persistence.get_score_trends_batch(pairs)
                    for o in filtered:
                        key = f"{o.get('symbol', '')}_{o.get('exchange', o.get('short_exchange', ''))}"
                        trend_data = trends.get(key)
                        if trend_data:
                            o["score_trend"] = trend_data["trend"]
                            o["score_avg"] = trend_data["avg_score"]
                            o["score_delta"] = trend_data["delta"]
                except Exception:
                    pass  # Non-critical: don't break opportunity listing

            return jsonify({
                "opportunities": filtered,
                "total_unfiltered": len(opps),
                "coinglass": s.get("coinglass_data", []),
                "last_scan": s.get("last_scan", 0),
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
                "last_scan": s.get("last_scan", 0),
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
        leverage = max(1, int(data.get("leverage", 1)))

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

            ok, result = open_position(merged, opp, capital, leverage)
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
            # Merge CEX and DeFi rates so cross-exchange positions with a DeFi
            # leg can resolve both sides (current_fr = short - long, mins_next
            # = earliest of the two).
            all_data = s.get("all_data", []) + s.get("defi_data", [])

            if uid and get_db_persist():
                user_state = get_db_persist().load_user_state(uid)
                positions_list = user_state.get("positions", [])
                merged = {
                    "positions": positions_list,
                    "total_capital": user_state.get("total_capital", 1000),
                    "max_positions": user_state.get("max_positions", 5),
                }
                summary = get_capital_summary(merged)
                summary["capital_source"] = user_state.get("capital_source", "manual")
                summary["real_balances"] = user_state.get("real_balances")
                bsnap = get_db_persist().get_account_snapshot(uid, "binance")
                bdata = (bsnap or {}).get("data") or {}
                real_by_sym = {(p.get("symbol") or "").upper(): p for p in bdata.get("positions", [])
                               if p.get("side") == "short"}
                drift_by_pos = {}
                for d in bdata.get("drift", []):
                    drift_by_pos.setdefault(str(d.get("position_id")), []).append(d)
                summary["importable"] = len(bdata.get("importable", []))
                summary["synced_at"] = (bsnap or {}).get("synced_at")
            else:
                positions_list = []
                summary = {"total": 1000, "used": 0, "available": 1000, "count": 0, "max_positions": 5}
                real_by_sym, drift_by_pos = {}, {}

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
                from portfolio.manager import position_fees as _pf
                entry_fee_val, exit_fee_val, est_fees, fees_is_real = _pf(pos)
                net_earned = earned - est_fees

                fr_reversed = ((pos["entry_fr"] > 0 and cfr < 0) or
                               (pos["entry_fr"] < 0 and cfr > 0))

                ipd = 24 / ih
                exposure = pos.get("exposure", pos["capital_used"] / 2)
                daily = exposure * abs(cfr) * ipd
                current_apr = (daily * 365 / pos["capital_used"] * 100) if pos["capital_used"] > 0 else 0

                # Include switch analysis if available
                pos_id = str(pos.get("id", ""))
                switch_data = scanner_worker._switch_results.get(pos_id)

                pdata.append({
                    **pos,
                    "current_fr": cfr,
                    "current_price": cp,
                    "price_change_pct": (
                        (cp - pos["entry_price"]) / pos["entry_price"] * 100
                        if pos.get("entry_price") else 0
                    ),
                    "elapsed_h": el_h,
                    "intervals": int(el_h / ih),
                    "est_earned": earned,
                    "est_fees_total": est_fees,
                    "entry_fees_effective": entry_fee_val,
                    "exit_fees_effective": exit_fee_val,
                    "fees_is_real": fees_is_real,
                    "net_earned": net_earned,
                    "current_apr": current_apr,
                    "fr_reversed": fr_reversed,
                    "mins_next": mins_next,
                    "switch_analysis": switch_data,
                    "real": (real_by_sym.get((pos.get("symbol") or "").upper())
                             if (pos.get("exchange") or "").lower() == "binance" else None),
                    "drift": drift_by_pos.get(str(pos.get("id")), []),
                })

            # Calculate total_earned from active positions
            total_earned = sum(p.get("earned_real", 0) for p in positions_list)

            return jsonify({
                "positions": pdata,
                "summary": summary,
                "total_earned": total_earned,
                "alerts": s.get("alerts", []),
            })

    # ── Position AI Analysis ────────────────────────────────────
    @app.route("/api/positions/ai", methods=["POST"])
    @auth_required
    def api_positions_ai():
        """On-demand AI + switching analysis for active positions.
        Runs switch analysis first, then AI analysis — both triggered
        only when the user clicks the 'Analizar con IA' button.
        """
        from analysis.ai_analyzer import analyze_positions

        uid = get_current_user_id()
        with state_manager.lock:
            s = state_manager.state
            all_data = s.get("all_data", []) + s.get("defi_data", [])

            if uid and get_db_persist():
                user_state = get_db_persist().load_user_state(uid)
                positions_list = user_state.get("positions", [])
            else:
                positions_list = []

            if not positions_list:
                return jsonify({"ok": True, "analyses": {}, "switch_results": {}})

        # ── Run switch analysis on-demand ──────────────────────
        scanner_worker.run_switch_analysis(positions_list)

        # ── Build enriched position data ───────────────────────
        with state_manager.lock:
            s = state_manager.state
            all_data = s.get("all_data", []) + s.get("defi_data", [])

        pdata = []
        switch_results_out = {}
        now = time.time()
        for pos in positions_list:
            is_cross = pos.get("mode") == "cross_exchange"
            if is_cross:
                long_ex = pos.get("long_exchange", "")
                short_ex = pos.get("short_exchange", pos.get("exchange", ""))
                long_d = next((d for d in all_data if d["symbol"] == pos["symbol"] and d["exchange"] == long_ex), None)
                short_d = next((d for d in all_data if d["symbol"] == pos["symbol"] and d["exchange"] == short_ex), None)
                cfr = (short_d["fr"] - long_d["fr"]) if long_d and short_d else pos["entry_fr"]
            else:
                cur = next((d for d in all_data if d["symbol"] == pos["symbol"] and d["exchange"] == pos["exchange"]), None)
                cfr = cur["fr"] if cur else pos["entry_fr"]

            ih = pos.get("ih", 8)
            el_h = (now - pos["entry_time"] / 1000) / 3600
            earned = pos.get("earned_real", 0)
            from portfolio.manager import position_fees as _pf
            _e, _x, _fees_total, _ = _pf(pos)
            net_earned = earned - _fees_total
            exposure = pos.get("exposure", pos["capital_used"] / 2)
            ipd = 24 / ih
            daily = exposure * abs(cfr) * ipd
            current_apr = (daily * 365 / pos["capital_used"] * 100) if pos["capital_used"] > 0 else 0
            fr_reversed = ((pos["entry_fr"] > 0 and cfr < 0) or (pos["entry_fr"] < 0 and cfr > 0))

            pos_id_ai = str(pos.get("id", ""))
            switch_data_ai = scanner_worker._switch_results.get(pos_id_ai)
            if switch_data_ai:
                switch_results_out[pos_id_ai] = switch_data_ai

            pdata.append({
                **pos,
                "current_fr": cfr,
                "elapsed_h": el_h,
                "net_earned": net_earned,
                "current_apr": current_apr,
                "fr_reversed": fr_reversed,
                "switch_analysis": switch_data_ai,
            })

        analyses = analyze_positions(pdata, config)
        return jsonify({
            "ok": True,
            "analyses": analyses,
            "switch_results": switch_results_out,
        })

    # ── Close Position ─────────────────────────────────────────
    @app.route("/api/close_position", methods=["POST"])
    @auth_required
    def api_close_position():
        """Close a position manually.

        Body may include `exit_fees_real` (USD) to record the actual exit
        trading cost — this overrides the estimate in the PnL and is
        persisted on the position before closing.
        """
        data = flask_req.json or {}
        pos_id = data.get("position_id", "")
        reason = data.get("reason", "manual")
        exit_fees_real = data.get("exit_fees_real")

        uid = get_current_user_id()

        # Close in DB
        if uid and get_db_persist():
            try:
                db_pos_id = int(pos_id)
            except (ValueError, TypeError):
                return jsonify({"ok": False, "msg": "Posicion no encontrada"})

            from core.db_models import UserPosition
            from core.database import db as _db
            pos = UserPosition.query.filter_by(id=db_pos_id, user_id=uid, status="active").first()
            if not pos:
                return jsonify({"ok": False, "msg": "Posicion no encontrada"})

            # Persist the user-supplied real exit fees before computing PnL
            if exit_fees_real is not None:
                try:
                    val = float(exit_fees_real)
                    if val >= 0:
                        pos.exit_fees_real = val
                        _db.session.commit()
                except (TypeError, ValueError):
                    pass

            ih = pos.ih or 8
            el_h = (time.time() - pos.entry_time / 1000) / 3600
            earned = pos.earned_real or 0
            from portfolio.manager import position_fees as _pf
            _e, _x, fees, _ = _pf({
                "entry_fees": pos.entry_fees or 0,
                "exit_fees_est": pos.exit_fees_est or 0,
                "entry_fees_real": pos.entry_fees_real,
                "exit_fees_real": pos.exit_fees_real,
            })
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

        # Clear any notified alerts for this symbol so the next position on
        # the same symbol can re-trigger condition alerts.
        closed_sym = result["symbol"]
        stale_keys = [k for k in scanner_worker._notified_alerts if closed_sym in k]
        for k in stale_keys:
            scanner_worker._notified_alerts.pop(k, None)

        # Send Telegram notification — route per-user via the dispatcher so
        # the user's own credentials are loaded from DB even if the in-memory
        # state wasn't previously synced for this session.
        if scanner_worker.email_notifier:
            try:
                import time as _t
                # Unique dedup_key per close event (timestamp) so legit
                # consecutive closes of the same symbol are never suppressed,
                # but a duplicate POST of the same close is.
                close_id = int(_t.time())
                close_alert = {
                    "type": "POSITION_CLOSED",
                    "severity": "INFO",
                    "symbol": closed_sym,
                    "exchange": "",
                    "user_id": uid,
                    "funding_ts": close_id,
                    "dedup_key": f"POSITION_CLOSED_{closed_sym}_{uid}_{close_id}",
                    "message": (
                        f"Posicion cerrada ({reason}). "
                        f"Ganancia: ${result['earned']:.2f} | "
                        f"Fees: ${result['fees']:.2f} | "
                        f"Neto: ${result['net_earned']:.2f} | "
                        f"Duracion: {result['hours']:.1f}h | "
                        f"Pagos: {result['payments']}"
                    ),
                }
                scanner_worker._dispatch_alerts_per_user([close_alert])
            except Exception as e:
                log.warning(f"Telegram close notification failed: {e}")

        return jsonify({"ok": True, "result": result})

    # ── Auto-execution: place REAL orders via the user's API keys ──
    def _find_opp(opp_id):
        with state_manager.lock:
            s = state_manager.state
            opp = next((o for o in s.get("opportunities", []) if o.get("_id") == opp_id), None)
            if not opp:
                opp = next((o for o in s.get("defi_opportunities", []) if o.get("_id") == opp_id), None)
            return dict(opp) if opp else None

    def _resolve_spot_venue(opp, creds_by_exchange):
        """("cex", None) si hay spot centralizado operable; ("bsc", token) si el
        spot solo existe on-chain (Alpha BSC) y está habilitado; (None, None) si no."""
        from exchanges import trade_executor as texec
        ex = opp.get("exchange", "")
        if texec.spot_tradeable(ex, creds_by_exchange[ex.lower()], opp.get("symbol", "")):
            return "cex", None
        if ex.lower() == "binance":
            from exchanges import onchain_bsc
            if onchain_bsc.enabled():
                token = onchain_bsc.alpha_token(opp.get("symbol", ""))
                if token:
                    return "bsc", token
        return None, None

    def _parse_twap(raw):
        if not raw:
            return None
        return {"slices": max(1, min(96, int(raw.get("slices", 1)))),
                "duration_s": max(0, min(86400, int(raw.get("duration_s", 0))))}

    def _bsc_leg_factory(wallet, token):
        from exchanges import onchain_bsc
        return lambda pc, ps, ctx: onchain_bsc.BscSpotLeg(wallet, token, ctx, pc, ps)

    @app.route("/api/execute_open", methods=["POST"])
    @auth_required
    def api_execute_open():
        """Open a position by placing real orders (CEX perp + spot CEX/BSC).

        Body: {opportunity_id, capital, leverage, dry_run?, twap?:{slices,duration_s},
               allow_market_fallback?}.
        dry_run → simulación síncrona (rápida). Real → crea un ExecutionJob y
        devuelve {job_id}; el frontend hace polling de /api/exec/jobs/<id>.
        """
        from exchanges import trade_executor as texec
        data = flask_req.json or {}
        opp_id = data.get("opportunity_id", "")
        capital = float(data.get("capital", 0))
        leverage = max(1, int(data.get("leverage", 1)))
        dry_run = bool(data.get("dry_run", False))
        twap = _parse_twap(data.get("twap"))

        uid = get_current_user_id()
        if not uid or not get_db_persist():
            return jsonify({"ok": False, "msg": "DB no disponible"}), 400
        dbp = get_db_persist()

        opp = _find_opp(opp_id)
        if not opp:
            return jsonify({"ok": False, "msg": "Oportunidad no encontrada"})
        mode = opp.get("mode", "spot_perp")
        is_binance_sp = mode == "spot_perp" and opp.get("exchange", "").lower() == "binance"

        if mode == "spot_perp":
            needed = [opp.get("exchange", "")]
        elif mode == "cross_exchange":
            needed = [opp.get("long_exchange", ""), opp.get("short_exchange", "")]
        else:
            return jsonify({"ok": False, "msg": "DeFi no soportado en auto — usa el flujo manual"})
        for ex in needed:
            if not texec.is_cex(ex):
                return jsonify({"ok": False, "msg": f"{ex} no es un CEX soportado para auto-ejecución"})

        creds_by_exchange = {}
        for ex in needed:
            c = dbp.load_user_exchange_keys(uid, ex)
            if not c or not c.get("api_key"):
                return jsonify({"ok": False, "msg": f"Configura las API keys de {ex} en la pestaña Cuenta"})
            creds_by_exchange[ex.lower()] = c

        spot_venue, token = ("cex", None)
        if mode == "spot_perp":
            spot_venue, token = _resolve_spot_venue(opp, creds_by_exchange)
            if not spot_venue:
                return jsonify({"ok": False, "msg": "El spot de este par no es operable por API "
                                "(solo Alpha/Web3). Usa el flujo manual."})
            if spot_venue == "bsc" and not dbp.load_user_wallet(uid, "bsc"):
                return jsonify({"ok": False, "code": "NO_WALLET",
                                "msg": "Este token solo tiene spot on-chain (BSC). Configura tu wallet "
                                       "BSC dedicada en Cuenta para operarlo."})

        user_state = dbp.load_user_state(uid)
        # Una posición basyo por símbolo en Binance (one-way aislado agrega por símbolo).
        if is_binance_sp and any(
                p.get("symbol") == opp.get("symbol") and (p.get("exchange") or "").lower() == "binance"
                for p in user_state.get("positions", [])):
            return jsonify({"ok": False, "msg": f"Ya tienes una posición en {opp.get('symbol')} en "
                            "Binance; ciérrala antes de abrir otra en el mismo símbolo."})
        merged = {
            "total_capital": user_state.get("total_capital", 1000),
            "max_positions": user_state.get("max_positions", 5),
            "positions": user_state.get("positions", []),
            "history": user_state.get("history", []),
            "total_earned": user_state.get("total_earned", 0),
        }
        ok, result = open_position(merged, opp, capital, leverage)
        if not ok:
            return jsonify({"ok": False, "msg": result})

        if dry_run:
            leg_factory = None
            if spot_venue == "bsc":
                leg_factory = _bsc_leg_factory(dbp.load_user_wallet(uid, "bsc"), token)
            exec_res = texec.execute_open(creds_by_exchange, opp, capital, leverage, dry_run=True,
                                          twap=twap, spot_leg_factory=leg_factory)
            if not exec_res.get("ok"):
                return jsonify({"ok": False, "msg": exec_res.get("msg", "Simulación fallida"),
                                "code": exec_res.get("code"), "exec": exec_res})
            if is_binance_sp:
                exec_res["wallets"] = _wallet_check(uid, creds_by_exchange["binance"], capital,
                                                    leverage, spot_venue, user_state)
            return jsonify({"ok": True, "dry_run": True, "exec": exec_res,
                            "estimated_daily": result["estimated_daily"],
                            "fees_total": result["fees_total"]})

        if not job_runner:
            return jsonify({"ok": False, "msg": "Runner de ejecución no disponible"}), 503
        if job_runner.active_job(uid):
            return jsonify({"ok": False, "msg": "Ya hay una ejecución en curso; espera a que termine."})

        # Fondos en la wallet correcta (y transfer automático si está habilitado).
        if is_binance_sp:
            wc = _wallet_check(uid, creds_by_exchange["binance"], capital, leverage, spot_venue,
                               user_state, do_transfer=True)
            if not wc.get("ok"):
                return jsonify({"ok": False, "msg": wc.get("msg"), "code": "FUNDS"})

        params = {
            "symbol": opp.get("symbol"), "opportunity": opp, "capital": capital,
            "leverage": leverage, "twap": twap,
            "allow_market_fallback": bool(data.get("allow_market_fallback", True)),
            "spot_venue": "bsc" if spot_venue == "bsc" else "", "token": token or {},
        }
        jid = job_runner.submit(uid, "open", params)
        log.info(f"Exec job #{jid} open {opp.get('symbol')} user={uid} cap={capital} lev={leverage} twap={twap}")
        return jsonify({"ok": True, "job_id": jid})

    def _wallet_check(uid, creds, capital, leverage, spot_venue, user_state, do_transfer=False):
        """Verifica USDT en spot (o wallet BSC) y margen en futuros; transfiere si procede."""
        try:
            from exchanges.binance_account import BinanceAccount
            acct = BinanceAccount(creds)
            bal = acct.balances()
        except Exception as e:
            return {"ok": True, "msg": f"No se pudo verificar saldos: {e}", "unchecked": True}
        margin = capital / (leverage + 1)
        spot_need = capital - margin
        spot_have = bal["spot_usdt_free"]
        if spot_venue == "bsc":
            spot_have = (user_state.get("real_balances") or {}).get("wallet", 0)
        fut_have = bal["futures_available"]
        out = {"ok": True, "spot_need": round(spot_need, 2), "spot_have": round(spot_have, 2),
               "futures_need": round(margin, 2), "futures_have": round(fut_have, 2),
               "transfers": []}
        short_fut = max(0.0, margin * 1.02 - fut_have)
        short_spot = max(0.0, spot_need * 1.003 - spot_have)
        if short_fut <= 0 and short_spot <= 0:
            return out
        auto = bool(user_state.get("auto_transfer"))
        if spot_venue != "bsc" and auto:
            if short_fut > 0 and spot_have - spot_need >= short_fut:
                out["transfers"].append({"to": "future", "amount": round(short_fut, 2)})
                short_fut = 0
            elif short_spot > 0 and fut_have - margin * 1.02 >= short_spot:
                out["transfers"].append({"to": "spot", "amount": round(short_spot, 2)})
                short_spot = 0
        if short_fut > 0 or short_spot > 0:
            where = []
            if short_spot > 0:
                where.append(f"faltan ${short_spot:.2f} USDT en "
                             + ("la wallet BSC" if spot_venue == "bsc" else "Spot"))
            if short_fut > 0:
                where.append(f"faltan ${short_fut:.2f} USDT de margen en Futuros USDⓈ-M")
            out.update(ok=False, msg="Fondos insuficientes: " + "; ".join(where)
                       + ("" if auto else ". Activa 'Transferir automáticamente' o mueve los fondos."))
            return out
        if do_transfer:
            for t in out["transfers"]:
                r = acct.transfer_usdt(t["amount"], t["to"])
                if not r.get("ok"):
                    return {"ok": False, "msg": r.get("msg")}
                log.info(f"Auto-transfer user={uid} {t}")
        return out

    @app.route("/api/execute_close", methods=["POST"])
    @auth_required
    def api_execute_close():
        """Close a position by placing the reverse orders (background job).

        Body: {position_id, dry_run?, twap?}.
        """
        from exchanges import trade_executor as texec
        data = flask_req.json or {}
        pos_id = data.get("position_id", "")
        dry_run = bool(data.get("dry_run", False))
        twap = _parse_twap(data.get("twap"))

        uid = get_current_user_id()
        if not uid or not get_db_persist():
            return jsonify({"ok": False, "msg": "DB no disponible"}), 400
        dbp = get_db_persist()

        try:
            db_pos_id = int(pos_id)
        except (ValueError, TypeError):
            return jsonify({"ok": False, "msg": "Posicion no encontrada"})

        from core.db_models import UserPosition
        pos_row = UserPosition.query.filter_by(id=db_pos_id, user_id=uid, status="active").first()
        if not pos_row:
            return jsonify({"ok": False, "msg": "Posicion no encontrada"})
        pos = dbp._pos_to_dict(pos_row)
        if pos.get("source") not in ("auto", "imported"):
            return jsonify({"ok": False, "msg": "Solo las posiciones abiertas o importadas desde el "
                            "exchange se pueden cerrar en automático."})

        mode = pos.get("mode", "spot_perp")
        needed = ([pos.get("exchange", "")] if mode == "spot_perp"
                  else [pos.get("long_exchange", ""), pos.get("short_exchange", "")])
        creds_by_exchange = {}
        for ex in needed:
            if not texec.is_cex(ex):
                return jsonify({"ok": False, "msg": f"{ex} no soportado para auto-cierre"})
            c = dbp.load_user_exchange_keys(uid, ex)
            if not c or not c.get("api_key"):
                return jsonify({"ok": False, "msg": f"Sin API keys para {ex}"})
            creds_by_exchange[ex.lower()] = c

        token = {}
        if pos.get("spot_venue") == "bsc":
            from exchanges import onchain_bsc
            token = onchain_bsc.alpha_token(pos.get("symbol", "")) or {}
            if not token or not dbp.load_user_wallet(uid, "bsc"):
                return jsonify({"ok": False, "msg": "Wallet BSC o token no disponible"})

        if dry_run:
            leg_factory = (_bsc_leg_factory(dbp.load_user_wallet(uid, "bsc"), token)
                           if pos.get("spot_venue") == "bsc" else None)
            exec_res = texec.execute_close(creds_by_exchange, pos, dry_run=True,
                                           spot_leg_factory=leg_factory)
            if not exec_res.get("ok"):
                return jsonify({"ok": False, "msg": exec_res.get("msg", "Simulación fallida"), "exec": exec_res})
            return jsonify({"ok": True, "dry_run": True, "exec": exec_res})

        if not job_runner:
            return jsonify({"ok": False, "msg": "Runner de ejecución no disponible"}), 503
        if job_runner.active_job(uid):
            return jsonify({"ok": False, "msg": "Ya hay una ejecución en curso; espera a que termine."})
        jid = job_runner.submit(uid, "close", {"position_id": db_pos_id, "symbol": pos.get("symbol"),
                                               "twap": twap, "token": token})
        log.info(f"Exec job #{jid} close pos={db_pos_id} user={uid}")
        return jsonify({"ok": True, "job_id": jid})

    # ── Execution jobs ─────────────────────────────────────────
    @app.route("/api/exec/jobs/<int:job_id>")
    @auth_required
    def api_exec_job(job_id):
        from core.db_models import ExecutionJob
        uid = get_current_user_id()
        job = ExecutionJob.query.filter_by(id=job_id, user_id=uid).first()
        if not job or not job_runner:
            return jsonify({"ok": False, "msg": "Job no encontrado"}), 404
        return jsonify({"ok": True, "job": job_runner.to_dict(job)})

    @app.route("/api/exec/jobs/active")
    @auth_required
    def api_exec_job_active():
        uid = get_current_user_id()
        job = job_runner.active_job(uid) if job_runner and uid else None
        return jsonify({"ok": True, "job": job_runner.to_dict(job) if job else None})

    @app.route("/api/exec/jobs/<int:job_id>/cancel", methods=["POST"])
    @auth_required
    def api_exec_job_cancel(job_id):
        from core.database import db as _db
        from core.db_models import ExecutionJob
        uid = get_current_user_id()
        job = ExecutionJob.query.filter_by(id=job_id, user_id=uid).first()
        if not job or job.status not in ("queued", "running"):
            return jsonify({"ok": False, "msg": "El job no está activo"})
        job.cancel_requested = True
        _db.session.commit()
        return jsonify({"ok": True, "msg": "Cancelación solicitada: se cubre lo ejecutado y se detiene"})

    # ── Update real fees on an open position ───────────────────
    @app.route("/api/positions/<pos_id>/fees", methods=["PATCH", "POST"])
    @auth_required
    def api_update_position_fees(pos_id):
        """Let the user enter the real fees they actually paid.

        Body: { "entry_fees_real"?: float, "exit_fees_real"?: float }
        Any missing key is left untouched.  Sending null clears an
        existing override (reverts to the estimate).
        """
        uid = get_current_user_id()
        if not uid or not get_db_persist():
            return jsonify({"ok": False, "msg": "DB no disponible"}), 400

        try:
            db_pos_id = int(pos_id)
        except (ValueError, TypeError):
            return jsonify({"ok": False, "msg": "ID invalido"}), 400

        from core.database import db as _db
        from core.db_models import UserPosition
        pos = UserPosition.query.filter_by(
            id=db_pos_id, user_id=uid, status="active"
        ).first()
        if not pos:
            return jsonify({"ok": False, "msg": "Posicion no encontrada"}), 404

        data = flask_req.json or {}
        updated = []

        if "entry_fees_real" in data:
            v = data["entry_fees_real"]
            if v is None or v == "":
                pos.entry_fees_real = None
                updated.append("entry_fees_real=null")
            else:
                try:
                    pos.entry_fees_real = max(0.0, float(v))
                    updated.append(f"entry_fees_real={pos.entry_fees_real}")
                except (TypeError, ValueError):
                    return jsonify({"ok": False, "msg": "entry_fees_real invalido"}), 400

        if "exit_fees_real" in data:
            v = data["exit_fees_real"]
            if v is None or v == "":
                pos.exit_fees_real = None
                updated.append("exit_fees_real=null")
            else:
                try:
                    pos.exit_fees_real = max(0.0, float(v))
                    updated.append(f"exit_fees_real={pos.exit_fees_real}")
                except (TypeError, ValueError):
                    return jsonify({"ok": False, "msg": "exit_fees_real invalido"}), 400

        if not updated:
            return jsonify({"ok": False, "msg": "Nada que actualizar"}), 400

        _db.session.commit()
        log.info(f"Position {db_pos_id} fees updated: {', '.join(updated)}")

        from portfolio.manager import position_fees as _pf
        entry, exit_, total, is_real = _pf({
            "entry_fees": pos.entry_fees or 0,
            "exit_fees_est": pos.exit_fees_est or 0,
            "entry_fees_real": pos.entry_fees_real,
            "exit_fees_real": pos.exit_fees_real,
        })

        return jsonify({
            "ok": True,
            "position_id": db_pos_id,
            "entry_fees": entry,
            "exit_fees": exit_,
            "fees_total": total,
            "fees_is_real": is_real,
            "entry_fees_real": pos.entry_fees_real,
            "exit_fees_real": pos.exit_fees_real,
            "entry_fees_est": pos.entry_fees or 0,
            "exit_fees_est": pos.exit_fees_est or 0,
        })

    # ── Manual earnings override (active position) ─────────────
    @app.route("/api/positions/<pos_id>/earnings", methods=["PATCH", "POST"])
    @auth_required
    def api_update_position_earnings(pos_id):
        """Let the user manually set the accumulated earnings for an active
        position when their exchange shows a different total.

        Body: { "earned": <float> }
        Semantics: absolute rewriteable — the value becomes the new running
        total. The scanner keeps adding future settlements on top because
        _count_payments_since uses `last_earn_update` as the lower bound,
        which we bump to now. An audit entry is appended to payments_json.
        """
        uid = get_current_user_id()
        if not uid or not get_db_persist():
            return jsonify({"ok": False, "msg": "DB no disponible"}), 400

        try:
            db_pos_id = int(pos_id)
        except (ValueError, TypeError):
            return jsonify({"ok": False, "msg": "ID invalido"}), 400

        from core.database import db as _db
        from core.db_models import UserPosition
        pos = UserPosition.query.filter_by(
            id=db_pos_id, user_id=uid, status="active"
        ).first()
        if not pos:
            return jsonify({"ok": False, "msg": "Posicion no encontrada"}), 404

        data = flask_req.json or {}
        if "earned" not in data or data["earned"] is None or data["earned"] == "":
            return jsonify({"ok": False, "msg": "Falta campo 'earned'"}), 400

        try:
            new_earned = float(data["earned"])
        except (TypeError, ValueError):
            return jsonify({"ok": False, "msg": "'earned' invalido"}), 400

        import math
        if not math.isfinite(new_earned):
            return jsonify({"ok": False, "msg": "'earned' no finito"}), 400

        prev_earned = float(pos.earned_real or 0)
        delta = new_earned - prev_earned
        now_ts = time.time()

        payments = list(pos.payments_json or [])
        payments.append({
            "ts": now_ts,
            "rate": 0,
            "earned": delta,
            "cumulative": new_earned,
            "kind": "manual_adjust",
            "note": "user override",
        })
        pos.earned_real = new_earned
        pos.last_earn_update = now_ts
        pos.payments_json = payments

        _db.session.commit()
        log.info(f"Position {db_pos_id} earnings manually set: "
                 f"{prev_earned:.4f} -> {new_earned:.4f} (delta {delta:+.4f})")

        return jsonify({
            "ok": True,
            "position_id": db_pos_id,
            "earned": new_earned,
            "delta": delta,
            "last_earn_update": now_ts,
        })

    # ── Manual earnings override (closed position) ─────────────
    @app.route("/api/history/<hist_id>/earnings", methods=["PATCH", "POST"])
    @auth_required
    def api_update_history_earnings(hist_id):
        """Edit accumulated earnings/fees of a closed position.

        Body: { "earned"?: float, "fees"?: float }
        Any field omitted is left untouched. `net_earned` is recomputed.
        """
        uid = get_current_user_id()
        if not uid or not get_db_persist():
            return jsonify({"ok": False, "msg": "DB no disponible"}), 400

        try:
            db_hist_id = int(hist_id)
        except (ValueError, TypeError):
            return jsonify({"ok": False, "msg": "ID invalido"}), 400

        from core.database import db as _db
        from core.db_models import UserHistory
        hist = UserHistory.query.filter_by(id=db_hist_id, user_id=uid).first()
        if not hist:
            return jsonify({"ok": False, "msg": "Registro no encontrado"}), 404

        data = flask_req.json or {}
        import math
        updated = []

        if "earned" in data and data["earned"] is not None and data["earned"] != "":
            try:
                v = float(data["earned"])
                if not math.isfinite(v):
                    raise ValueError()
                hist.earned = v
                updated.append(f"earned={v}")
            except (TypeError, ValueError):
                return jsonify({"ok": False, "msg": "'earned' invalido"}), 400

        if "fees" in data and data["fees"] is not None and data["fees"] != "":
            try:
                v = float(data["fees"])
                if not math.isfinite(v):
                    raise ValueError()
                hist.fees = max(0.0, v)
                updated.append(f"fees={hist.fees}")
            except (TypeError, ValueError):
                return jsonify({"ok": False, "msg": "'fees' invalido"}), 400

        if not updated:
            return jsonify({"ok": False, "msg": "Nada que actualizar"}), 400

        hist.net_earned = (hist.earned or 0) - (hist.fees or 0)
        stamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        note_line = f"[{stamp}] manual edit: {', '.join(updated)}"
        hist.notes = (hist.notes + "\n" + note_line) if hist.notes else note_line

        _db.session.commit()
        log.info(f"History {db_hist_id} edited: {', '.join(updated)}")

        return jsonify({
            "ok": True,
            "history_id": db_hist_id,
            "earned": hist.earned,
            "fees": hist.fees,
            "net_earned": hist.net_earned,
            "notes": hist.notes,
        })

    # ── Daily earnings rollup ──────────────────────────────────
    @app.route("/api/earnings/daily")
    @auth_required
    def api_earnings_daily():
        """Daily breakdown of earnings combining active + closed positions."""
        uid = get_current_user_id()
        if not uid or not get_db_persist():
            return jsonify({"ok": False, "msg": "DB no disponible"}), 400
        try:
            days = int(flask_req.args.get("days", 30))
        except (TypeError, ValueError):
            days = 30
        data = get_db_persist().aggregate_daily_earnings(uid, days=days)
        data["ok"] = True
        return jsonify(data)

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

    # ── Test Telegram ─────────────────────────────────────────
    @app.route("/api/test_email", methods=["POST"])
    def api_test_telegram():
        """Send a test Telegram message via Bot API.

        Tests the FULL alert pipeline (send_alerts -> send_alert)
        using a simulated alert, not just the raw HTTP call.
        """
        n = scanner_worker.email_notifier
        if not n:
            return jsonify({"ok": False, "msg": "Notifier no disponible"})

        n._sync_from_state()
        if not all([n.tg_chat_id, n.tg_bot_token]):
            return jsonify({"ok": False, "msg": "Configura Bot Token y Chat ID primero"})

        # Unique dedup_key per request avoids racing _sent_cache mutation and
        # lets the same user re-run the test without being silenced by cooldown.
        test_alert = {
            "type": "TEST_TELEGRAM",
            "severity": "CRITICAL",
            "symbol": "TEST",
            "exchange": "Bot",
            "message": "Prueba de alerta automatica — pipeline completo OK",
            "dedup_key": f"TEST_TELEGRAM:{time.time_ns()}",
        }

        sent = n.send_alerts([test_alert])
        if sent > 0:
            return jsonify({"ok": True, "msg": f"Telegram enviado a chat {n.tg_chat_id} (pipeline completo)"})

        diag = []
        if not n.enabled:
            diag.append(f"Notificaciones deshabilitadas (email_enabled={n.enabled})")
        if not n.tg_chat_id:
            diag.append("Chat ID vacio")
        if not n.tg_bot_token:
            diag.append("Bot Token vacio")

        if not diag:
            try:
                n._send_telegram("\u2705 Funding Bot — Prueba de Telegram OK")
                return jsonify({"ok": True, "msg": f"Telegram enviado (fallback directo) a chat {n.tg_chat_id}"})
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
            "telegram": {
                "notifier_exists": n is not None,
                "enabled": n.enabled if n else False,
                "chat_id_set": bool(n.tg_chat_id) if n else False,
                "token_set": bool(n.tg_bot_token) if n else False,
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

    # ── Score History ──────────────────────────────────────────
    @app.route("/api/score_history/<symbol>/<exchange>")
    def api_score_history(symbol, exchange):
        """Get score evolution for a symbol+exchange pair."""
        if not db_enabled:
            return jsonify({"error": "DB not enabled"}), 400
        try:
            trend = db_persistence.get_score_trend(symbol, exchange)
            return jsonify(trend)
        except Exception as e:
            return jsonify({"scores": [], "trend": "new", "error": str(e)})

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
            from core.encryption import encrypt_value, EncryptionUnavailable

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
                from core.db_models import UserAccountSnapshot
                UserAccountSnapshot.query.filter_by(user_id=current_user.id,
                                                    exchange=exchange.lower()).delete()
                _db.session.commit()
                return jsonify({"ok": True, "msg": f"Keys de {exchange} eliminadas"})

            # Binance: validar permisos ANTES de guardar (rechaza keys con retiros).
            check = None
            if exchange.lower() == "binance":
                from exchanges.binance_account import BinanceAccount
                check = BinanceAccount({"api_key": api_key, "api_secret": api_secret}).check()
                perm = check.get("permissions") or {}
                if not check.get("ok") and (perm.get("errors") or not perm):
                    return jsonify({"ok": False, "msg": check.get("msg"), "check": check})

            try:
                enc = (encrypt_value(api_key, required=True), encrypt_value(api_secret, required=True),
                       encrypt_value(passphrase, required=True) if passphrase else None)
            except EncryptionUnavailable as e:
                return jsonify({"ok": False, "msg": str(e)}), 500

            if not existing:
                existing = UserExchangeKey(user_id=current_user.id, exchange_name=exchange)
                _db.session.add(existing)
            existing.api_key_encrypted, existing.api_secret_encrypted = enc[0], enc[1]
            if enc[2] is not None or not existing.passphrase_encrypted:
                existing.passphrase_encrypted = enc[2] or ""
            _db.session.commit()

            if exchange.lower() == "binance" and account_syncer:
                threading.Thread(target=account_syncer.sync_user, args=(current_user.id,),
                                 daemon=True).start()
            return jsonify({"ok": True, "msg": f"Keys de {exchange} guardadas", "check": check})

        @app.route("/api/account/exchange_keys/test", methods=["POST"])
        @auth_required
        def api_test_exchange_keys():
            """Validate stored API keys (Binance: permisos + modo de cuenta + saldos)."""
            from exchanges import trade_executor as texec
            uid = get_current_user_id()
            if not uid or not get_db_persist():
                return jsonify({"ok": False, "msg": "DB no disponible"}), 400
            data = flask_req.get_json() or {}
            exchange = (data.get("exchange", "") or "").strip()
            if not texec.is_cex(exchange):
                return jsonify({"ok": False, "msg": f"{exchange} no soportado para auto-trading"})
            creds = get_db_persist().load_user_exchange_keys(uid, exchange)
            if not creds or not creds.get("api_key"):
                return jsonify({"ok": False, "msg": f"No hay keys guardadas para {exchange}"})
            return jsonify(texec.test_connection(exchange, creds))

        # ── Binance: cuenta real ─────────────────────────────
        def _binance_creds(uid):
            dbp = get_db_persist()
            return dbp.load_user_exchange_keys(uid, "Binance") or dbp.load_user_exchange_keys(uid, "binance")

        @app.route("/api/account/binance")
        @auth_required
        def api_binance_status():
            """Último snapshot sincronizado (saldos, posiciones, permisos, importables, drift)."""
            uid = get_current_user_id()
            dbp = get_db_persist()
            snap = dbp.get_account_snapshot(uid, "binance")
            wsnap = dbp.get_account_snapshot(uid, "bsc_wallet")
            return jsonify({"ok": True, "connected": bool(_binance_creds(uid)),
                            "snapshot": snap, "wallet": wsnap,
                            "real_balances": dbp.real_free_usdt(uid)})

        @app.route("/api/account/binance/sync", methods=["POST"])
        @auth_required
        def api_binance_sync():
            uid = get_current_user_id()
            if not account_syncer:
                return jsonify({"ok": False, "msg": "Sync no disponible"}), 503
            if not _binance_creds(uid) and not get_db_persist().load_user_wallet(uid, "bsc"):
                return jsonify({"ok": False, "msg": "Configura tus API keys de Binance"})
            snap = account_syncer.sync_user(uid)
            if snap and snap.get("ok") is False:
                return jsonify({"ok": False, "msg": f"Sync falló: {snap.get('error')}"})
            return jsonify({"ok": True, "snapshot": get_db_persist().get_account_snapshot(uid, "binance")})

        @app.route("/api/account/binance/multi_assets_off", methods=["POST"])
        @auth_required
        def api_binance_multi_assets_off():
            """Desactiva Multi-Assets (requiere confirmación explícita del usuario)."""
            uid = get_current_user_id()
            data = flask_req.get_json() or {}
            if data.get("confirm") is not True:
                return jsonify({"ok": False, "msg": "Confirmación requerida"}), 400
            creds = _binance_creds(uid)
            if not creds:
                return jsonify({"ok": False, "msg": "Configura tus API keys de Binance"})
            from exchanges.binance_account import BinanceAccount
            res = BinanceAccount(creds).disable_multi_assets()
            log.info(f"multi_assets_off user={uid}: {res}")
            if res.get("ok") and account_syncer:
                account_syncer.request()
            return jsonify(res)

        @app.route("/api/account/binance/import", methods=["POST"])
        @auth_required
        def api_binance_import():
            """Importa un short USDⓈ-M existente (+ spot si lo hay) como posición basyo."""
            uid = get_current_user_id()
            dbp = get_db_persist()
            data = flask_req.get_json() or {}
            symbol = (data.get("symbol") or "").upper()
            snap = dbp.get_account_snapshot(uid, "binance")
            imp = next((p for p in ((snap or {}).get("data") or {}).get("importable", [])
                        if p.get("symbol", "").upper() == symbol), None)
            if not imp:
                return jsonify({"ok": False, "msg": "Posición no encontrada entre las importables; sincroniza primero"})
            state = dbp.load_user_state(uid)
            if any(p.get("symbol") == symbol and (p.get("exchange") or "").lower() == "binance"
                   for p in state.get("positions", [])):
                return jsonify({"ok": False, "msg": f"{symbol} ya está registrada en basyo"})
            # Datos de mercado del scan (tasa, intervalo) si existen.
            with state_manager.lock:
                cur = next((d for d in state_manager.state.get("all_data", [])
                            if d.get("symbol") == symbol and str(d.get("exchange", "")).lower() == "binance"), {})
            qty = float(imp["qty"])
            spot_held = float(imp.get("spot_held") or 0)
            entry = float(imp.get("entry_price") or imp.get("mark_price") or 0)
            lev = int(round(float(imp.get("leverage") or 1)))
            margin = float(imp.get("isolated_margin") or (qty * entry / max(1, lev)))
            pos = {
                "symbol": symbol, "exchange": "Binance", "mode": "spot_perp",
                "entry_fr": cur.get("fr", 0), "entry_price": entry,
                "entry_time": int(imp.get("update_time") or time.time() * 1000),
                "capital_used": round(min(spot_held, qty) * entry + margin, 4),
                "leverage": max(1, lev), "exposure": round(qty * entry, 4),
                "ih": cur.get("ih", 8), "entry_fees": 0, "exit_fees_est": 0,
                "auto_executed": False, "source": "imported",
                "spot_qty": spot_held, "perp_qty": qty, "spot_venue": "binance",
                "earnings_source": "real",
                "payments_json": [{"ts": time.time(), "kind": "import",
                                   "note": f"Importada de Binance: short {qty:g}, spot {spot_held:g}"}],
            }
            pid = dbp.save_position(uid, pos)
            if account_syncer:
                account_syncer.request()
            msg = f"{symbol} importada"
            if spot_held < qty * 0.95:
                msg += f" — ojo: spot {spot_held:g} no cubre el short {qty:g} (posición no delta-neutral)"
            return jsonify({"ok": True, "msg": msg, "position_id": pid})

        @app.route("/api/account/settings", methods=["POST"])
        @auth_required
        def api_account_settings():
            uid = get_current_user_id()
            data = flask_req.get_json() or {}
            upd = {}
            if "auto_transfer" in data:
                upd["auto_transfer"] = bool(data["auto_transfer"])
            if upd:
                get_db_persist().save_user_config(uid, upd)
            return jsonify({"ok": True})

        # ── Wallet BSC dedicada ──────────────────────────────
        @app.route("/api/account/wallet", methods=["GET", "POST", "DELETE"])
        @auth_required
        def api_wallet():
            from core.database import db as _db
            from core.db_models import UserWallet
            uid = get_current_user_id()
            row = UserWallet.query.filter_by(user_id=uid, chain="bsc").first()
            if flask_req.method == "GET":
                snap = get_db_persist().get_account_snapshot(uid, "bsc_wallet")
                return jsonify({"ok": True, "wallet": {"address": row.address} if row else None,
                                "snapshot": snap})
            if flask_req.method == "DELETE":
                if row:
                    _db.session.delete(row)
                    _db.session.commit()
                return jsonify({"ok": True, "msg": "Wallet eliminada de basyo"})
            data = flask_req.get_json() or {}
            if data.get("accept_risk") is not True:
                return jsonify({"ok": False, "msg": "Debes aceptar el aviso de riesgo"}), 400
            from exchanges import onchain_bsc
            from core.encryption import encrypt_value, EncryptionUnavailable
            try:
                address = onchain_bsc.address_from_key(data.get("private_key", ""))
            except Exception:
                return jsonify({"ok": False, "msg": "Private key inválida (64 hex)"}), 400
            try:
                enc = encrypt_value(onchain_bsc.normalize_key(data["private_key"]), required=True)
            except EncryptionUnavailable as e:
                return jsonify({"ok": False, "msg": str(e)}), 500
            if not row:
                row = UserWallet(user_id=uid, chain="bsc", address=address, pk_encrypted=enc)
                _db.session.add(row)
            else:
                row.address, row.pk_encrypted = address, enc
            _db.session.commit()
            if account_syncer:
                threading.Thread(target=account_syncer.sync_user, args=(uid,), daemon=True).start()
            return jsonify({"ok": True, "msg": "Wallet BSC guardada", "address": address})

        @app.route("/api/account", methods=["DELETE"])
        @auth_required
        def api_delete_account():
            from flask_login import current_user, logout_user
            from core.database import db as _db
            _db.session.delete(current_user)
            _db.session.commit()
            logout_user()
            return jsonify({"ok": True, "msg": "Cuenta eliminada"})
