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
        self._started = False
        self._last_scan_ts = 0
        self._scan_lock = threading.Lock()
        # Track which payment windows we already scanned for
        # Key: "{symbol}_{exchange}_{payment_ts}" → avoids duplicate scans
        self._scanned_events = set()
        # Momento 3: pending refresh scans after payment
        # Key: "refresh_{sym_key}_{nts}" → value: timestamp when to trigger
        self._pending_refreshes = {}

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

    def _monitor_tick(self):
        """Single monitor tick — check positions, trigger scans if needed."""
        scan_reason = None
        alerts = []
        now = time.time()

        # Momento 3: Check pending refresh scans (5 min after payment)
        for rkey, trigger_at in list(self._pending_refreshes.items()):
            if now >= trigger_at:
                scan_reason = f"Refresh post-pago ({rkey})"
                del self._pending_refreshes[rkey]
                break

        with self.state_manager.lock:
            s = self.state_manager.state
            all_data = s.get("all_data", [])
            defi_data = s.get("defi_data", [])
            positions = s.get("positions", [])

            if not positions:
                # No positions → no need for scans, just update mins_next
                self._refresh_mins_next(all_data, now, defi_data)
                return

            # Recalculate mins_next from stored timestamps (no API call)
            self._refresh_mins_next(all_data, now, defi_data)

            # Check each position for scan triggers (only if no refresh pending)
            # Collect ALL triggers first, then group by ±5 min window
            if not scan_reason:
                triggered_positions = []  # [(mins_next, pos_label, event_keys)]
                combined = all_data + defi_data

                for pos in positions:
                    cur = self._find_data(pos, all_data, defi_data)

                    # For cross-exchange: use the earliest funding time
                    # between long and short sides
                    mn = -1
                    nts = 0
                    if pos.get("mode") == "cross_exchange":
                        long_ex = pos.get("long_exchange", "")
                        short_ex = pos.get("short_exchange", pos.get("exchange", ""))
                        long_d = next((d for d in combined if d["symbol"] == pos["symbol"] and d["exchange"] == long_ex), None)
                        short_d = next((d for d in combined if d["symbol"] == pos["symbol"] and d["exchange"] == short_ex), None)
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

                    sym_key = f"{pos['symbol']}_{pos['exchange']}"
                    event_key = f"{sym_key}_{nts}"

                    # Momento 1: ~10 min antes del pago → verificar rate
                    pre_key = f"pre_{event_key}"
                    if PRE_PAYMENT_SCAN_MINS >= mn > POST_PAYMENT_SCAN_MINS:
                        if pre_key not in self._scanned_events:
                            triggered_positions.append((
                                mn, f"Pre-pago {pos['symbol']}@{pos['exchange']} en {mn:.0f}min",
                                [pre_key], []
                            ))

                    # Momento 2: Justo despues del pago → obtener rate real
                    post_key = f"post_{event_key}"
                    if mn <= POST_PAYMENT_SCAN_MINS:
                        if post_key not in self._scanned_events:
                            triggered_positions.append((
                                mn, f"Pago {pos['symbol']}@{pos['exchange']}",
                                [post_key],
                                [(sym_key, nts)]  # schedule refresh
                            ))

                # Group triggers within ±5 min window → single scan
                if triggered_positions:
                    # Sort by mins_next
                    triggered_positions.sort(key=lambda t: t[0])

                    # Build groups: positions within 5 min of each other
                    groups = []
                    current_group = [triggered_positions[0]]
                    for tp in triggered_positions[1:]:
                        if tp[0] - current_group[0][0] <= 5:
                            current_group.append(tp)
                        else:
                            groups.append(current_group)
                            current_group = [tp]
                    groups.append(current_group)

                    # Take the first (most urgent) group
                    group = groups[0]
                    labels = [tp[1] for tp in group]
                    scan_reason = " + ".join(labels)

                    # Mark all event keys in the group as scanned
                    for tp in group:
                        for ek in tp[2]:
                            self._scanned_events.add(ek)
                        # Schedule refreshes
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

        # Process earnings and alerts with current data
        with self.state_manager.lock:
            s = self.state_manager.state
            all_data = s.get("all_data", [])
            defi_data = s.get("defi_data", [])
            positions = s.get("positions", [])
            combined = all_data + defi_data

            if combined and positions:
                self._refresh_mins_next(all_data, time.time(), defi_data)
                self._update_earnings(s, combined)
                alerts = self._check_alerts(s, combined)
                s["alerts"] = alerts
                self.state_manager.save()

        # Send WhatsApp alerts outside lock
        if alerts and self.email_notifier:
            try:
                sent = self.email_notifier.send_alerts(alerts)
                if sent:
                    log.info(f"Sent {sent} WhatsApp alert(s)")
            except Exception as e:
                log.error(f"WhatsApp error: {e}")

        # Cleanup old event keys (keep only recent ones)
        self._cleanup_events()

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

        # 7. Update state
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
            self._update_earnings(s, all_data + defi_data)
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
            self.state_manager.save()

        self._last_scan_ts = time.time()
        log.info(
            f"Scan #{self.state_manager.get('scan_count')}: "
            f"{len(all_data)} pairs, {n_sp} spot-perp, {n_cx} cross-exchange"
        )

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

    def _update_earnings(self, state: dict, all_data: list) -> None:
        """Accumulate real earnings based on funding payment timestamps."""
        now = time.time()
        for pos in state["positions"]:
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

        ih = pos.get("ih", 8)
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
            fut_size = pos["capital_used"] / 2
            earn_per_payment = fut_size * cfr
        else:
            earn_per_payment = 0

        self._record_earnings(pos, earn_per_payment * payments, cfr, now, payments)

    def _update_cross_exchange_earnings(self, pos: dict, all_data: list, now: float):
        """Cross-exchange earnings: track differential between both exchanges."""
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

        long_ih = long_data.get("ih", 8)
        short_ih = short_data.get("ih", 8)
        min_ih = min(long_ih, short_ih)
        interval_secs = min_ih * 3600

        short_nts = short_data.get("next_funding_ts", 0)
        long_nts = long_data.get("next_funding_ts", 0)
        nts = short_nts if short_nts > 0 else long_nts

        last_payment_ts = self._calc_last_payment_ts(nts, interval_secs, now)
        last_up = pos.get("last_earn_update", pos["entry_time"] / 1000)

        if last_payment_ts > 0:
            payments = self._count_payments_since(last_up, last_payment_ts, interval_secs)
        else:
            elapsed_h = (now - last_up) / 3600
            payments = int(elapsed_h / min_ih)

        if payments < 1:
            return

        fut_size = pos["capital_used"] / 2
        short_fr = short_data["fr"]
        long_fr = long_data["fr"]
        short_earn = fut_size * short_fr if short_fr > 0 else -(fut_size * abs(short_fr))
        long_cost = -(fut_size * long_fr) if long_fr > 0 else fut_size * abs(long_fr)
        earn_per_payment = short_earn + long_cost

        differential = short_fr - long_fr
        self._record_earnings(pos, earn_per_payment * payments, differential, now, payments)

    def _calc_last_payment_ts(self, next_funding_ts: int, interval_secs: int,
                              now: float) -> float:
        if not next_funding_ts or next_funding_ts <= 0:
            return 0

        next_ts_sec = next_funding_ts / 1000

        if next_ts_sec > now:
            return next_ts_sec - interval_secs
        else:
            ts = next_ts_sec
            while ts + interval_secs <= now:
                ts += interval_secs
            return ts

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
            fut_size = pos["capital_used"] / 2
            earn_per_iv = fut_size * cfr
        else:
            earn_per_iv = 0

        self._record_earnings(pos, earn_per_iv * full_ivs, cfr, now, full_ivs)

    def _record_earnings(self, pos: dict, earned_now: float, rate: float,
                         now: float, full_ivs: int):
        """Record earnings for a position."""
        if earned_now > 0:
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
        pos["payment_count"] = len(pos.get("payments", []))
        if pos.get("payments"):
            pos["avg_rate"] = sum(p["rate"] for p in pos["payments"]) / len(pos["payments"])

        if earned_now > 0:
            log.info(f"  +${earned_now:.4f} {pos['symbol']} ({full_ivs}ivs @ {rate*100:.4f}%)")

    # ── Alerts ────────────────────────────────────────────────

    def _check_alerts(self, state: dict, all_data: list) -> list:
        """Check positions for alerts: rate reversal, rate drop, pre-payment."""
        alerts = []
        alert_mins = state.get("alert_minutes_before", 5)

        for i, pos in enumerate(state["positions"]):
            cur = self._find_data(pos, all_data)
            if not cur:
                continue

            cfr = cur["fr"]

            # Rate reversal (critical)
            if (pos["entry_fr"] > 0 and cfr < 0) or (pos["entry_fr"] < 0 and cfr > 0):
                alerts.append({
                    "type": "RATE_REVERSAL",
                    "severity": "CRITICAL",
                    "position_idx": i,
                    "symbol": pos["symbol"],
                    "exchange": pos["exchange"],
                    "message": f"Funding rate cambio de signo: {pos['entry_fr']*100:.4f}% -> {cfr*100:.4f}%",
                })

            # Rate dropped >75% (warning)
            elif abs(cfr) < abs(pos["entry_fr"]) * 0.25:
                alerts.append({
                    "type": "RATE_DROP",
                    "severity": "WARNING",
                    "position_idx": i,
                    "symbol": pos["symbol"],
                    "exchange": pos["exchange"],
                    "message": f"Rate cayo >75%: {pos['entry_fr']*100:.4f}% -> {cfr*100:.4f}%",
                })

            # Pre-payment alert (N min before next funding)
            mins_next = cur.get("mins_next", -1)
            if 0 < mins_next <= alert_mins:
                if cfr <= 0 and pos["entry_fr"] > 0:
                    alerts.append({
                        "type": "PRE_PAYMENT_UNFAVORABLE",
                        "severity": "WARNING",
                        "position_idx": i,
                        "symbol": pos["symbol"],
                        "exchange": pos["exchange"],
                        "message": f"Proximo pago en {mins_next:.0f}min — tasa desfavorable: {cfr*100:.4f}%",
                    })

        return alerts
