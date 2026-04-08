"""Background monitor — v8.3 minimal API calls.

Scans happen in 4 moments:
1. ~10 min before a funding payment (verify rate is still good)
2. At payment time (get actual rate for earnings)
3. 5 min after payment (refresh to get NEW next_funding_ts)
4. Manual scan (user clicks button)

NO periodic scan loop. Monitor runs every 30s checking timestamps
locally (zero API calls) and triggers scans only when needed.
"""
import time
import logging
import threading
from datetime import datetime
from analysis.ai_analyzer import analyze_top_opportunities

log = logging.getLogger("bot")

# Pre-payment scan: minutes before payment to trigger verification scan
PRE_PAYMENT_SCAN_MINS = 10
# Payment scan: minutes after payment to fetch actual rate used
POST_PAYMENT_SCAN_MINS = 1
# Refresh scan: seconds after payment scan to refresh next_funding_ts
REFRESH_AFTER_PAYMENT_SECS = 5 * 60  # 5 minutes


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
        self._pending_refreshes = {}
        self._sl_tp_review_sent = {}  # {position_id: last_sent_timestamp}
        self._db_persist = None  # Lazy-init DBPersistence

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

        # Momento 3: Check pending refresh scans (5 min after payment)
        for rkey, trigger_at in list(self._pending_refreshes.items()):
            if now >= trigger_at:
                scan_reason = f"Refresh post-pago ({rkey})"
                del self._pending_refreshes[rkey]
                break

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
            if not scan_reason:
                triggered_positions = []
                combined = all_data + defi_data
                seen_pairs = set()  # (symbol, exchange) already checked

                for pos in positions:
                    sym = pos["symbol"]
                    ex = pos["exchange"]
                    pair_key = f"{sym}_{ex}"

                    # Skip duplicate symbol/exchange pairs for trigger checking
                    if pair_key in seen_pairs:
                        continue
                    seen_pairs.add(pair_key)

                    cur = self._find_data(pos, all_data, defi_data)

                    mn = -1
                    nts = 0
                    if pos.get("mode") == "cross_exchange":
                        long_ex = pos.get("long_exchange", "")
                        short_ex = pos.get("short_exchange", pos.get("exchange", ""))
                        long_d = next((d for d in combined if d["symbol"] == sym and d["exchange"] == long_ex), None)
                        short_d = next((d for d in combined if d["symbol"] == sym and d["exchange"] == short_ex), None)
                        candidates = [d for d in (long_d, short_d) if d and d.get("mins_next", -1) >= 0]
                        if candidates:
                            nearest = min(candidates, key=lambda d: d["mins_next"])
                            mn = nearest["mins_next"]
                            nts = nearest.get("next_funding_ts", 0)
                    elif cur:
                        mn = cur.get("mins_next", -1)
                        nts = cur.get("next_funding_ts", 0)

                    if mn < 0:
                        continue

                    event_key = f"{pair_key}_{nts}"

                    pre_key = f"pre_{event_key}"
                    if PRE_PAYMENT_SCAN_MINS >= mn > POST_PAYMENT_SCAN_MINS:
                        if pre_key not in self._scanned_events:
                            triggered_positions.append((
                                mn, f"Pre-pago {sym}@{ex} en {mn:.0f}min",
                                [pre_key], []
                            ))

                    post_key = f"post_{event_key}"
                    if mn <= POST_PAYMENT_SCAN_MINS:
                        if post_key not in self._scanned_events:
                            triggered_positions.append((
                                mn, f"Pago {sym}@{ex}",
                                [post_key],
                                [(pair_key, nts)]
                            ))

                if triggered_positions:
                    triggered_positions.sort(key=lambda t: t[0])
                    groups = []
                    current_group = [triggered_positions[0]]
                    for tp in triggered_positions[1:]:
                        if tp[0] - current_group[0][0] <= 5:
                            current_group.append(tp)
                        else:
                            groups.append(current_group)
                            current_group = [tp]
                    groups.append(current_group)

                    group = groups[0]
                    labels = [tp[1] for tp in group]
                    scan_reason = " + ".join(labels)

                    for tp in group:
                        for ek in tp[2]:
                            self._scanned_events.add(ek)
                        for (sk, nts_val) in tp[3]:
                            refresh_key = f"{sk}_{nts_val}"
                            self._pending_refreshes[refresh_key] = now + REFRESH_AFTER_PAYMENT_SECS
                            log.info(f"Refresh programado en 5min para {sk}")

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

        # Send WhatsApp alerts
        if alerts:
            log.info(f"Alerts detected: {[a['type'] for a in alerts]}")
            if self.email_notifier:
                try:
                    sent = self.email_notifier.send_alerts(alerts)
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

    def _cleanup_events(self):
        """Remove old scanned events to avoid memory growth."""
        if len(self._scanned_events) > 200:
            self._scanned_events.clear()

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

                # DeFi cross-exchange opportunities (same logic as CEX)
                defi_cross = self.arb_scanner.scan_cross_exchange_opportunities(
                    defi_rates, min_volume=0  # DeFi often has low/no volume data
                )
                for o in defi_cross:
                    d = o.to_dict()
                    d["_id"] = f"{d['symbol']}_{d['long_exchange']}_{d['short_exchange']}_defi_cross"
                    d["is_defi"] = True
                    defi_opportunities.append(d)

                # Also scan CEX vs DeFi cross-exchange
                combined_rates = {**all_rates, **defi_rates}
                cex_defi_cross = self.arb_scanner.scan_cross_exchange_opportunities(
                    combined_rates, min_volume=0
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

                # Build batch of rows to insert
                rows = []
                for d in all_data:
                    funding_ts = d.get("next_funding_ts", 0)
                    if not funding_ts:
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

                log.info(f"Stored {inserted} rate snapshots (batch of {len(rows)})")

        except Exception as e:
            log.warning(f"Rate snapshot storage failed (non-critical): {e}")

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

        if cfr > 0:
            exposure = pos.get("exposure", pos["capital_used"] / 2)
            earn_per_payment = exposure * cfr
        else:
            earn_per_payment = 0

        self._record_earnings(pos, earn_per_payment * payments, cfr, now, payments)

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

        # --- SHORT side payments ---
        short_fr = short_data["fr"]
        short_last_pay = self._calc_last_payment_ts(short_nts, short_interval, now)
        if short_last_pay > 0:
            short_payments = self._count_payments_since(short_last, short_last_pay, short_interval)
        else:
            short_payments = int((now - short_last) / short_interval)

        if short_payments >= 1:
            # Short side: we are SHORT, so we RECEIVE when rate > 0
            short_earn = exposure * short_fr * short_payments
            total_earned += short_earn
            pos["_short_last_update"] = now
            any_payment = True
            log.debug(f"  Cross {pos['symbol']} SHORT@{short_ex}: {short_payments} pays, "
                       f"fr={short_fr*100:.4f}%, earn=${short_earn:.4f}")

        # --- LONG side payments ---
        long_fr = long_data["fr"]
        long_last_pay = self._calc_last_payment_ts(long_nts, long_interval, now)
        if long_last_pay > 0:
            long_payments = self._count_payments_since(long_last, long_last_pay, long_interval)
        else:
            long_payments = int((now - long_last) / long_interval)

        if long_payments >= 1:
            # Long side: we are LONG, so we PAY when rate > 0
            long_earn = -(exposure * long_fr * long_payments)
            total_earned += long_earn
            pos["_long_last_update"] = now
            any_payment = True
            log.debug(f"  Cross {pos['symbol']} LONG@{long_ex}: {long_payments} pays, "
                       f"fr={long_fr*100:.4f}%, earn=${long_earn:.4f}")

        if not any_payment:
            return

        # Record with the current differential as the rate
        differential = short_fr - long_fr
        total_payments = max(short_payments, long_payments)
        self._record_earnings(pos, total_earned, differential, now, total_payments)

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

    def _update_earnings_elapsed(self, pos: dict, cfr: float, ih: int, now: float):
        """Fallback: elapsed-time earnings when no timestamp available."""
        last_up = pos.get("last_earn_update", pos["entry_time"] / 1000)
        elapsed_h = (now - last_up) / 3600
        full_ivs = int(elapsed_h / ih)
        if full_ivs < 1:
            return

        if cfr > 0:
            exposure = pos.get("exposure", pos["capital_used"] / 2)
            earn_per_iv = exposure * cfr
        else:
            earn_per_iv = 0

        self._record_earnings(pos, earn_per_iv * full_ivs, cfr, now, full_ivs)

    def _record_earnings(self, pos: dict, earned_now: float, rate: float,
                         now: float, full_ivs: int):
        """Record earnings for a position (positive and negative)."""
        if "payments" not in pos:
            pos["payments"] = []
        pos["payments"].append({
            "ts": int(now),
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
