"""Core arbitrage opportunity detection — spot-perp and cross-exchange."""
import logging
import time
from core.models import FundingRate, SpotPerpOpportunity, CrossExchangeOpportunity
from analysis.funding import FundingAggregator
from analysis.fees import (calculate_spot_perp_fees, calculate_cross_exchange_fees,
                           calculate_break_even_hours)
from analysis.scoring import opportunity_score, stability_grade, grade_from_net_apr, estimated_hold_days
from analysis import ml_scorer
from analysis.ml_features import LOOKBACK, window_stats, fee_drag_deterministic

log = logging.getLogger("bot")

NOTIONAL = 1000  # Reference notional for comparison
# Piso de tasa DIARIA (rate × pagos/día) para analizar un par spot_perp: la tasa
# base de Binance (interés 0.01 %/8h = 0.03 %/día, sin prima) INCLUIDA. Cientos
# de pares la pagan; la mayoría el modelo los da negativos tras fees, pero
# algunos con historia fuerte salen positivos (VELODROME +16 % el 2026-10-22),
# por eso no se excluyen. Antes el piso era por pago (0.01 %): dejaba fuera los
# 1h/4h buenos y admitía 8h mediocres.
SCAN_MIN_DAILY_RATE = 0.0003
# Tope de candidatos por exchange: primero los que pagan prima sobre la tasa
# base (por tasa diaria), luego los de tasa base por volumen. El historial se
# cachea por símbolo hasta el próximo settlement (máx. 1 h) → el primer scan
# hace ≤ 250 llamadas y los siguientes casi ninguna.
SPOT_PERP_MAX_CANDIDATES = 250


class ArbitrageScanner:
    def __init__(self, exchange_manager, config, defi_manager=None):
        self.exchange_manager = exchange_manager
        self.defi_manager = defi_manager
        self.config = config
        self.aggregator = FundingAggregator()
        self._defi_exchanges = set(getattr(config, "DEFI_EXCHANGES", []))

    @staticmethod
    def _model_params(all_rates_hist: list, fr, score_params: dict) -> dict:
        """Params para el MODELO según el feature_spec del artefacto cargado.

        settled_v1 (dataset Binance): ventana = las LOOKBACK liquidaciones
        anteriores a la última y "tasa actual" = la última liquidada — igual que
        scripts/ml_binance.py; ambas salen del mismo endpoint (/fapi/v1/fundingRate).
        """
        if ml_scorer.feature_spec() != "settled_v1":
            return score_params
        from analysis.indicators import compute_all_indicators
        if len(all_rates_hist) < LOOKBACK + 1:
            return {**score_params, "_thin": True}
        window = all_rates_hist[-(LOOKBACK + 1):-1]
        cur = all_rates_hist[-1]
        ws = window_stats(window)
        if ws is None:
            return {**score_params, "_thin": True}
        return {"cv": ws["cv"], "min_ratio": ws["min_ratio"], "streak": ws["streak"],
                "pct": ws["pct"], "volume": fr.volume_24h, "settlement_avg": ws["settlement_avg"],
                "payments_per_day": fr.payments_per_day, "current_rate": cur,
                "mode": "spot_perp", "_indicators": compute_all_indicators(abs(cur), window)}

    @staticmethod
    def _resolve_score(heuristic_score: int, score_params: dict):
        """Devuelve (display_score, score_heuristic, model_prediction).

        Si el modelo ML está cargado y predice, el MODELO manda el ranking; el
        score heurístico v11.0 se conserva para fallback/comparación en vivo.
        Si no hay modelo o falla, se usa el heurístico (model_prediction=None).

        Solo spot_perp: el modelo se entrena con series de tasa única y en
        cross_exchange/defi no hay label válido para validarlo (IC en vivo
        −0.105, reports/ml_train_20260913.md) → esos modos usan el heurístico.
        """
        if score_params.get("mode", "spot_perp") != "spot_perp" or score_params.get("_thin"):
            return heuristic_score, heuristic_score, None
        ms = ml_scorer.predict_score(score_params, score_params.get("_indicators"))
        if ms is not None:
            model_score, model_pred = ms
            return model_score, heuristic_score, model_pred
        return heuristic_score, heuristic_score, None

    def _fetch_history(self, symbol: str, exchange: str, limit: int):
        """Route history fetch to the correct manager (CEX vs DeFi)."""
        if self.defi_manager and exchange in self._defi_exchanges:
            return self.defi_manager.fetch_funding_history(
                symbol, exchange, limit=limit
            )
        return self.exchange_manager.fetch_funding_history(
            symbol, exchange, limit=limit
        )

    def _spot_venue(self, fr: FundingRate):
        """'cex' si hay spot centralizado activo, 'bsc' si solo existe on-chain
        (Alpha BSC) y el on-chain está habilitado, None si no es cubrible."""
        if self.exchange_manager.fetch_spot_availability(fr.symbol, fr.exchange):
            return "cex"
        if fr.exchange.lower() == "binance":
            try:
                from exchanges import onchain_bsc
                if onchain_bsc.enabled() and onchain_bsc.alpha_token(fr.symbol):
                    return "bsc"
            except Exception as e:
                log.debug(f"alpha check {fr.symbol}: {e}")
        return None

    def scan_spot_perp_opportunities(
        self, all_rates: dict, min_volume: float = None,
        limit: int = SPOT_PERP_MAX_CANDIDATES
    ) -> list:
        """
        Mode 1: Same-exchange spot+perp hedge.
        Candidatos: tasa DIARIA > SCAN_MIN_DAILY_RATE, volumen ≥ piso y spot
        cubrible (CEX, o BSC si el on-chain está activo). Los pares sin spot
        operable ya no se muestran: no se pueden ejecutar delta-neutral.
        """
        if "spot_perp" not in self.config.ARBITRAGE_MODES:
            return []

        mv = min_volume or self.config.MIN_VOLUME
        opportunities = []

        for exchange_name, rates in all_rates.items():
            eligible = [r for r in rates
                        if r.rate * r.payments_per_day >= SCAN_MIN_DAILY_RATE * (1 - 1e-6)
                        and r.volume_24h >= mv]
            # Tasa diaria redondeada (los de tasa base empatan) y, a igualdad, volumen.
            eligible.sort(key=lambda r: (round(r.rate * r.payments_per_day, 7), r.volume_24h),
                          reverse=True)
            n_done = 0
            for fr in eligible:
                if n_done >= limit:
                    break
                venue = self._spot_venue(fr)
                if not venue:
                    continue
                n_done += 1
                try:
                    opp = self._analyze_spot_perp(fr, spot_venue=venue)
                    # Antes: net_3d > 0 (recuperar el round-trip de fees en 3 días)
                    # exigía ~73 % APR → solo pasaban picos y nunca los pares
                    # persistentes de tasa moderada, que son los que más rinden en el
                    # tiempo. Basta funding acumulado positivo; el ranking/filtro lo
                    # decide el APR neto esperado del modelo (ya descuenta fees).
                    if opp and opp.accumulated_3d_pct > 0:
                        opportunities.append(opp)
                except Exception as e:
                    log.warning(f"Spot-perp analysis error {fr.symbol}@{fr.exchange}: {e}")

        opportunities.sort(key=lambda o: o.score, reverse=True)
        return opportunities

    def _analyze_spot_perp(self, fr: FundingRate, spot_venue: str = "cex") -> SpotPerpOpportunity:
        """Analyze a single spot-perp opportunity."""
        n_payments_3d = int(fr.payments_per_day * 3)
        history_obj = self._fetch_history(
            fr.symbol, fr.exchange, limit=max(n_payments_3d, LOOKBACK + 1)
        )

        accumulated_3d = self.aggregator.calculate_3day_accumulated(
            history_obj.rates, fr.payments_per_day
        ) if history_obj.rates else fr.rate * n_payments_3d

        fees = calculate_spot_perp_fees(
            fr.exchange, NOTIONAL, fr.volume_24h
        )

        # El funding se cobra sobre la pierna perp = NOTIONAL/2 (la otra mitad es
        # el spot). Con signo: un acumulado negativo es pérdida, no ingreso
        # (antes NOTIONAL·abs() inflaba ×2 y convertía pérdidas en revenue).
        revenue_3d_usd = (NOTIONAL / 2) * accumulated_3d
        net_3d = revenue_3d_usd - fees["total_cost"]

        daily_income = self.aggregator.calculate_daily_income(
            fr.rate, fr.payments_per_day, NOTIONAL / 2
        )
        apr = self.aggregator.calculate_apr(fr.rate, fr.payments_per_day)

        hourly_income = daily_income / 24
        break_even_h = calculate_break_even_hours(fees["total_cost"], hourly_income)

        # Features de historia: últimas LOOKBACK liquidaciones con la MISMA
        # función que el entrenamiento (paridad por construcción).
        all_rates_hist = list(history_obj.rates or [])
        rates = all_rates_hist[-LOOKBACK:]
        ws = window_stats(rates)
        hist_dict = history_obj.to_dict()
        hist_dict["_rates"] = rates
        if ws:
            hist_dict.update(pct=ws["pct"], streak=ws["streak"])
            cv, min_ratio, settlement_avg = ws["cv"], ws["min_ratio"], ws["settlement_avg"]
        else:
            cv, min_ratio, settlement_avg = 999.0, 0.0, abs(fr.rate)
            hist_dict.update(pct=0, streak=0)
        # Fee drag determinista (el mismo con el que se optimizó v11.0 y se entrenó el modelo).
        fee_drag = fee_drag_deterministic(settlement_avg, fr.payments_per_day)
        hist_dict["fee_drag"] = fee_drag
        hist_dict["settlement_avg"] = settlement_avg

        score_params = {
            "cv": cv,
            "min_ratio": min_ratio,
            "streak": hist_dict["streak"],
            "pct": hist_dict["pct"],
            "volume": fr.volume_24h,
            "settlement_avg": settlement_avg,
            "payments_per_day": fr.payments_per_day,
            "fee_drag": fee_drag,
            "current_rate": fr.rate,
            # thin history (<5 no triviales) → el heurístico usa sus neutros y
            # el modelo NO predice (fuera de su distribución de entrenamiento).
            "rates": rates if ws else rates[:4],
            "mode": "spot_perp",
            "_thin": ws is None,
        }
        sc = opportunity_score(score_params)
        model_params = self._model_params(all_rates_hist, fr, score_params)
        display_score, _, model_pred = self._resolve_score(sc, model_params)
        score_heuristic = sc
        grade = grade_from_net_apr(model_pred) if model_pred is not None else stability_grade(display_score)
        est_days = estimated_hold_days(hist_dict, fr.payments_per_day)

        has_spot = spot_venue == "cex"

        return SpotPerpOpportunity(
            symbol=fr.symbol,
            exchange=fr.exchange,
            funding_rate=fr.rate,
            interval_hours=fr.interval_hours,
            payments_per_day=fr.payments_per_day,
            price=fr.price,
            volume_24h=fr.volume_24h,
            accumulated_3d_pct=accumulated_3d * 100,
            apr=apr,
            daily_income_per_1000=daily_income,
            net_3d_revenue_per_1000=net_3d,
            fees_total=fees["total_cost"],
            break_even_hours=break_even_h,
            score=display_score,
            score_heuristic=score_heuristic,
            model_prediction=model_pred,
            has_spot=has_spot,
            spot_venue="bsc" if spot_venue == "bsc" else fr.exchange.lower(),
            mins_to_next=fr.mins_to_next,
            next_funding_ts=fr.next_funding_ts,
            history=hist_dict,
            stability_grade=grade,
            estimated_hold_days=est_days,
            indicators=score_params.get("_indicators", {}),
        )

    def scan_cross_exchange_opportunities(
        self, all_rates: dict, min_volume: float = None, limit: int = 15
    ) -> list:
        """
        Mode 2: Cross-exchange arbitrage.
        For each symbol on 2+ exchanges, find profitable differentials.
        """
        if "cross_exchange" not in self.config.ARBITRAGE_MODES:
            return []

        mv = min_volume or self.config.MIN_VOLUME
        by_symbol = self.aggregator.aggregate_rates_by_symbol(all_rates)

        opportunities = []
        checked = 0

        for symbol, rates_list in by_symbol.items():
            if len(rates_list) < 2:
                continue

            # volume_24h == 0 means "unknown" (DeFi venues like GMX/Aster/
            # Lighter/Extended don't expose it) → treat as passing the floor
            # instead of excluding the leg.
            vol_rates = [r for r in rates_list
                         if r.volume_24h >= mv or r.volume_24h == 0]
            if len(vol_rates) < 2:
                continue

            sorted_rates = sorted(vol_rates, key=lambda r: r.rate)
            long_candidate = sorted_rates[0]
            short_candidate = sorted_rates[-1]

            differential = short_candidate.rate - long_candidate.rate

            if differential < self.config.MIN_FUNDING_DIFFERENTIAL:
                continue

            try:
                opp = self._analyze_cross_exchange(
                    symbol, long_candidate, short_candidate
                )
                if opp and opp.net_3d_revenue_per_1000 > 0:
                    opportunities.append(opp)
                    checked += 1
            except Exception as e:
                log.warning(f"Cross-exchange analysis error {symbol}: {e}")

            if checked >= limit:
                break
            time.sleep(0.05)

        opportunities.sort(key=lambda o: o.score, reverse=True)
        return opportunities

    def _analyze_cross_exchange(
        self, symbol: str,
        long_fr: FundingRate, short_fr: FundingRate
    ) -> CrossExchangeOpportunity:
        """Analyze a single cross-exchange opportunity.

        Each side earns/costs at its own payment frequency:
        - Short side earns: short_rate * short_ppd per day
        - Long side costs: long_rate * long_ppd per day
        Net daily rate = short_rate * short_ppd - long_rate * long_ppd
        """
        differential = short_fr.rate - long_fr.rate
        long_ppd = long_fr.payments_per_day
        short_ppd = short_fr.payments_per_day

        # Daily rate accounting for each side's actual payment frequency
        # Short side: you receive short_rate each payment (short is positive = you earn)
        # Long side: you pay long_rate each payment (long's rate, usually lower/negative)
        daily_rate = (short_fr.rate * short_ppd) - (long_fr.rate * long_ppd)

        # Fetch history from both exchanges
        n_long_3d = int(long_ppd * 3)
        n_short_3d = int(short_ppd * 3)
        long_hist = self._fetch_history(
            symbol, long_fr.exchange, limit=max(n_long_3d + 5, 15)
        )
        short_hist = self._fetch_history(
            symbol, short_fr.exchange, limit=max(n_short_3d + 5, 15)
        )

        # Calculate 3-day accumulated using each side's own history and frequency
        if long_hist.rates and short_hist.rates:
            n_long = min(len(long_hist.rates), n_long_3d)
            n_short = min(len(short_hist.rates), n_short_3d)
            long_sum = sum(long_hist.rates[-n_long:])
            short_sum = sum(short_hist.rates[-n_short:])
            accumulated_3d = short_sum - long_sum
        else:
            accumulated_3d = daily_rate * 3

        fees = calculate_cross_exchange_fees(
            long_fr.exchange, short_fr.exchange,
            NOTIONAL, min(long_fr.volume_24h, short_fr.volume_24h)
        )

        revenue_3d_usd = NOTIONAL / 2 * abs(accumulated_3d)
        net_3d = revenue_3d_usd - fees["total_cost"]

        daily_income = abs(daily_rate) * (NOTIONAL / 2)
        apr = abs(daily_rate) * 365 * 100

        hourly_income = daily_income / 24
        break_even_h = calculate_break_even_hours(fees["total_cost"], hourly_income)

        # Analyze historical differential: consistency, stability, streak, trend
        diff_hist = {"consistency_pct": 0, "streak": 0, "cv": 999,
                     "min_ratio": 0, "trend": 1.0, "diff_series": [],
                     "period_diffs": []}
        if (long_hist.rates and long_hist.timestamps
                and short_hist.rates and short_hist.timestamps):
            diff_hist = self._analyze_differential_history(
                long_hist, short_hist, long_ppd, short_ppd
            )

        fee_ratio = fees["total_cost"] / revenue_3d_usd if revenue_3d_usd > 0 else 1
        diff_hist["fee_drag"] = fee_ratio
        diff_series = diff_hist.get("diff_series", [])
        # Per-period differentials give enough samples for momentum/z-score/
        # percentile/regime to compute (daily aggregates alone rarely clear the
        # 5-10 sample thresholds required by indicators.py).
        period_diffs = diff_hist.get("period_diffs", [])
        rates_for_indicators = period_diffs if len(period_diffs) >= 5 else diff_series

        # Settlement-based yield: avg of historical daily differentials
        settlement_avg = (abs(diff_hist.get("avg_diff", 0))
                          if diff_series else abs(differential))

        long_is_defi = long_fr.exchange in self._defi_exchanges
        short_is_defi = short_fr.exchange in self._defi_exchanges
        if long_is_defi and short_is_defi:
            cross_mode = "defi"
        elif long_is_defi or short_is_defi:
            cross_mode = "defi"  # mixed uses DeFi-side thresholds (weaker leg)
        else:
            cross_mode = "cross_exchange"

        cross_score_params = {
            "cv": diff_hist.get("cv", 999),
            "min_ratio": diff_hist.get("min_ratio", 0),
            "streak": diff_hist.get("streak", 0),
            "pct": diff_hist.get("consistency_pct", 0),
            "volume": min(long_fr.volume_24h, short_fr.volume_24h),
            "settlement_avg": settlement_avg,
            "payments_per_day": (long_ppd + short_ppd) / 2,
            "fee_drag": fee_ratio,
            "current_rate": differential,
            "rates": rates_for_indicators,
            "mode": cross_mode,
        }
        sc = opportunity_score(cross_score_params)
        display_score, score_heuristic, model_pred = self._resolve_score(
            sc, cross_score_params)

        liq_risk = "LOW"
        if NOTIONAL < 2000:
            liq_risk = "HIGH"
        elif NOTIONAL < 5000:
            liq_risk = "MEDIUM"

        grade = grade_from_net_apr(model_pred) if model_pred is not None else stability_grade(display_score)

        # Estimated hold days based on differential history
        est_days = 0
        streak_d = diff_hist.get("streak", 0)
        cons_pct = diff_hist.get("consistency_pct", 0)
        if cons_pct >= 90 and streak_d >= 5:
            est_days = min(int(streak_d * 0.7), 14)
        elif cons_pct >= 80 and streak_d >= 3:
            est_days = min(int(streak_d * 0.5), 10)
        elif cons_pct >= 70 and streak_d >= 2:
            est_days = min(int(streak_d * 0.4), 7)
        elif streak_d >= 1:
            est_days = max(1, int(streak_d * 0.3))

        return CrossExchangeOpportunity(
            symbol=symbol,
            long_exchange=long_fr.exchange,
            short_exchange=short_fr.exchange,
            long_rate=long_fr.rate,
            short_rate=short_fr.rate,
            rate_differential=differential,
            long_price=long_fr.price,
            short_price=short_fr.price,
            long_interval_hours=long_fr.interval_hours,
            short_interval_hours=short_fr.interval_hours,
            long_ppd=long_ppd,
            short_ppd=short_ppd,
            accumulated_3d_pct=accumulated_3d * 100,
            apr=apr,
            daily_income_per_1000=daily_income,
            net_3d_revenue_per_1000=net_3d,
            total_fees=fees["total_cost"],
            break_even_hours=break_even_h,
            score=display_score,
            score_heuristic=score_heuristic,
            model_prediction=model_pred,
            liquidation_risk=liq_risk,
            mins_to_next=min(long_fr.mins_to_next, short_fr.mins_to_next),
            next_funding_ts=min(long_fr.next_funding_ts, short_fr.next_funding_ts),
            stability_grade=grade,
            estimated_hold_days=est_days,
            volume_24h=max(long_fr.volume_24h, short_fr.volume_24h),
            history=diff_hist,
            indicators=cross_score_params.get("_indicators", {}),
        )

    @staticmethod
    def _analyze_differential_history(
        long_hist, short_hist, long_ppd: float, short_ppd: float
    ) -> dict:
        """Analyze the historical differential between two exchanges.

        Groups rates into daily buckets by timestamp, then computes:
        - consistency_pct: % of days where differential > 0
        - streak: consecutive recent favorable days
        - diff_series: list of daily net differentials (chronological)
        - avg_diff: average daily differential
        - stddev_diff: std deviation of daily differentials
        - cv: coefficient of variation (stddev/avg) — lower = more stable
        - min_ratio: min(favorable_diffs) / avg — how bad is the worst day
        - trend: ratio of recent avg vs older avg — >1 = improving
        """
        import math
        MS_PER_DAY = 86_400_000

        empty = {
            "consistency_pct": 0, "streak": 0, "diff_series": [],
            "period_diffs": [],
            "avg_diff": 0, "stddev_diff": 0, "cv": 999,
            "min_ratio": 0, "trend": 1.0,
        }

        def _bucket_by_day(rates, timestamps):
            buckets = {}
            for rate, ts in zip(rates, timestamps):
                day = ts // MS_PER_DAY
                buckets.setdefault(day, []).append(rate)
            return buckets

        long_days = _bucket_by_day(long_hist.rates, long_hist.timestamps)
        short_days = _bucket_by_day(short_hist.rates, short_hist.timestamps)

        common_days = sorted(set(long_days) & set(short_days))
        if not common_days:
            return empty

        # Build daily differential series
        diff_series = []
        for day in common_days:
            short_sum = sum(short_days[day])
            long_sum = sum(long_days[day])
            diff_series.append(short_sum - long_sum)

        # Per-period differential series: pair each short-side rate with the
        # long-side rate that was active at the same timestamp (nearest match
        # within one bucket). Produces many more samples than the daily
        # aggregate, so indicators.py can compute momentum/z-score/percentile.
        long_pairs = sorted(zip(long_hist.timestamps, long_hist.rates))
        short_pairs = sorted(zip(short_hist.timestamps, short_hist.rates))
        period_diffs = []
        if long_pairs and short_pairs:
            li = 0
            for ts, sr in short_pairs:
                while li + 1 < len(long_pairs) and long_pairs[li + 1][0] <= ts:
                    li += 1
                period_diffs.append(sr - long_pairs[li][1])

        n = len(diff_series)

        # Consistency: % of days where differential > 0
        favorable_diffs = [d for d in diff_series if d > 0]
        favorable_count = len(favorable_diffs)
        consistency_pct = favorable_count / n * 100

        # Streak: consecutive favorable days from most recent
        streak = 0
        for d in reversed(diff_series):
            if d > 0:
                streak += 1
            else:
                break

        # Stability: avg, stddev, CV of the differential
        avg_diff = sum(diff_series) / n
        variance = sum((d - avg_diff) ** 2 for d in diff_series) / n
        stddev_diff = math.sqrt(variance)
        cv = stddev_diff / abs(avg_diff) if abs(avg_diff) > 1e-10 else 999

        # Min ratio: worst favorable day / average
        min_ratio = 0
        if favorable_diffs and abs(avg_diff) > 1e-10:
            min_ratio = min(favorable_diffs) / abs(avg_diff)

        # Trend: compare recent half vs older half
        trend = 1.0
        if n >= 4:
            mid = n // 2
            older_avg = sum(abs(d) for d in diff_series[:mid]) / mid
            recent_avg = sum(abs(d) for d in diff_series[mid:]) / (n - mid)
            if older_avg > 1e-10:
                trend = recent_avg / older_avg

        return {
            "consistency_pct": consistency_pct,
            "streak": streak,
            "diff_series": diff_series,
            "period_diffs": period_diffs,
            "avg_diff": avg_diff,
            "stddev_diff": stddev_diff,
            "cv": cv,
            "min_ratio": min_ratio,
            "trend": trend,
        }
