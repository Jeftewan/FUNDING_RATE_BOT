"""Core arbitrage opportunity detection — spot-perp and cross-exchange."""
import logging
import time
from core.models import FundingRate, SpotPerpOpportunity, CrossExchangeOpportunity
from analysis.funding import FundingAggregator
from analysis.fees import (calculate_spot_perp_fees, calculate_cross_exchange_fees,
                           calculate_break_even_hours)
from analysis.scoring import risk_score, score_cross_exchange, stability_grade, estimated_hold_days

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

        token_dict = fr.to_dict()
        hist_dict = history_obj.to_dict()
        sc = risk_score(token_dict, hist_dict)
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

        # Consistency: align by timestamp into daily buckets, then check
        # if net daily rate (short_sum*short_ppd - long_sum*long_ppd) > 0
        # per day.  This correctly handles different payment intervals.
        consistency = 0
        if (long_hist.rates and long_hist.timestamps
                and short_hist.rates and short_hist.timestamps):
            consistency = self._calc_time_aligned_consistency(
                long_hist, short_hist, long_ppd, short_ppd
            )

        fee_ratio = fees["total_cost"] / revenue_3d_usd if revenue_3d_usd > 0 else 1
        sc = score_cross_exchange(
            differential, consistency,
            min(long_fr.volume_24h, short_fr.volume_24h),
            fee_ratio,
        )

        liq_risk = "LOW"
        if NOTIONAL < 2000:
            liq_risk = "HIGH"
        elif NOTIONAL < 5000:
            liq_risk = "MEDIUM"

        grade = stability_grade(sc)

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
            volume_24h=max(long_fr.volume_24h, short_fr.volume_24h),
        )

    @staticmethod
    def _calc_time_aligned_consistency(
        long_hist, short_hist, long_ppd: float, short_ppd: float
    ) -> float:
        """Calculate consistency by grouping rates into daily buckets.

        Instead of comparing rates by array index (wrong when intervals
        differ), we bucket each side's payments by calendar day (UTC) and
        compute the net daily rate:
            net = sum(short_rates_that_day) - sum(long_rates_that_day)
        A day is "favorable" when net > 0.
        Only days where BOTH sides have data are evaluated.
        """
        MS_PER_DAY = 86_400_000

        def _bucket_by_day(rates, timestamps):
            """Group rates by day (ts // MS_PER_DAY)."""
            buckets = {}
            for rate, ts in zip(rates, timestamps):
                day = ts // MS_PER_DAY
                buckets.setdefault(day, []).append(rate)
            return buckets

        long_days = _bucket_by_day(long_hist.rates, long_hist.timestamps)
        short_days = _bucket_by_day(short_hist.rates, short_hist.timestamps)

        common_days = sorted(set(long_days) & set(short_days))
        if not common_days:
            return 0

        favorable = 0
        for day in common_days:
            short_sum = sum(short_days[day])
            long_sum = sum(long_days[day])
            if short_sum - long_sum > 0:
                favorable += 1

        return favorable / len(common_days) * 100
