"""Background scanner and position monitor threads."""
import time
import logging
import threading
from datetime import datetime
from analysis.scoring import risk_score, calculate_rsi
from analysis.fees import calculate_returns
from portfolio.manager import update_position_earnings
from portfolio.actions import generate_actions
from portfolio.risk import generate_alerts

log = logging.getLogger("bot")


class ScannerWorker:
    def __init__(self, exchange_manager, arbitrage_scanner, state_manager,
                 coinglass_client, config):
        self.exchange_manager = exchange_manager
        self.arb_scanner = arbitrage_scanner
        self.state_manager = state_manager
        self.coinglass = coinglass_client
        self.config = config
        self._started = False

    def start(self):
        if self._started:
            return
        self._started = True
        threading.Thread(target=self._scan_loop, daemon=True).start()
        threading.Thread(target=self._monitor_loop, daemon=True).start()
        log.info("Scanner workers launched (scan + monitor)")

    def _scan_loop(self):
        """Main scan loop — fetch rates, find opportunities, generate actions."""
        time.sleep(5)  # Initial delay
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
        """Position monitor — update earnings and check alerts every 60s."""
        time.sleep(15)  # Wait for first scan
        while True:
            try:
                with self.state_manager.lock:
                    s = self.state_manager.state
                    all_data = s.get("all_data", [])
                    if all_data:
                        update_position_earnings(s, all_data)
                        alerts = generate_alerts(s["positions"], all_data)
                        s["alerts"] = alerts
                        self.state_manager.save()
            except Exception as e:
                log.exception(f"Monitor error: {e}")
            time.sleep(60)

    def _run_scan(self):
        log.info("Scan starting...")
        with self.state_manager.lock:
            self.state_manager.set("status", "Escaneando...")

        # 1. Fetch rates from all exchanges via CCXT
        all_rates = self.exchange_manager.fetch_all_funding_rates()

        # Build flat all_data list (backward compatible format)
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

        # 3. Scan for spot-perp opportunities (v7 new)
        spot_perp_opps = self.arb_scanner.scan_spot_perp_opportunities(all_rates)

        # 4. Scan for cross-exchange opportunities (v7 new)
        cross_ex_opps = self.arb_scanner.scan_cross_exchange_opportunities(all_rates)

        # 5. Legacy analysis — safe/aggr top (backward compat with v5/v6 dashboard)
        mv = self.state_manager.get("min_volume", 1000000)
        pos_l = sorted(
            [t for t in all_data if t["fr"] > 0.0001 and t["vol24h"] >= mv],
            key=lambda x: x["fr"], reverse=True,
        )
        neg_l = sorted(
            [t for t in all_data if t["fr"] < -0.0001 and t["vol24h"] >= mv],
            key=lambda x: x["fr"],
        )

        safe = self._analyze_safe(pos_l)
        aggr = self._analyze_aggr(neg_l)

        # 6. Update state
        exchange_counts = {ex: len(rates) for ex, rates in all_rates.items()}
        status_parts = [f"{len(r)}{n[:2].upper()}" for n, r in all_rates.items() if r]
        status_str = "+".join(status_parts)

        with self.state_manager.lock:
            s = self.state_manager.state
            s["all_data"] = all_data
            update_position_earnings(s, all_data)
            s["safe_top"] = safe
            s["aggr_top"] = aggr
            s["spot_perp_opportunities"] = [o.to_dict() for o in spot_perp_opps]
            s["cross_exchange_opportunities"] = [o.to_dict() for o in cross_ex_opps]
            s["coinglass_data"] = cg_opps
            s["last_scan"] = time.time()
            s["scan_count"] = s.get("scan_count", 0) + 1
            s["last_scan_time"] = datetime.now().strftime("%H:%M:%S")
            s["actions"] = generate_actions(s)
            n_sp = len(spot_perp_opps)
            n_cx = len(cross_ex_opps)
            s["status"] = (
                f"OK — {status_str} | "
                f"{len(pos_l)}pos {len(neg_l)}neg | "
                f"{n_sp}sp {n_cx}cx | "
                f"{len(aggr)}aggr"
            )
            s["last_error"] = ""
            self.state_manager.save()

        log.info(
            f"Scan #{self.state_manager.get('scan_count')}: "
            f"{len(all_data)} pairs, {n_sp} spot-perp, {n_cx} cross-exchange"
        )

    def _analyze_safe(self, tokens, limit=12):
        """Analyze top safe (positive funding) tokens."""
        scored = []
        for t in tokens[:limit]:
            hist_obj = self.exchange_manager.fetch_funding_history(
                t["symbol"], t["exchange"]
            )
            time.sleep(0.08)

            # Detect interval from history
            if hist_obj.timestamps:
                detected_ih = self.exchange_manager.detect_funding_interval_from_history(
                    hist_obj.timestamps
                )
                t["ih"] = detected_ih
                t["ipd"] = 24 / detected_ih

            h = hist_obj.to_dict()
            sc = risk_score(t, h, is_aggressive=False)
            scored.append({"token": t, "hist": h, "score": sc})

        scored.sort(
            key=lambda x: (x["score"], -x["token"].get("mins_next", 999)),
            reverse=True,
        )
        return scored[:5]

    def _analyze_aggr(self, tokens, limit=10):
        """Analyze top aggressive (negative funding, low RSI) tokens."""
        scored = []
        for t in tokens[:limit]:
            hist_obj = self.exchange_manager.fetch_funding_history(
                t["symbol"], t["exchange"]
            )
            time.sleep(0.08)

            if hist_obj.timestamps:
                detected_ih = self.exchange_manager.detect_funding_interval_from_history(
                    hist_obj.timestamps
                )
                t["ih"] = detected_ih
                t["ipd"] = 24 / detected_ih

            h = hist_obj.to_dict()

            # RSI check
            klines = self.exchange_manager.fetch_klines(
                t["symbol"], t["exchange"], interval="1d", limit=16
            )
            time.sleep(0.08)

            if not klines or len(klines) < 15:
                continue
            closes = [float(k[4]) for k in klines]
            rsi = calculate_rsi(closes)
            if rsi < 0 or rsi > 40:
                continue

            sc = risk_score(t, h, is_aggressive=True)
            scored.append({"token": t, "hist": h, "score": sc, "rsi": rsi})

        scored.sort(
            key=lambda x: (x["score"], -x["token"].get("mins_next", 999)),
            reverse=True,
        )
        return scored[:5]
