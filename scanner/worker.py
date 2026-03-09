"""Background scanner and position monitor threads — v8.0 unified."""
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
        """Position monitor — update earnings, check pre-payment alerts every 60s."""
        time.sleep(15)
        while True:
            alerts = []
            try:
                with self.state_manager.lock:
                    s = self.state_manager.state
                    all_data = s.get("all_data", [])
                    positions = s.get("positions", [])
                    if all_data and positions:
                        self._update_earnings(s, all_data)
                        alerts = self._check_alerts(s, all_data)
                        s["alerts"] = alerts
                        self.state_manager.save()

                # Send email alerts outside the lock
                if alerts and self.email_notifier:
                    try:
                        sent = self.email_notifier.send_alerts(alerts)
                        if sent:
                            log.info(f"Sent {sent} alert email(s)")
                    except Exception as e:
                        log.error(f"Email notification error: {e}")
            except Exception as e:
                log.exception(f"Monitor error: {e}")
            time.sleep(60)

    def _run_scan(self):
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

        log.info(
            f"Scan #{self.state_manager.get('scan_count')}: "
            f"{len(all_data)} pairs, {n_sp} spot-perp, {n_cx} cross-exchange"
        )

    def _update_earnings(self, state: dict, all_data: list) -> None:
        """Accumulate real earnings based on funding payment intervals."""
        now = time.time()
        for pos in state["positions"]:
            mode = pos.get("mode", "spot_perp")

            if mode == "spot_perp":
                self._update_spot_perp_earnings(pos, all_data, now)
            else:
                self._update_cross_exchange_earnings(pos, all_data, now)

    def _update_spot_perp_earnings(self, pos: dict, all_data: list, now: float):
        """Spot-perp earnings: earn when FR > 0 (we are short futures)."""
        cur = next(
            (d for d in all_data
             if d["symbol"] == pos["symbol"] and d["exchange"] == pos["exchange"]),
            None,
        )
        if not cur:
            return

        ih = pos.get("ih", 8)
        last_up = pos.get("last_earn_update", pos["entry_time"] / 1000)
        elapsed_h = (now - last_up) / 3600
        full_ivs = int(elapsed_h / ih)
        if full_ivs < 1:
            return

        cfr = cur["fr"]
        if cfr > 0:
            fut_size = pos["capital_used"] / 2
            earn_per_iv = fut_size * cfr
        else:
            earn_per_iv = 0

        self._record_earnings(pos, earn_per_iv * full_ivs, cfr, now, full_ivs)

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

        # Use the shorter interval to determine payment timing
        long_ih = long_data.get("ih", 8)
        short_ih = short_data.get("ih", 8)
        min_ih = min(long_ih, short_ih)

        last_up = pos.get("last_earn_update", pos["entry_time"] / 1000)
        elapsed_h = (now - last_up) / 3600
        full_ivs = int(elapsed_h / min_ih)
        if full_ivs < 1:
            return

        # Net earnings: short side receives, long side pays
        fut_size = pos["capital_used"] / 2
        short_fr = short_data["fr"]
        long_fr = long_data["fr"]
        # Short side: we receive short_fr when positive
        short_earn = fut_size * short_fr if short_fr > 0 else -(fut_size * abs(short_fr))
        # Long side: we pay long_fr when positive (longs pay shorts)
        long_cost = -(fut_size * long_fr) if long_fr > 0 else fut_size * abs(long_fr)
        earn_per_iv = short_earn + long_cost

        differential = short_fr - long_fr
        self._record_earnings(pos, earn_per_iv * full_ivs, differential, now, full_ivs)

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
        now = time.time()
        alert_mins = state.get("alert_minutes_before", 5)

        for i, pos in enumerate(state["positions"]):
            cur = next(
                (d for d in all_data
                 if d["symbol"] == pos["symbol"] and d["exchange"] == pos["exchange"]),
                None,
            )
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

            # Pre-payment check (5 min before next funding)
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
