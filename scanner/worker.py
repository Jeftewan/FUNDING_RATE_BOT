"""Background monitor — v8.4 minimal API calls.

Scans happen in 3 moments:
1. ~10 min before a funding payment (verify rate is still good, alerts)
2. ~3 min after a funding payment (capture exact settled rate from
   exchange history + refresh next_funding_ts in one pass)
3. Manual scan (user clicks button)

NO periodic scan loop. Monitor runs every 30s checking timestamps
locally (zero API calls) and triggers scans only when needed.
"""
import time
import logging
import threading
from datetime import datetime
from analysis.ai_analyzer import analyze_top_opportunities
from analysis.indicators import detect_exceptional

log = logging.getLogger("bot")

# Pre-payment scan: minutes before payment to trigger verification scan
PRE_PAYMENT_SCAN_MINS = 10
# Post-settlement delay: seconds after the actual settlement before the
# post-payment scan fires. Gives the exchange time to (a) expose the
# settled rate via fetch_funding_rate_history and (b) update
# nextFundingTime to the following period. Replaces the old
# POST_PAYMENT_SCAN_MINS + REFRESH_AFTER_PAYMENT_SECS pair.
POST_SETTLEMENT_DELAY_SECS = 3 * 60
# Tolerance (in seconds) when matching a settlement timestamp against
# historical entries returned by the exchange / DB snapshots.
SETTLEMENT_RATE_TOLERANCE_SECS = 120


class ScannerWorker:
    def __init__(self, exchange_manager, arbitrage_scanner, state_manager,
                 coinglass_client, config, email_notifier=None,
                 defi_manager=None):
        self.exchange_manager = exchange_manager
        self.arb_scanner = arbitrage_scanner
        self.state_manager = state_manager
        self.coinglass = coinglass_client
        self.config = config
        self.email_notifier = email_notifier
        self.defi_manager = defi_manager
        self._flask_app = None  # Set by app.py for DB access
        self._started = False
        self._last_scan_ts = 0
        self._scan_lock = threading.Lock()
        self._scanned_events = set()
        self._notified_alerts = set()
        self._sl_tp_review_sent = {}  # {position_id: last_sent_timestamp}
        self._db_persist = None  # Lazy-init DBPersistence
        self._last_switch_analysis = 0  # Timestamp of last switch analysis
        self._switch_results = {}  # {position_id: switch_analysis_result}
        self._prev_exceptional_keys = set()  # Exceptional keys from previous scan

    def start(self):
        if self._started:
            return
        self._started = True
        # Only ONE thread — the monitor. No periodic scan loop.
        threading.Thread(target=self._monitor_loop, daemon=True).start()
        log.info("Monitor started (scans only on payment events + manual)")

    # ── Monitor Loop (runs every 30s, ZERO api calls) ─────────

    def _monitor_loop(self):
        """Every 30s: check payment schedule locally, trigger scan if needed."""
        # Initial scan to have data on first load
        time.sleep(5)
        try:
            log.info("Initial scan on startup...")
            self._run_scan()
        except Exception as e:
            log.error(f"Initial scan failed: {e}")

        while True:
            try:
                self._monitor_tick()
            except Exception as e:
                log.exception(f"Monitor error: {e}")
            time.sleep(30)

    def _get_db_persist(self):
        """Lazy-init DBPersistence."""
        if self._db_persist is None and self._flask_app:
            from core.db_persistence import DBPersistence
            self._db_persist = DBPersistence()
        return self._db_persist

    def _load_all_positions_from_db(self) -> list:
        """Load all active positions from DB (all users). Returns list of dicts."""
        if not self._flask_app:
            return []
        try:
            with self._flask_app.app_context():
                db_persist = self._get_db_persist()
                if not db_persist:
                    return []
                db_positions = db_persist.get_all_active_positions()
                return [self._db_pos_to_dict(p) for p in db_positions]
        except Exception as e:
            log.error(f"Failed to load positions from DB: {e}")
            return []

    @staticmethod
    def _db_pos_to_dict(pos) -> dict:
        """Convert a UserPosition ORM object to a dict for scanner use."""
        return {
            "db_id": pos.id,
            "user_id": pos.user_id,
            "id": str(pos.id),
            "symbol": pos.symbol,
            "exchange": pos.exchange,
            "mode": pos.mode,
            "entry_fr": pos.entry_fr,
            "entry_price": pos.entry_price,
            "entry_time": pos.entry_time,
            "capital_used": pos.capital_used,
            "leverage": pos.leverage or 1,
            "exposure": pos.exposure or (pos.capital_used / 2),
            "ih": pos.ih,
            "earned_real": pos.earned_real,
            "last_earn_update": pos.last_earn_update or (pos.entry_time / 1000 if pos.entry_time else 0),
            "last_fr_used": pos.last_fr_used or 0,
            "long_exchange": pos.long_exchange or "",
            "short_exchange": pos.short_exchange or "",
            "payment_count": pos.payment_count or 0,
            "avg_rate": pos.avg_rate or 0,
            "entry_fees": pos.entry_fees or 0,
            "exit_fees_est": getattr(pos, "exit_fees_est", 0) or 0,
            "entry_fees_real": getattr(pos, "entry_fees_real", None),
            "exit_fees_real": getattr(pos, "exit_fees_real", None),
            "payments": pos.payments_json or [],
        }

    def _save_position_earnings_to_db(self, pos: dict):
        """Write updated earnings for a position back to DB."""
        if not self._flask_app or "db_id" not in pos:
            return
        try:
            with self._flask_app.app_context():
                db_persist = self._get_db_persist()
                if db_persist:
                    db_persist.update_position_earnings(
                        pos["db_id"],
                        earned=pos.get("earned_real", 0),
                        payment_count=pos.get("payment_count", 0),
                        avg_rate=pos.get("avg_rate", 0),
                        last_fr=pos.get("last_fr_used", 0),
                        payments=pos.get("payments", []),
                    )
        except Exception as e:
            log.error(f"Failed to save earnings for position {pos.get('db_id')}: {e}")

    def _monitor_tick(self):
        """Single monitor tick — check positions, trigger scans if needed.

        Loads positions from DB (all users), checks payment triggers,
        updates earnings, and generates alerts.
        """
        scan_reason = None
        alerts = []
        now = time.time()

        # Load positions from DB (all users' active positions)
        positions = self._load_all_positions_from_db()

        with self.state_manager.lock:
            s = self.state_manager.state
            all_data = s.get("all_data", [])
            defi_data = s.get("defi_data", [])

            if not positions:
                self._refresh_mins_next(all_data, now, defi_data)
                return

            self._refresh_mins_next(all_data, now, defi_data)

            # Optimize: deduplicate positions by (symbol, exchange) for trigger checks
            # Multiple users with same symbol/exchange only need ONE trigger check
            triggered_keys = []
            triggered_labels = []
            combined = all_data + defi_data
            seen_pairs = set()  # (symbol, exchange) already checked

            for pos in positions:
                sym = pos["symbol"]
                ex = pos["exchange"]
                pair_key = f"{sym}_{ex}"
                if pair_key in seen_pairs:
                    continue
                seen_pairs.add(pair_key)

                # Each leg of the position has its own funding schedule.
                # For cross_exchange we evaluate both legs independently.
                legs = []
                if pos.get("mode") == "cross_exchange":
                    long_ex = pos.get("long_exchange", "")
                    short_ex = pos.get("short_exchange", pos.get("exchange", ""))
                    long_d = next(
                        (d for d in combined
                         if d["symbol"] == sym and d["exchange"] == long_ex),
                        None,
                    )
                    short_d = next(
                        (d for d in combined
                         if d["symbol"] == sym and d["exchange"] == short_ex),
                        None,
                    )
                    if long_d:
                        legs.append((long_ex, long_d))
                    if short_d:
                        legs.append((short_ex, short_d))
                else:
                    cur = self._find_data(pos, all_data, defi_data)
                    if cur:
                        legs.append((ex, cur))

                for leg_ex, leg in legs:
                    mn = leg.get("mins_next", -1)
                    nts = leg.get("next_funding_ts", 0)
                    ih = leg.get("ih", 8)
                    interval_secs = max(1, int(ih) * 3600)

                    # PRE: 1-10 min before next payment (rate verification + alerts)
                    if PRE_PAYMENT_SCAN_MINS >= mn > 0:
                        pre_key = f"pre_{sym}_{leg_ex}_{nts}"
                        if pre_key not in self._scanned_events:
                            triggered_keys.append(pre_key)
                            triggered_labels.append(
                                f"Pre-pago {sym}@{leg_ex} en {mn:.0f}min"
                            )

                    # POST: settlement happened ≥ POST_SETTLEMENT_DELAY_SECS ago
                    # (and within the current interval, so we don't backfill old
                    # payments). Captures the exact settled rate via history and
                    # refreshes next_funding_ts in a single pass.
                    last_pay = self._calc_last_payment_ts(nts, interval_secs, now)
                    if last_pay > 0:
                        elapsed = now - last_pay
                        if POST_SETTLEMENT_DELAY_SECS <= elapsed < interval_secs:
                            post_key = f"post_{sym}_{leg_ex}_{int(last_pay)}"
                            if post_key not in self._scanned_events:
                                triggered_keys.append(post_key)
                                triggered_labels.append(f"Pago {sym}@{leg_ex}")

            if triggered_keys:
                scan_reason = " + ".join(triggered_labels)
                for ek in triggered_keys:
                    self._scanned_events.add(ek)

        # Trigger scan if needed (outside lock)
        if scan_reason:
            log.info(f"Scan trigger: {scan_reason}")
            try:
                self._run_scan()
            except Exception as e:
                log.error(f"Triggered scan failed: {e}")

        # Process earnings and alerts (re-read market data after possible scan)
        with self.state_manager.lock:
            s = self.state_manager.state
            all_data = s.get("all_data", [])
            defi_data = s.get("defi_data", [])
            combined = all_data + defi_data

            if not combined:
                log.debug(f"Monitor tick: no market data (all_data={len(all_data)}, defi={len(defi_data)})")
            elif positions:
                self._refresh_mins_next(all_data, time.time(), defi_data)
                updated_positions = self._update_earnings_db(positions, combined)
                alerts = self._check_alerts_db(positions, combined)
                alerts.extend(self._check_sl_tp_reviews(positions))
                s["alerts"] = alerts

                if not alerts and int(now) % 300 < 35:
                    for pos in positions:
                        log.info(f"  Monitor {pos['symbol']} ({pos.get('mode','spot_perp')}): "
                                 f"entry_fr={pos['entry_fr']*100:.4f}%, "
                                 f"last_fr={pos.get('last_fr_used',0)*100:.4f}%")

        # Save updated earnings to DB in background
        if positions and self._flask_app:
            updated = [p for p in positions if p.get("_earnings_updated")]
            if updated:
                threading.Thread(
                    target=self._batch_save_earnings,
                    args=(updated,),
                    daemon=True
                ).start()

        # Switch analysis removed from auto-loop — now on-demand via /api/positions/ai

        # Send Telegram alerts — route per-user so each user gets their own alerts.
        if alerts:
            log.info(f"Alerts detected: {[a['type'] for a in alerts]}")
            if self.email_notifier:
                try:
                    sent = self._dispatch_alerts_per_user(alerts)
                    log.info(f"WhatsApp: {sent}/{len(alerts)} alert(s) sent")
                    if sent > 0:
                        for a in alerts:
                            key = f"{a['type']}_{a['symbol']}_{a.get('exchange', '')}"
                            self._notified_alerts.add(key)
                        with self.state_manager.lock:
                            self.state_manager.state["alerts"] = []
                except Exception as e:
                    log.error(f"WhatsApp error: {e}")

        self._cleanup_events()

    def _dispatch_alerts_per_user(self, alerts: list) -> int:
        """Send each alert using the Telegram credentials of its owning user.

        Groups alerts by user_id, loads that user's config from DB, syncs the
        shared notifier's state for the duration of the send, and restores the
        previous state afterwards.
        """
        if not self.email_notifier:
            return 0

        by_user: dict = {}
        orphans: list = []
        for a in alerts:
            uid = a.get("user_id")
            if uid:
                by_user.setdefault(uid, []).append(a)
            else:
                orphans.append(a)

        sent_total = 0

        with self.state_manager.lock:
            s = self.state_manager.state
            prev_enabled = s.get("email_enabled", False)
            prev_chat_id = s.get("tg_chat_id", "")
            prev_token = s.get("tg_bot_token", "")

        try:
            for uid, user_alerts in by_user.items():
                cfg = self._load_user_telegram_config(uid)
                if not cfg or not cfg.get("email_enabled") or not cfg.get("tg_chat_id") or not cfg.get("tg_bot_token"):
                    log.info(
                        f"Telegram: skipping {len(user_alerts)} alert(s) for user {uid} "
                        f"(config incomplete/disabled)"
                    )
                    continue

                with self.state_manager.lock:
                    s = self.state_manager.state
                    s["email_enabled"] = True
                    s["tg_chat_id"] = cfg["tg_chat_id"]
                    s["tg_bot_token"] = cfg["tg_bot_token"]

                try:
                    sent_total += self.email_notifier.send_alerts(user_alerts)
                except Exception as e:
                    log.error(f"Telegram send failed for user {uid}: {e}")

            if orphans:
                with self.state_manager.lock:
                    s = self.state_manager.state
                    s["email_enabled"] = prev_enabled
                    s["tg_chat_id"] = prev_chat_id
                    s["tg_bot_token"] = prev_token
                try:
                    sent_total += self.email_notifier.send_alerts(orphans)
                except Exception as e:
                    log.error(f"Telegram send failed for orphan alerts: {e}")
        finally:
            with self.state_manager.lock:
                s = self.state_manager.state
                s["email_enabled"] = prev_enabled
                s["tg_chat_id"] = prev_chat_id
                s["tg_bot_token"] = prev_token
            if self.email_notifier:
                try:
                    self.email_notifier._sync_from_state()
                except Exception:
                    pass

        return sent_total

    def _load_user_telegram_config(self, user_id) -> dict:
        """Load a user's Telegram config from DB."""
        if not self._flask_app:
            return {}
        try:
            with self._flask_app.app_context():
                db_persist = self._get_db_persist()
                if not db_persist:
                    return {}
                us = db_persist.load_user_state(user_id)
                return {
                    "email_enabled": us.get("email_enabled", False),
                    "tg_chat_id": us.get("tg_chat_id", ""),
                    "tg_bot_token": us.get("tg_bot_token", ""),
                }
        except Exception as e:
            log.error(f"Failed to load Telegram config for user {user_id}: {e}")
            return {}

    def _broadcast_alerts_all_users(self, alerts: list) -> int:
        """Send alerts to every user who has Telegram notifications enabled.

        Used for market-wide alerts (EXCEPTIONAL_OPPORTUNITY, etc.) that are
        not tied to a specific user's position.
        """
        if not self.email_notifier or not alerts or not self._flask_app:
            return 0

        try:
            with self._flask_app.app_context():
                db_persist = self._get_db_persist()
                if not db_persist:
                    return 0
                user_configs = db_persist.get_all_users_telegram()
        except Exception as e:
            log.error(f"Broadcast: failed to load user configs: {e}")
            return 0

        if not user_configs:
            log.debug("Broadcast: no users with Telegram enabled, skipping")
            return 0

        with self.state_manager.lock:
            s = self.state_manager.state
            prev_enabled = s.get("email_enabled", False)
            prev_chat_id = s.get("tg_chat_id", "")
            prev_token = s.get("tg_bot_token", "")

        sent_total = 0
        try:
            for cfg in user_configs:
                with self.state_manager.lock:
                    s = self.state_manager.state
                    s["email_enabled"] = True
                    s["tg_chat_id"] = cfg["tg_chat_id"]
                    s["tg_bot_token"] = cfg["tg_bot_token"]
                try:
                    sent_total += self.email_notifier.send_alerts(alerts)
                except Exception as e:
                    log.error(f"Broadcast: send failed for user {cfg['user_id']}: {e}")
        finally:
            with self.state_manager.lock:
                s = self.state_manager.state
                s["email_enabled"] = prev_enabled
                s["tg_chat_id"] = prev_chat_id
                s["tg_bot_token"] = prev_token
            try:
                self.email_notifier._sync_from_state()
            except Exception:
                pass

        return sent_total

    def _batch_save_earnings(self, positions: list):
        """Save earnings for multiple positions to DB (background thread)."""
        if not self._flask_app:
            return
        try:
            with self._flask_app.app_context():
                db_persist = self._get_db_persist()
                if not db_persist:
                    return
                for pos in positions:
                    db_persist.update_position_earnings(
                        pos["db_id"],
                        earned=pos.get("earned_real", 0),
                        payment_count=pos.get("payment_count", 0),
                        avg_rate=pos.get("avg_rate", 0),
                        last_fr=pos.get("last_fr_used", 0),
                        payments=pos.get("payments", []),
                    )
                log.debug(f"Saved earnings for {len(positions)} positions to DB")
        except Exception as e:
            log.error(f"Batch earnings save failed: {e}")

    def _refresh_mins_next(self, all_data: list, now: float,
                            defi_data: list = None):
        """Recalculate mins_next from next_funding_ts. Zero API calls."""
        for d in all_data:
            nts = d.get("next_funding_ts", 0)
            if nts and nts > 0:
                d["mins_next"] = max(0, (nts / 1000 - now) / 60)
        if defi_data:
            for d in defi_data:
                nts = d.get("next_funding_ts", 0)
                if nts and nts > 0:
                    d["mins_next"] = max(0, (nts / 1000 - now) / 60)

    def run_switch_analysis(self, positions: list, alerts: list = None):
        """Run switch analysis for active positions against current opportunities.
        Called on-demand from /api/positions/ai endpoint.
        """
        from analysis.switch_analyzer import analyze_switch

        with self.state_manager.lock:
            s = self.state_manager.state
            opportunities = s.get("opportunities", []) + s.get("defi_opportunities", [])
            all_data = s.get("all_data", []) + s.get("defi_data", [])

        if not opportunities:
            return

        db_persist = None
        if self._flask_app:
            with self._flask_app.app_context():
                db_persist = self._get_db_persist()

                for pos in positions:
                    pos_id = str(pos.get("id", ""))
                    if not pos.get("earned_real"):
                        continue  # Skip positions with no earnings yet

                    result = analyze_switch(
                        pos, opportunities, all_data, db_persist
                    )
                    self._switch_results[pos_id] = result

                    # Generate alert if SWITCH recommended (only if alerts list provided)
                    if alerts is not None and result["recommendation"] == "SWITCH":
                        best = result.get("best_switch")
                        if best:
                            alert_key = f"SWITCH_{pos.get('symbol')}_{pos_id}"
                            if alert_key not in self._notified_alerts:
                                alerts.append({
                                    "type": "SWITCH_OPPORTUNITY",
                                    "severity": "INFO",
                                    "symbol": pos.get("symbol", ""),
                                    "exchange": pos.get("exchange", ""),
                                    "user_id": pos.get("user_id"),
                                    "message": (
                                        f"Alternativa superior: {best['symbol']} en {best['exchange']} "
                                        f"APR {best['apr']:.1f}%. "
                                        f"Beneficio neto: ${best['adjusted_switch_value']:.2f}. "
                                        f"Break-even: {best['break_even_h']:.0f}h"
                                    ),
                                })
                                self._notified_alerts.add(alert_key)

                # Drop switch results of closed positions to bound memory.
                active_ids = {str(p.get("id", "")) for p in positions}
                self._switch_results = {
                    k: v for k, v in self._switch_results.items()
                    if k in active_ids
                }

                log.info(f"Switch analysis: {len(self._switch_results)} positions analyzed")

    def _cleanup_events(self):
        """Remove old tracking entries to avoid memory growth."""
        if len(self._scanned_events) > 200:
            self._scanned_events.clear()
        # Alert keys are formed as TIPO_SYMBOL_EXCHANGE with no timestamp,
        # so we cap and reset when the set grows beyond typical volume.
        if len(self._notified_alerts) > 500:
            self._notified_alerts.clear()
        # _sl_tp_review_sent stores timestamps, purge entries older than 24h.
        cutoff = time.time() - 86400
        self._sl_tp_review_sent = {
            k: v for k, v in self._sl_tp_review_sent.items() if v > cutoff
        }

    # ── Scan (called only by triggers or manual) ──────────────

    def _run_scan(self):
        with self._scan_lock:
            self._run_scan_inner()

    def _run_scan_inner(self):
        log.info("Scan starting...")
        with self.state_manager.lock:
            self.state_manager.set("status", "Escaneando...")
            self.state_manager.set("scanning", True)
            min_volume = self.state_manager.get("min_volume", 1_000_000)

        # 1. Fetch rates from all exchanges via CCXT
        all_rates = self.exchange_manager.fetch_all_funding_rates()

        # Build flat all_data list
        all_data = []
        for exchange, rates in all_rates.items():
            for fr in rates:
                all_data.append(fr.to_dict())

        if not all_data:
            with self.state_manager.lock:
                self.state_manager.update(
                    status="Error: sin conexion",
                    last_error="Sin conexion a exchanges",
                    scanning=False,
                )
            return

        # 2. Try Coinglass for additional data
        cg_opps = []
        if self.coinglass and self.config.COINGLASS_API_KEY:
            try:
                cg_opps = self.coinglass.fetch_arbitrage_opportunities()
                log.info(f"Coinglass: {len(cg_opps)} opportunities")
            except Exception as e:
                log.warning(f"Coinglass fetch failed: {e}")

        # 3. Scan for spot-perp opportunities
        spot_perp_opps = self.arb_scanner.scan_spot_perp_opportunities(
            all_rates, min_volume=min_volume
        )

        # 4. Scan for cross-exchange opportunities
        cross_ex_opps = self.arb_scanner.scan_cross_exchange_opportunities(
            all_rates, min_volume=min_volume
        )

        # 5. Fetch DeFi rates (parallel with CEX scan results)
        defi_rates = {}
        defi_data = []
        defi_opportunities = []
        if self.defi_manager:
            try:
                defi_rates = self.defi_manager.fetch_all_funding_rates()
                for exchange, rates in defi_rates.items():
                    for fr in rates:
                        defi_data.append(fr.to_dict())

                # DeFi cross-exchange opportunities (same logic as CEX).
                # Both legs must meet the user's min_volume threshold.
                defi_cross = self.arb_scanner.scan_cross_exchange_opportunities(
                    defi_rates, min_volume=min_volume
                )
                for o in defi_cross:
                    d = o.to_dict()
                    d["_id"] = f"{d['symbol']}_{d['long_exchange']}_{d['short_exchange']}_defi_cross"
                    d["is_defi"] = True
                    defi_opportunities.append(d)

                # Also scan CEX vs DeFi cross-exchange. Both legs must meet
                # min_volume so CEX+DeFi mixed pairs are filtered symmetrically.
                combined_rates = {**all_rates, **defi_rates}
                cex_defi_cross = self.arb_scanner.scan_cross_exchange_opportunities(
                    combined_rates, min_volume=min_volume
                )
                for o in cex_defi_cross:
                    d = o.to_dict()
                    oid = f"{d['symbol']}_{d['long_exchange']}_{d['short_exchange']}_mixed_cross"
                    d["_id"] = oid
                    le = d.get("long_exchange", "")
                    se = d.get("short_exchange", "")
                    defi_exs = set(getattr(self.config, 'DEFI_EXCHANGES', []))
                    is_mixed = (le in defi_exs) != (se in defi_exs)
                    if is_mixed or (le in defi_exs and se in defi_exs):
                        d["is_defi"] = True
                        # Avoid duplicates
                        if not any(x["_id"] == oid for x in defi_opportunities):
                            defi_opportunities.append(d)

                defi_opportunities.sort(key=lambda o: o.get("score", 0), reverse=True)
                log.info(f"DeFi: {len(defi_data)} pairs, {len(defi_opportunities)} opportunities")
            except Exception as e:
                log.error(f"DeFi scan error: {e}")

        # 6. Build unified CEX opportunities list
        opportunities = []
        for o in spot_perp_opps:
            d = o.to_dict()
            d["_id"] = f"{d['symbol']}_{d['exchange']}_spot_perp"
            opportunities.append(d)
        for o in cross_ex_opps:
            d = o.to_dict()
            d["_id"] = f"{d['symbol']}_{d['long_exchange']}_{d['short_exchange']}_cross"
            opportunities.append(d)

        # Sort by score DESC
        opportunities.sort(key=lambda o: o.get("score", 0), reverse=True)

        # 6b. AI analysis of top opportunities
        try:
            opportunities = analyze_top_opportunities(opportunities, self.config)
        except Exception as e:
            log.warning(f"AI analysis skipped: {e}")

        # 6c. Detect exceptional opportunities (global score percentile)
        # One global query for the p95 threshold, then check each top opp.
        # Only alerts NEW exceptional opportunities (not in previous scan).
        exceptional_alerts = []
        current_exceptional_keys = set()
        if self._flask_app:
            try:
                with self._flask_app.app_context():
                    db_persist = self._get_db_persist()
                    global_p95 = db_persist.get_global_score_p95() if db_persist else None
                    if global_p95 is not None:
                        for opp in opportunities[:10]:
                            score = opp.get("score", 0)
                            exc = detect_exceptional(
                                current_score=score,
                                global_p95=global_p95,
                                current_apr=opp.get("apr", 0),
                            )
                            opp["is_exceptional"] = exc["is_exceptional"]
                            opp["exceptional_reasons"] = exc["reasons"]
                            if exc["is_exceptional"]:
                                sym = opp.get("symbol", "")
                                ex = opp.get("exchange", opp.get("short_exchange", ""))
                                ekey = f"{sym}_{ex}"
                                current_exceptional_keys.add(ekey)
                                if ekey not in self._prev_exceptional_keys:
                                    exceptional_alerts.append({
                                        "type": "EXCEPTIONAL_OPPORTUNITY",
                                        "severity": "INFO",
                                        "symbol": sym,
                                        "exchange": ex,
                                        "_score": score,
                                        "message": (
                                            f"Oportunidad excepcional: {sym} en {ex}. "
                                            f"Score {score}, APR {opp.get('apr', 0):.1f}%. "
                                            + " | ".join(exc["reasons"][:2])
                                        ),
                                    })
            except Exception as e:
                log.warning(f"Exceptional detection skipped: {e}")
        self._prev_exceptional_keys = current_exceptional_keys

        # 7. Update state (BEFORE snapshot storage — snapshots are slow and non-critical)
        status_parts = [f"{len(r)}{n[:2].upper()}" for n, r in all_rates.items() if r]
        status_str = "+".join(status_parts)
        defi_parts = [f"{len(r)}{n[:2].upper()}" for n, r in defi_rates.items() if r]
        if defi_parts:
            status_str += " | DeFi:" + "+".join(defi_parts)

        with self.state_manager.lock:
            s = self.state_manager.state
            s["all_data"] = all_data
            s["defi_data"] = defi_data
            s["defi_opportunities"] = defi_opportunities
            # Earnings are updated in _monitor_tick via _update_earnings_db
            s["opportunities"] = opportunities
            s["coinglass_data"] = cg_opps
            s["last_scan"] = time.time()
            s["scan_count"] = s.get("scan_count", 0) + 1
            s["last_scan_time"] = datetime.now().strftime("%H:%M:%S")
            n_sp = len(spot_perp_opps)
            n_cx = len(cross_ex_opps)
            n_defi = len(defi_opportunities)
            s["status"] = (
                f"OK — {status_str} | "
                f"{n_sp} spot-perp, {n_cx} cross-ex, {n_defi} defi | "
                f"{len(opportunities) + n_defi} total"
            )
            s["last_error"] = ""
            s["scanning"] = False

        self._last_scan_ts = time.time()
        log.info(
            f"Scan #{self.state_manager.get('scan_count')}: "
            f"{len(all_data)} pairs, {n_sp} spot-perp, {n_cx} cross-exchange"
        )

        # Send exceptional opportunity alerts via WhatsApp to ALL users with
        # notifications enabled (max 1 best alert per scan, broadcast to everyone).
        if exceptional_alerts and self.email_notifier:
            try:
                exceptional_alerts.sort(
                    key=lambda a: a.get("_score", 0), reverse=True
                )
                best = exceptional_alerts[:1]  # Only the single best new exceptional
                sent = self._broadcast_alerts_all_users(best)
                if sent > 0:
                    log.info(f"Exceptional alert broadcast: {best[0].get('symbol')} "
                             f"(score {best[0].get('_score')}) → {sent} user(s)")
            except Exception as e:
                log.warning(f"Exceptional alert broadcast failed: {e}")

        # 8. Store funding rate snapshots in background (non-blocking)
        snapshot_data = all_data + defi_data
        scan_count = self.state_manager.get("scan_count", 0)
        threading.Thread(
            target=self._store_rate_snapshots,
            args=(snapshot_data,),
            daemon=True,
        ).start()

        # 9. Store score snapshots (rolling window, non-blocking)
        all_opps = opportunities + defi_opportunities
        if all_opps:
            threading.Thread(
                target=self._store_score_snapshots,
                args=(all_opps, scan_count),
                daemon=True,
            ).start()

    # ── Rate Snapshot Storage (data accumulation for ML) ─────

    def _store_rate_snapshots(self, all_data: list):
        """Store funding rate snapshots to PostgreSQL for future analysis.

        Runs in a background thread. Uses bulk INSERT with ON CONFLICT
        to avoid individual SELECT queries per row.
        """
        if not self._flask_app:
            return

        try:
            with self._flask_app.app_context():
                from core.database import db
                from core.db_models import FundingRateSnapshot
                from datetime import datetime, timezone

                now = datetime.now(timezone.utc)

                # Build batch of rows to insert.  Track per-exchange skip
                # counts so we can spot adapters returning next_funding_ts=0.
                rows = []
                skipped_by_exchange: dict = {}
                for d in all_data:
                    funding_ts = d.get("next_funding_ts", 0)
                    if not funding_ts:
                        ex = d.get("exchange", "?")
                        skipped_by_exchange[ex] = skipped_by_exchange.get(ex, 0) + 1
                        continue
                    rows.append({
                        "symbol": d.get("symbol", ""),
                        "exchange": d.get("exchange", ""),
                        "rate": d.get("fr", 0),
                        "volume_24h": d.get("vol24h", 0),
                        "mark_price": d.get("price", 0),
                        "interval_hours": int(d.get("ih", 8)),
                        "funding_ts": int(funding_ts),
                        "captured_at": now,
                    })

                if skipped_by_exchange:
                    log.warning(
                        f"Snapshot filter: dropped {sum(skipped_by_exchange.values())} "
                        f"rows with funding_ts<=0 — {skipped_by_exchange}"
                    )

                if not rows:
                    return

                # Bulk insert, skip duplicates via ON CONFLICT DO NOTHING
                from sqlalchemy.dialects.postgresql import insert as pg_insert
                stmt = pg_insert(FundingRateSnapshot.__table__).values(rows)
                stmt = stmt.on_conflict_do_nothing(
                    constraint="uq_snapshot_symbol_exchange_ts"
                )
                result = db.session.execute(stmt)
                db.session.commit()
                inserted = result.rowcount if result.rowcount else 0

                # Cleanup old snapshots (>90 days) — run occasionally
                scan_count = self.state_manager.get("scan_count", 0)
                if scan_count % 50 == 0:
                    from datetime import timedelta
                    cutoff = now - timedelta(days=90)
                    deleted = FundingRateSnapshot.query.filter(
                        FundingRateSnapshot.captured_at < cutoff
                    ).delete()
                    db.session.commit()
                    if deleted:
                        log.info(f"Cleaned {deleted} old rate snapshots")

                # Distinguish "constraint dedup'd as expected" from "all
                # timestamps are stale — investigate".  The unique constraint
                # (symbol, exchange, funding_ts) naturally dedupes within a
                # single funding window, so Stored 0 is the common case and
                # only a concern when every funding_ts is in the past.
                now_ms = int(time.time() * 1000)
                min_ts = min(r["funding_ts"] for r in rows)
                max_ts = max(r["funding_ts"] for r in rows)
                if inserted == 0 and max_ts <= now_ms:
                    log.warning(
                        f"Snapshots 0/{len(rows)} inserted — all next_funding_ts "
                        f"are in the past (min={min_ts}, max={max_ts}). "
                        f"Exchanges may be returning stale data."
                    )
                else:
                    log.info(
                        f"Stored {inserted}/{len(rows)} rate snapshots "
                        f"(funding_ts range: {min_ts}..{max_ts})"
                    )

        except Exception as e:
            log.exception(f"Rate snapshot storage failed (non-critical): {e}")

    # ── Score Snapshot Storage (rolling window) ────────────────

    MAX_SCORE_HISTORY = 30  # Max entries per symbol+exchange pair

    def _store_score_snapshots(self, opportunities: list, scan_count: int):
        """Store score snapshots with rolling window cleanup.

        Keeps max 30 entries per symbol+exchange pair (~10 days of data).
        Runs in background thread.
        """
        if not self._flask_app:
            return

        try:
            with self._flask_app.app_context():
                from core.database import db
                from core.db_models import ScoreSnapshot
                from datetime import datetime, timezone

                now = datetime.now(timezone.utc)

                rows = []
                for opp in opportunities:
                    indicators = opp.get("indicators", {})
                    rows.append(ScoreSnapshot(
                        symbol=opp.get("symbol", ""),
                        exchange=opp.get("exchange", opp.get("short_exchange", "")),
                        mode=opp.get("mode", "spot_perp"),
                        score=opp.get("score", 0),
                        funding_rate=opp.get("funding_rate", 0) or opp.get("rate_differential", 0),
                        apr=opp.get("apr", 0),
                        volume_24h=opp.get("volume_24h", 0) or 0,
                        z_score=indicators.get("z_score", 0) if isinstance(indicators, dict) else 0,
                        momentum_signal=indicators.get("momentum_signal", "flat") if isinstance(indicators, dict) else "flat",
                        scan_number=scan_count,
                        captured_at=now,
                    ))

                if rows:
                    db.session.add_all(rows)
                    db.session.commit()

                # Rolling window cleanup: every 10 scans, trim old entries
                if scan_count % 10 == 0:
                    self._trim_score_snapshots(db, ScoreSnapshot)

                log.info(f"Stored {len(rows)} score snapshots")

        except Exception as e:
            log.warning(f"Score snapshot storage failed (non-critical): {e}")

    def _trim_score_snapshots(self, db, ScoreSnapshot):
        """Keep only the most recent MAX_SCORE_HISTORY entries per pair."""
        try:
            from sqlalchemy import func, text

            # Find pairs that exceed the limit
            pairs = db.session.query(
                ScoreSnapshot.symbol,
                ScoreSnapshot.exchange,
                func.count(ScoreSnapshot.id).label("cnt")
            ).group_by(
                ScoreSnapshot.symbol, ScoreSnapshot.exchange
            ).having(
                func.count(ScoreSnapshot.id) > self.MAX_SCORE_HISTORY
            ).all()

            total_deleted = 0
            for symbol, exchange, cnt in pairs:
                excess = cnt - self.MAX_SCORE_HISTORY
                # Delete oldest entries for this pair
                oldest_ids = db.session.query(ScoreSnapshot.id).filter(
                    ScoreSnapshot.symbol == symbol,
                    ScoreSnapshot.exchange == exchange,
                ).order_by(
                    ScoreSnapshot.captured_at.asc()
                ).limit(excess).subquery()

                deleted = ScoreSnapshot.query.filter(
                    ScoreSnapshot.id.in_(oldest_ids)
                ).delete(synchronize_session=False)
                total_deleted += deleted

            if total_deleted:
                db.session.commit()
                log.info(f"Trimmed {total_deleted} old score snapshots")

        except Exception as e:
            log.warning(f"Score snapshot trim failed: {e}")

    # ── Data helpers ──────────────────────────────────────────

    def _find_data(self, pos: dict, all_data: list,
                    defi_data: list = None) -> dict:
        """Find current market data for a position (searches CEX + DeFi)."""
        sym = pos["symbol"]
        ex = pos["exchange"]
        result = next(
            (d for d in all_data if d["symbol"] == sym and d["exchange"] == ex),
            None,
        )
        if result is None and defi_data:
            result = next(
                (d for d in defi_data
                 if d["symbol"] == sym and d["exchange"] == ex),
                None,
            )
        return result

    # ── Earnings ──────────────────────────────────────────────

    def _update_earnings_db(self, positions: list, all_data: list) -> list:
        """Accumulate real earnings for DB-loaded positions.

        Returns list of positions that had earnings updated.
        """
        now = time.time()
        updated = []
        for pos in positions:
            old_earned = pos.get("earned_real", 0)
            mode = pos.get("mode", "spot_perp")

            if mode == "spot_perp":
                self._update_spot_perp_earnings(pos, all_data, now)
            else:
                self._update_cross_exchange_earnings(pos, all_data, now)

            if pos.get("earned_real", 0) != old_earned:
                pos["_earnings_updated"] = True
                updated.append(pos)
        return updated

    def _update_earnings(self, state: dict, all_data: list) -> None:
        """Accumulate real earnings based on funding payment timestamps."""
        now = time.time()
        for pos in state.get("positions", []):
            mode = pos.get("mode", "spot_perp")

            if mode == "spot_perp":
                self._update_spot_perp_earnings(pos, all_data, now)
            else:
                self._update_cross_exchange_earnings(pos, all_data, now)

    def _update_spot_perp_earnings(self, pos: dict, all_data: list, now: float):
        """Spot-perp earnings: detect funding payments using next_funding_ts."""
        cur = self._find_data(pos, all_data)
        if not cur:
            return

        # Use current market ih (not position's stored ih) to handle interval changes
        ih = cur.get("ih", pos.get("ih", 8))
        if ih != pos.get("ih", 8):
            log.info(f"  {pos['symbol']}: interval changed {pos.get('ih')}h -> {ih}h, updating")
            pos["ih"] = ih
        interval_secs = ih * 3600
        cfr = cur["fr"]
        nts = cur.get("next_funding_ts", 0)

        last_payment_ts = self._calc_last_payment_ts(nts, interval_secs, now)
        if last_payment_ts <= 0:
            self._update_earnings_elapsed(pos, cfr, ih, now)
            return

        last_up = pos.get("last_earn_update", pos["entry_time"] / 1000)
        payments = self._count_payments_since(last_up, last_payment_ts, interval_secs)
        if payments < 1:
            return

        # Resolve the actual settled rate from history to avoid the latency
        # divergence between the scan time and the settlement time.
        settled_rate = self._resolve_settlement_rate(
            pos["exchange"], pos["symbol"], last_payment_ts, fallback=cfr
        )

        # Short-perp side: receives when settled_rate > 0, PAYS when < 0.
        # Always apply the sign so negative rates are recorded as losses.
        exposure = pos.get("exposure", pos["capital_used"] / 2)
        earn_per_payment = exposure * settled_rate

        self._record_earnings(
            pos, earn_per_payment * payments, settled_rate, now, payments,
            payment_ts=last_payment_ts,
        )

    def _update_cross_exchange_earnings(self, pos: dict, all_data: list, now: float):
        """Cross-exchange earnings: track each side's payments independently.

        Each exchange pays at its own schedule (potentially different intervals
        and different times).  We detect payments on each side independently
        and record the net earnings per side per payment event.
        """
        long_ex = pos.get("long_exchange", "")
        short_ex = pos.get("short_exchange", pos.get("exchange", ""))

        long_data = next(
            (d for d in all_data
             if d["symbol"] == pos["symbol"] and d["exchange"] == long_ex),
            None,
        )
        short_data = next(
            (d for d in all_data
             if d["symbol"] == pos["symbol"] and d["exchange"] == short_ex),
            None,
        )
        if not long_data or not short_data:
            return

        exposure = pos.get("exposure", pos["capital_used"] / 2)
        long_ih = long_data.get("ih", 8)
        short_ih = short_data.get("ih", 8)
        long_interval = long_ih * 3600
        short_interval = short_ih * 3600

        long_nts = long_data.get("next_funding_ts", 0)
        short_nts = short_data.get("next_funding_ts", 0)

        # Track last update per side (so each side counts independently)
        last_up = pos.get("last_earn_update", pos["entry_time"] / 1000)
        long_last = pos.get("_long_last_update", last_up)
        short_last = pos.get("_short_last_update", last_up)

        total_earned = 0
        any_payment = False
        last_settlement_ts = 0
        short_settled = short_data["fr"]  # fallback if no payment on this side
        long_settled = long_data["fr"]
        short_payments = 0
        long_payments = 0

        # --- SHORT side payments ---
        short_fr = short_data["fr"]
        short_last_pay = self._calc_last_payment_ts(short_nts, short_interval, now)
        if short_last_pay > 0:
            short_payments = self._count_payments_since(short_last, short_last_pay, short_interval)
        else:
            short_payments = int((now - short_last) / short_interval)

        if short_payments >= 1:
            # Short side: we are SHORT, so we RECEIVE when rate > 0
            if short_last_pay > 0:
                short_settled = self._resolve_settlement_rate(
                    short_ex, pos["symbol"], short_last_pay, fallback=short_fr
                )
                last_settlement_ts = max(last_settlement_ts, short_last_pay)
            short_earn = exposure * short_settled * short_payments
            total_earned += short_earn
            pos["_short_last_update"] = now
            any_payment = True
            log.debug(f"  Cross {pos['symbol']} SHORT@{short_ex}: {short_payments} pays, "
                       f"fr={short_settled*100:.4f}%, earn=${short_earn:.4f}")

        # --- LONG side payments ---
        long_fr = long_data["fr"]
        long_last_pay = self._calc_last_payment_ts(long_nts, long_interval, now)
        if long_last_pay > 0:
            long_payments = self._count_payments_since(long_last, long_last_pay, long_interval)
        else:
            long_payments = int((now - long_last) / long_interval)

        if long_payments >= 1:
            # Long side: we are LONG, so we PAY when rate > 0
            if long_last_pay > 0:
                long_settled = self._resolve_settlement_rate(
                    long_ex, pos["symbol"], long_last_pay, fallback=long_fr
                )
                last_settlement_ts = max(last_settlement_ts, long_last_pay)
            long_earn = -(exposure * long_settled * long_payments)
            total_earned += long_earn
            pos["_long_last_update"] = now
            any_payment = True
            log.debug(f"  Cross {pos['symbol']} LONG@{long_ex}: {long_payments} pays, "
                       f"fr={long_settled*100:.4f}%, earn=${long_earn:.4f}")

        if not any_payment:
            return

        # Record with the settled differential as the rate
        differential = short_settled - long_settled
        total_payments = max(short_payments, long_payments)
        self._record_earnings(
            pos, total_earned, differential, now, total_payments,
            payment_ts=last_settlement_ts or None,
        )

    def _calc_last_payment_ts(self, next_funding_ts: int, interval_secs: int,
                              now: float) -> float:
        if not next_funding_ts or next_funding_ts <= 0:
            return 0

        next_ts_sec = next_funding_ts / 1000

        if next_ts_sec > now:
            last_pay = next_ts_sec - interval_secs
        else:
            ts = next_ts_sec
            while ts + interval_secs <= now:
                ts += interval_secs
            last_pay = ts

        # Safety: last payment can never be in the future
        if last_pay > now:
            return 0
        return last_pay

    def _count_payments_since(self, last_update: float, last_payment_ts: float,
                              interval_secs: int) -> int:
        if last_payment_ts <= last_update:
            return 0

        count = 0
        ts = last_payment_ts
        while ts > last_update:
            count += 1
            ts -= interval_secs

        return count

    def _resolve_settlement_rate(self, exchange: str, symbol: str,
                                  settlement_ts: float,
                                  fallback: float) -> float:
        """Return the rate the exchange actually applied at settlement_ts.

        Uses the exchange's funding history (CCXT) for CEX, and snapshots
        from funding_rate_snapshots for DeFi adapters. Falls back to the
        provided rate (typically the current scan's cfr) if no historic
        match is found within the tolerance window.
        """
        try:
            if (self.defi_manager
                    and self.defi_manager.is_defi_exchange(exchange)):
                rate = self.defi_manager.fetch_settlement_rate(
                    symbol, exchange, settlement_ts,
                    tolerance_secs=SETTLEMENT_RATE_TOLERANCE_SECS,
                )
            else:
                rate = self.exchange_manager.fetch_settlement_rate(
                    symbol, exchange, settlement_ts,
                    tolerance_secs=SETTLEMENT_RATE_TOLERANCE_SECS,
                )
        except Exception as e:
            log.warning(f"Settlement rate lookup failed {symbol}@{exchange}: {e}")
            rate = None

        if rate is None:
            log.warning(
                f"Settlement rate not found {symbol}@{exchange} "
                f"ts={int(settlement_ts)}; using fallback={fallback*100:.4f}%"
            )
            return fallback

        if abs(rate - fallback) > 1e-9:
            log.info(
                f"Settled {symbol}@{exchange} ts={int(settlement_ts)}: "
                f"cfr={fallback*100:.6f}% historic={rate*100:.6f}%"
            )
        return rate

    def _update_earnings_elapsed(self, pos: dict, cfr: float, ih: int, now: float):
        """Fallback: elapsed-time earnings when no timestamp available."""
        last_up = pos.get("last_earn_update", pos["entry_time"] / 1000)
        elapsed_h = (now - last_up) / 3600
        full_ivs = int(elapsed_h / ih)
        if full_ivs < 1:
            return

        # Short-perp side: receives when cfr > 0, PAYS when cfr < 0.
        # Always apply the sign so negative rates are recorded as losses.
        exposure = pos.get("exposure", pos["capital_used"] / 2)
        earn_per_iv = exposure * cfr

        self._record_earnings(pos, earn_per_iv * full_ivs, cfr, now, full_ivs)

    def _record_earnings(self, pos: dict, earned_now: float, rate: float,
                         now: float, full_ivs: int,
                         payment_ts: float = None):
        """Record earnings for a position (positive and negative).

        payment_ts (optional, seconds): timestamp of the actual settlement
        to stamp on the payment record. Falls back to `now` when not given
        (used by the elapsed-time fallback path).
        """
        if "payments" not in pos:
            pos["payments"] = []
        record_ts = int(payment_ts) if payment_ts else int(now)
        pos["payments"].append({
            "ts": record_ts,
            "rate": rate,
            "earned": earned_now,
            "cumulative": pos.get("earned_real", 0) + earned_now,
        })

        pos["earned_real"] = pos.get("earned_real", 0) + earned_now
        pos["last_earn_update"] = now
        pos["last_fr_used"] = rate
        pos["payment_count"] = len(pos["payments"])
        pos["avg_rate"] = sum(p["rate"] for p in pos["payments"]) / len(pos["payments"])

        if earned_now > 0:
            log.info(f"  +${earned_now:.4f} {pos['symbol']} ({full_ivs}ivs @ {rate*100:.4f}%)")
        elif earned_now < 0:
            log.info(f"  -${abs(earned_now):.4f} {pos['symbol']} ({full_ivs}ivs @ {rate*100:.4f}%) [pago funding]")

    # ── Alerts ────────────────────────────────────────────────

    def _check_alerts_db(self, positions: list, all_data: list) -> list:
        """Check DB-loaded positions for alerts. Same logic as _check_alerts."""
        alerts = []
        alert_mins = self.state_manager.get("alert_minutes_before", 5)
        active_keys = set()

        for i, pos in enumerate(positions):
            is_cross = pos.get("mode") == "cross_exchange"

            if is_cross:
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
                if not long_d or not short_d:
                    continue
                cfr = short_d["fr"] - long_d["fr"]
                mins_candidates = [d for d in (long_d, short_d) if d.get("mins_next", -1) >= 0]
                mins_next = min((d["mins_next"] for d in mins_candidates), default=-1)
                display_ex = f"{short_ex}/{long_ex}"
            else:
                cur = self._find_data(pos, all_data)
                if not cur:
                    continue
                cfr = cur["fr"]
                mins_next = cur.get("mins_next", -1)
                display_ex = pos["exchange"]

            sym = pos["symbol"]

            if (pos["entry_fr"] > 0 and cfr < 0) or (pos["entry_fr"] < 0 and cfr > 0):
                key = f"RATE_REVERSAL_{sym}_{display_ex}"
                active_keys.add(key)
                if key not in self._notified_alerts:
                    alerts.append({
                        "type": "RATE_REVERSAL", "severity": "CRITICAL",
                        "position_idx": i, "symbol": sym, "exchange": display_ex,
                        "user_id": pos.get("user_id"),
                        "message": f"Funding rate cambio de signo: {pos['entry_fr']*100:.4f}% -> {cfr*100:.4f}%",
                    })
            elif abs(cfr) < abs(pos["entry_fr"]) * 0.25:
                key = f"RATE_DROP_{sym}_{display_ex}"
                active_keys.add(key)
                if key not in self._notified_alerts:
                    alerts.append({
                        "type": "RATE_DROP", "severity": "WARNING",
                        "position_idx": i, "symbol": sym, "exchange": display_ex,
                        "user_id": pos.get("user_id"),
                        "message": f"Rate cayo >75%: {pos['entry_fr']*100:.4f}% -> {cfr*100:.4f}%",
                    })

            if 0 < mins_next <= alert_mins:
                if cfr <= 0 and pos["entry_fr"] > 0:
                    key = f"PRE_PAYMENT_UNFAVORABLE_{sym}_{display_ex}"
                    active_keys.add(key)
                    if key not in self._notified_alerts:
                        alerts.append({
                            "type": "PRE_PAYMENT_UNFAVORABLE", "severity": "WARNING",
                            "position_idx": i, "symbol": sym, "exchange": display_ex,
                            "user_id": pos.get("user_id"),
                            "message": f"Proximo pago en {mins_next:.0f}min — tasa desfavorable: {cfr*100:.4f}%",
                        })

        stale = self._notified_alerts - active_keys
        if stale:
            log.info(f"Alert conditions cleared: {stale}")
            self._notified_alerts -= stale

        return alerts

    def _check_sl_tp_reviews(self, positions: list) -> list:
        """Generate SL_TP_REVIEW alerts for positions open > 144h, every 144h."""
        SL_TP_REVIEW_INTERVAL = 144 * 3600  # 144 hours in seconds
        alerts = []
        now = time.time()

        for pos in positions:
            pos_id = str(pos.get("id", ""))
            entry_time_s = (pos.get("entry_time", 0) or 0) / 1000
            if entry_time_s <= 0:
                continue

            elapsed_s = now - entry_time_s
            if elapsed_s < SL_TP_REVIEW_INTERVAL:
                continue

            last_sent = self._sl_tp_review_sent.get(pos_id, 0)
            if now - last_sent < SL_TP_REVIEW_INTERVAL:
                continue

            elapsed_h = elapsed_s / 3600
            sym = pos.get("symbol", "???")
            ex = pos.get("exchange", "")
            if pos.get("mode") == "cross_exchange":
                ex = f"{pos.get('short_exchange', '')}/{pos.get('long_exchange', '')}"
            earned = pos.get("earned_real", 0) or 0

            alerts.append({
                "type": "SL_TP_REVIEW",
                "severity": "WARNING",
                "symbol": sym,
                "exchange": ex,
                "user_id": pos.get("user_id"),
                "message": (
                    f"Posicion abierta {elapsed_h:.0f}h ({elapsed_h/24:.0f} dias) "
                    f"— revisar SL/TP. Ganancia acum: ${earned:.2f}"
                ),
            })
            self._sl_tp_review_sent[pos_id] = now

        # Cleanup: remove entries for positions no longer active
        active_ids = {str(p.get("id", "")) for p in positions}
        stale = [k for k in self._sl_tp_review_sent if k not in active_ids]
        for k in stale:
            del self._sl_tp_review_sent[k]

        return alerts

    def _check_alerts(self, state: dict, all_data: list) -> list:
        """Check positions for alerts: rate reversal, rate drop, pre-payment.

        Uses _notified_alerts to ensure each alert is only generated ONCE.
        When the condition clears (no longer triggers), its key is removed
        so it can fire again if the condition reappears.
        """
        alerts = []
        alert_mins = state.get("alert_minutes_before", 5)
        # Track which alert keys are active THIS tick — used to clear stale ones
        active_keys = set()

        for i, pos in enumerate(state.get("positions", [])):
            is_cross = pos.get("mode") == "cross_exchange"

            if is_cross:
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
                if not long_d or not short_d:
                    continue
                cfr = short_d["fr"] - long_d["fr"]
                mins_candidates = [d for d in (long_d, short_d) if d.get("mins_next", -1) >= 0]
                mins_next = min((d["mins_next"] for d in mins_candidates), default=-1)
                display_ex = f"{short_ex}/{long_ex}"
            else:
                cur = self._find_data(pos, all_data)
                if not cur:
                    continue
                cfr = cur["fr"]
                mins_next = cur.get("mins_next", -1)
                display_ex = pos["exchange"]

            sym = pos["symbol"]

            # Rate reversal (critical)
            if (pos["entry_fr"] > 0 and cfr < 0) or (pos["entry_fr"] < 0 and cfr > 0):
                key = f"RATE_REVERSAL_{sym}_{display_ex}"
                active_keys.add(key)
                if key not in self._notified_alerts:
                    alerts.append({
                        "type": "RATE_REVERSAL",
                        "severity": "CRITICAL",
                        "position_idx": i,
                        "symbol": sym,
                        "exchange": display_ex,
                        "message": f"Funding rate cambio de signo: {pos['entry_fr']*100:.4f}% -> {cfr*100:.4f}%",
                    })

            # Rate dropped >75% (warning)
            elif abs(cfr) < abs(pos["entry_fr"]) * 0.25:
                key = f"RATE_DROP_{sym}_{display_ex}"
                active_keys.add(key)
                if key not in self._notified_alerts:
                    alerts.append({
                        "type": "RATE_DROP",
                        "severity": "WARNING",
                        "position_idx": i,
                        "symbol": sym,
                        "exchange": display_ex,
                        "message": f"Rate cayo >75%: {pos['entry_fr']*100:.4f}% -> {cfr*100:.4f}%",
                    })

            # Pre-payment alert (N min before next funding)
            if 0 < mins_next <= alert_mins:
                if cfr <= 0 and pos["entry_fr"] > 0:
                    key = f"PRE_PAYMENT_UNFAVORABLE_{sym}_{display_ex}"
                    active_keys.add(key)
                    if key not in self._notified_alerts:
                        alerts.append({
                            "type": "PRE_PAYMENT_UNFAVORABLE",
                            "severity": "WARNING",
                            "position_idx": i,
                            "symbol": sym,
                            "exchange": display_ex,
                            "message": f"Proximo pago en {mins_next:.0f}min — tasa desfavorable: {cfr*100:.4f}%",
                        })

        # Clear notified keys whose condition is no longer active
        # This allows the alert to fire again if the condition reappears
        stale = self._notified_alerts - active_keys
        if stale:
            log.info(f"Alert conditions cleared: {stale}")
            self._notified_alerts -= stale

        return alerts
