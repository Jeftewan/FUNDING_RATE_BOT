"""Background scanner and position monitor threads — v8.1 smart scan."""
import time
import logging
import threading
from datetime import datetime

log = logging.getLogger("bot")


class ScannerWorker:
    def __init__(self, exchange_manager, arbitrage_scanner, state_manager,
                 coinglass_client, config, email_notifier=None):
        self.exchange_manager = exchange_manager
        self.arb_scanner = arbitrage_scanner
        self.state_manager = state_manager
        self.coinglass = coinglass_client
        self.config = config
        self.email_notifier = email_notifier
        self._started = False
        self._last_scan_ts = 0
        self._scan_lock = threading.Lock()

    def start(self):
        if self._started:
            return
        self._started = True
        threading.Thread(target=self._scan_loop, daemon=True).start()
        threading.Thread(target=self._monitor_loop, daemon=True).start()
        log.info("Scanner workers launched (scan + monitor)")

    def _scan_loop(self):
        """Main scan loop — fetch rates, find opportunities."""
        time.sleep(5)
        while True:
            try:
                self._run_scan()
            except Exception as e:
                log.exception(f"Scan error: {e}")
                with self.state_manager.lock:
                    self.state_manager.update(
                        status=f"Error: {str(e)[:80]}",
                        last_error=str(e),
                    )
            time.sleep(self.state_manager.get("scan_interval", 300))

    def _monitor_loop(self):
        """Position monitor — every 30s: update mins_next, check payments,
        trigger smart scan before payments, send alerts."""
        time.sleep(15)
        while True:
            alerts = []
            try:
                with self.state_manager.lock:
                    s = self.state_manager.state
                    all_data = s.get("all_data", [])
                    positions = s.get("positions", [])

                    if positions:
                        # Recalculate mins_next from stored timestamps (live)
                        now = time.time()
                        for d in all_data:
                            nts = d.get("next_funding_ts", 0)
                            if nts and nts > 0:
                                d["mins_next"] = max(0, (nts / 1000 - now) / 60)

                        # Check if any position has a payment coming in <= 2 min
                        # and we haven't scanned in the last 90 seconds
                        need_fresh_scan = False
                        for pos in positions:
                            cur = self._find_data(pos, all_data)
                            if cur:
                                mn = cur.get("mins_next", -1)
                                if 0 < mn <= 2 and (now - self._last_scan_ts) > 90:
                                    need_fresh_scan = True
                                    log.info(
                                        f"Smart scan: {pos['symbol']}@{pos['exchange']} "
                                        f"payment in {mn:.1f}min"
                                    )
                                    break

                if need_fresh_scan:
                    # Run scan outside the lock to get fresh rates
                    log.info("Triggering pre-payment scan...")
                    try:
                        self._run_scan()
                    except Exception as e:
                        log.error(f"Pre-payment scan failed: {e}")

                # Now process earnings and alerts with (possibly refreshed) data
                with self.state_manager.lock:
                    s = self.state_manager.state
                    all_data = s.get("all_data", [])
                    positions = s.get("positions", [])

                    if all_data and positions:
                        # Recalc mins_next again (may have been updated by scan)
                        now = time.time()
                        for d in all_data:
                            nts = d.get("next_funding_ts", 0)
                            if nts and nts > 0:
                                d["mins_next"] = max(0, (nts / 1000 - now) / 60)

                        self._update_earnings(s, all_data)
                        alerts = self._check_alerts(s, all_data)
                        s["alerts"] = alerts
                        self.state_manager.save()

                # Send WhatsApp alerts outside the lock
                if alerts and self.email_notifier:
                    try:
                        sent = self.email_notifier.send_alerts(alerts)
                        if sent:
                            log.info(f"Sent {sent} WhatsApp alert(s)")
                    except Exception as e:
                        log.error(f"WhatsApp notification error: {e}")
            except Exception as e:
                log.exception(f"Monitor error: {e}")
            time.sleep(30)

    def _find_data(self, pos: dict, all_data: list) -> dict:
        """Find current market data for a position."""
        return next(
            (d for d in all_data
             if d["symbol"] == pos["symbol"] and d["exchange"] == pos["exchange"]),
            None,
        )

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

        # 5. Build unified opportunities list
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

        # 6. Update state
        status_parts = [f"{len(r)}{n[:2].upper()}" for n, r in all_rates.items() if r]
        status_str = "+".join(status_parts)

        with self.state_manager.lock:
            s = self.state_manager.state
            s["all_data"] = all_data
            self._update_earnings(s, all_data)
            s["opportunities"] = opportunities
            s["coinglass_data"] = cg_opps
            s["last_scan"] = time.time()
            s["scan_count"] = s.get("scan_count", 0) + 1
            s["last_scan_time"] = datetime.now().strftime("%H:%M:%S")
            n_sp = len(spot_perp_opps)
            n_cx = len(cross_ex_opps)
            s["status"] = (
                f"OK — {status_str} | "
                f"{n_sp} spot-perp, {n_cx} cross-ex | "
                f"{len(opportunities)} total"
            )
            s["last_error"] = ""
            self.state_manager.save()

        self._last_scan_ts = time.time()
        log.info(
            f"Scan #{self.state_manager.get('scan_count')}: "
            f"{len(all_data)} pairs, {n_sp} spot-perp, {n_cx} cross-exchange"
        )

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

        # Determine the last payment timestamp
        last_payment_ts = self._calc_last_payment_ts(nts, interval_secs, now)
        if last_payment_ts <= 0:
            # Fallback: use elapsed-time method if no timestamp available
            self._update_earnings_elapsed(pos, cfr, ih, now)
            return

        # How many payments happened since last update?
        last_up = pos.get("last_earn_update", pos["entry_time"] / 1000)
        payments = self._count_payments_since(last_up, last_payment_ts, interval_secs)
        if payments < 1:
            return

        # Calculate earnings
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

        # Use the shorter interval
        long_ih = long_data.get("ih", 8)
        short_ih = short_data.get("ih", 8)
        min_ih = min(long_ih, short_ih)
        interval_secs = min_ih * 3600

        # Try timestamp-based detection
        short_nts = short_data.get("next_funding_ts", 0)
        long_nts = long_data.get("next_funding_ts", 0)
        # Use whichever has a valid next timestamp
        nts = short_nts if short_nts > 0 else long_nts

        last_payment_ts = self._calc_last_payment_ts(nts, interval_secs, now)
        last_up = pos.get("last_earn_update", pos["entry_time"] / 1000)

        if last_payment_ts > 0:
            payments = self._count_payments_since(last_up, last_payment_ts, interval_secs)
        else:
            # Fallback: elapsed time
            elapsed_h = (now - last_up) / 3600
            payments = int(elapsed_h / min_ih)

        if payments < 1:
            return

        # Net earnings
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
        """Calculate when the most recent funding payment occurred.

        next_funding_ts is in milliseconds from the exchange.
        Returns unix timestamp in seconds of the last payment, or 0.
        """
        if not next_funding_ts or next_funding_ts <= 0:
            return 0

        next_ts_sec = next_funding_ts / 1000

        if next_ts_sec > now:
            # Next payment is in the future → last payment was one interval before
            return next_ts_sec - interval_secs
        else:
            # next_funding_ts is in the past (stale data from last scan)
            # Walk forward to find the actual last payment before now
            ts = next_ts_sec
            while ts + interval_secs <= now:
                ts += interval_secs
            return ts

    def _count_payments_since(self, last_update: float, last_payment_ts: float,
                              interval_secs: int) -> int:
        """Count how many funding payments occurred between last_update and last_payment_ts."""
        if last_payment_ts <= last_update:
            return 0

        # Walk backwards from last_payment to count payments after last_update
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
