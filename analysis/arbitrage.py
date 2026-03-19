"""Core arbitrage opportunity detection — spot-perp and cross-exchange."""
import logging
import time
from core.models import FundingRate, SpotPerpOpportunity, CrossExchangeOpportunity
from analysis.funding import FundingAggregator
from analysis.fees import (calculate_spot_perp_fees, calculate_cross_exchange_fees,
                           calculate_break_even_hours)
from analysis.scoring import opportunity_score, stability_grade, estimated_hold_days

log = logging.getLogger("bot")

NOTIONAL = 1000  # Reference notional for comparison


class ArbitrageScanner:
    def __init__(self, exchange_manager, config):
        self.exchange_manager = exchange_manager
        self.config = config
        self.aggregator = FundingAggregator()

    def scan_spot_perp_opportunities(
        self, all_rates: dict, min_volume: float = None, limit: int = 20
    ) -> list:
        """
        Mode 1: Same-exchange spot+perp hedge.
        Find symbols where funding rate > 0, spot available, and
        3-day revenue is attractive after fees.
        """
        if "spot_perp" not in self.config.ARBITRAGE_MODES:
            return []

        mv = min_volume or self.config.MIN_VOLUME
        opportunities = []

        for exchange_name, rates in all_rates.items():
            candidates = sorted(
                [r for r in rates
                 if r.rate > 0.0001 and r.volume_24h >= mv],
                key=lambda r: r.rate, reverse=True
            )[:limit]

            for fr in candidates:
                try:
                    opp = self._analyze_spot_perp(fr)
                    if opp and opp.net_3d_revenue_per_1000 > 0:
                        opportunities.append(opp)
                except Exception as e:
                    log.warning(f"Spot-perp analysis error {fr.symbol}@{fr.exchange}: {e}")
                time.sleep(0.05)

        opportunities.sort(key=lambda o: o.score, reverse=True)
        return opportunities

    def _analyze_spot_perp(self, fr: FundingRate) -> SpotPerpOpportunity:
        """Analyze a single spot-perp opportunity."""
        n_payments_3d = int(fr.payments_per_day * 3)
        history_obj = self.exchange_manager.fetch_funding_history(
            fr.symbol, fr.exchange, limit=max(n_payments_3d + 5, 15)
        )

        accumulated_3d = self.aggregator.calculate_3day_accumulated(
            history_obj.rates, fr.payments_per_day
        ) if history_obj.rates else fr.rate * n_payments_3d

        fees = calculate_spot_perp_fees(
            fr.exchange, NOTIONAL, fr.volume_24h
        )

        revenue_3d_usd = NOTIONAL * abs(accumulated_3d)
        net_3d = revenue_3d_usd - fees["total_cost"]

        daily_income = self.aggregator.calculate_daily_income(
            fr.rate, fr.payments_per_day, NOTIONAL / 2
        )
        apr = self.aggregator.calculate_apr(fr.rate, fr.payments_per_day)

        hourly_income = daily_income / 24
        break_even_h = calculate_break_even_hours(fees["total_cost"], hourly_income)

        hist_dict = history_obj.to_dict()
        rates = hist_dict.get("_rates", [])
        avg_abs = history_obj.avg if history_obj.avg else abs(fr.rate)
        stddev = history_obj.stddev if history_obj.stddev < 999 else 0
        cv = stddev / abs(avg_abs) if abs(avg_abs) > 1e-10 else 999

        # Favorable rates for min_ratio
        favorable = [abs(r) for r in rates
                     if (fr.rate > 0 and r > 0) or (fr.rate < 0 and r < 0)]
        min_ratio = min(favorable) / abs(avg_abs) if favorable and abs(avg_abs) > 1e-10 else 0

        # Settlement-based yield: avg of actual historical settlement rates
        settlement_avg = sum(abs(r) for r in rates) / len(rates) if rates else abs(fr.rate)

        # Fee drag
        gross_3d_usd = NOTIONAL * abs(accumulated_3d) if abs(accumulated_3d) > 0 else 1
        fee_drag = fees["total_cost"] / gross_3d_usd

        sc = opportunity_score({
            "cv": cv,
            "min_ratio": min_ratio,
            "streak": history_obj.streak,
            "pct": history_obj.favorable_pct,
            "volume": fr.volume_24h,
            "settlement_avg": settlement_avg,
            "payments_per_day": fr.payments_per_day,
            "fee_drag": fee_drag,
            "current_rate": fr.rate,
            "rates": rates,
        })
        grade = stability_grade(sc)
        est_days = estimated_hold_days(hist_dict)

        has_spot = self.exchange_manager.fetch_spot_availability(
            fr.symbol, fr.exchange
        )

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
            score=sc,
            has_spot=has_spot,
            mins_to_next=fr.mins_to_next,
            next_funding_ts=fr.next_funding_ts,
            history=hist_dict,
            stability_grade=grade,
            estimated_hold_days=est_days,
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

            vol_rates = [r for r in rates_list if r.volume_24h >= mv]
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
        long_hist = self.exchange_manager.fetch_funding_history(
            symbol, long_fr.exchange, limit=max(n_long_3d + 5, 15)
        )
        short_hist = self.exchange_manager.fetch_funding_history(
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
                     "min_ratio": 0, "trend": 1.0, "diff_series": []}
        if (long_hist.rates and long_hist.timestamps
                and short_hist.rates and short_hist.timestamps):
            diff_hist = self._analyze_differential_history(
                long_hist, short_hist, long_ppd, short_ppd
            )

        fee_ratio = fees["total_cost"] / revenue_3d_usd if revenue_3d_usd > 0 else 1
        diff_series = diff_hist.get("diff_series", [])

        # Settlement-based yield: avg of historical daily differentials
        settlement_avg = (abs(diff_hist.get("avg_diff", 0))
                          if diff_series else abs(differential))

        sc = opportunity_score({
            "cv": diff_hist.get("cv", 999),
            "min_ratio": diff_hist.get("min_ratio", 0),
            "streak": diff_hist.get("streak", 0),
            "pct": diff_hist.get("consistency_pct", 0),
            "volume": min(long_fr.volume_24h, short_fr.volume_24h),
            "settlement_avg": settlement_avg,
            "payments_per_day": (long_ppd + short_ppd) / 2,
            "fee_drag": fee_ratio,
            "current_rate": differential,
            "rates": diff_series,
        })

        liq_risk = "LOW"
        if NOTIONAL < 2000:
            liq_risk = "HIGH"
        elif NOTIONAL < 5000:
            liq_risk = "MEDIUM"

        grade = stability_grade(sc)

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
            score=sc,
            liquidation_risk=liq_risk,
            mins_to_next=min(long_fr.mins_to_next, short_fr.mins_to_next),
            next_funding_ts=min(long_fr.next_funding_ts, short_fr.next_funding_ts),
            stability_grade=grade,
            estimated_hold_days=est_days,
            volume_24h=max(long_fr.volume_24h, short_fr.volume_24h),
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
            "avg_diff": avg_diff,
            "stddev_diff": stddev_diff,
            "cv": cv,
            "min_ratio": min_ratio,
            "trend": trend,
        }
