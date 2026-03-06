"""Core arbitrage opportunity detection — spot-perp and cross-exchange."""
import logging
import time
from core.models import FundingRate, SpotPerpOpportunity, CrossExchangeOpportunity
from analysis.funding import FundingAggregator
from analysis.fees import (calculate_spot_perp_fees, calculate_cross_exchange_fees,
                           calculate_break_even_hours)
from analysis.scoring import risk_score, score_cross_exchange

log = logging.getLogger("bot")

NOTIONAL = 1000  # Reference notional for comparison


class ArbitrageScanner:
    def __init__(self, exchange_manager, config):
        self.exchange_manager = exchange_manager
        self.config = config
        self.aggregator = FundingAggregator()

    def scan_spot_perp_opportunities(
        self, all_rates: dict, limit: int = 20
    ) -> list:
        """
        Mode 1: Same-exchange spot+perp hedge.
        Find symbols where funding rate > 0, spot available, and
        3-day revenue is attractive after fees.
        """
        if "spot_perp" not in self.config.ARBITRAGE_MODES:
            return []

        opportunities = []

        for exchange_name, rates in all_rates.items():
            # Filter to positive rates above threshold, with volume
            candidates = sorted(
                [r for r in rates
                 if r.rate > 0.0001 and r.volume_24h >= self.config.MIN_VOLUME],
                key=lambda r: r.rate, reverse=True
            )[:limit]

            for fr in candidates:
                try:
                    opp = self._analyze_spot_perp(fr)
                    if opp and opp.net_3d_revenue_per_1000 > 0:
                        opportunities.append(opp)
                except Exception as e:
                    log.warning(f"Spot-perp analysis error {fr.symbol}@{fr.exchange}: {e}")
                time.sleep(0.05)  # Rate limit

        # Sort by net 3-day revenue
        opportunities.sort(key=lambda o: o.net_3d_revenue_per_1000, reverse=True)
        return opportunities

    def _analyze_spot_perp(self, fr: FundingRate) -> SpotPerpOpportunity:
        """Analyze a single spot-perp opportunity."""
        # Fetch 3-day history
        n_payments_3d = int(fr.payments_per_day * 3)
        history_obj = self.exchange_manager.fetch_funding_history(
            fr.symbol, fr.exchange, limit=max(n_payments_3d + 5, 15)
        )

        # Calculate 3-day accumulated rate
        accumulated_3d = self.aggregator.calculate_3day_accumulated(
            history_obj.rates, fr.payments_per_day
        ) if history_obj.rates else fr.rate * n_payments_3d

        # Calculate fees
        fees = calculate_spot_perp_fees(
            fr.exchange, NOTIONAL, fr.volume_24h
        )

        # Revenue calculations
        revenue_3d_usd = NOTIONAL * abs(accumulated_3d)
        net_3d = revenue_3d_usd - fees["total_cost"]

        daily_income = self.aggregator.calculate_daily_income(
            fr.rate, fr.payments_per_day, NOTIONAL / 2  # futures side only
        )
        apr = self.aggregator.calculate_apr(fr.rate, fr.payments_per_day)

        # Break-even
        hourly_income = daily_income / 24
        break_even_h = calculate_break_even_hours(fees["total_cost"], hourly_income)

        # Score using v6.2 scoring on backward-compat dict
        token_dict = fr.to_dict()
        hist_dict = history_obj.to_dict()
        sc = risk_score(token_dict, hist_dict)

        # Check spot availability (cached)
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
            history=hist_dict,
        )

    def scan_cross_exchange_opportunities(
        self, all_rates: dict, limit: int = 15
    ) -> list:
        """
        Mode 2: Cross-exchange arbitrage.
        For each symbol on 2+ exchanges, find profitable differentials.
        """
        if "cross_exchange" not in self.config.ARBITRAGE_MODES:
            return []

        # Group by symbol
        by_symbol = self.aggregator.aggregate_rates_by_symbol(all_rates)

        opportunities = []
        checked = 0

        for symbol, rates_list in by_symbol.items():
            if len(rates_list) < 2:
                continue

            # Check minimum volume on at least two exchanges
            vol_rates = [r for r in rates_list
                         if r.volume_24h >= self.config.MIN_VOLUME]
            if len(vol_rates) < 2:
                continue

            # Find best long (lowest rate) and best short (highest rate)
            sorted_rates = sorted(vol_rates, key=lambda r: r.rate)
            long_candidate = sorted_rates[0]   # Lowest rate (pay less or receive)
            short_candidate = sorted_rates[-1]  # Highest rate (receive most)

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

        opportunities.sort(key=lambda o: o.net_3d_revenue_per_1000, reverse=True)
        return opportunities

    def _analyze_cross_exchange(
        self, symbol: str,
        long_fr: FundingRate, short_fr: FundingRate
    ) -> CrossExchangeOpportunity:
        """Analyze a single cross-exchange opportunity."""
        differential = short_fr.rate - long_fr.rate

        # Use average payments per day (may differ between exchanges)
        avg_ppd = (long_fr.payments_per_day + short_fr.payments_per_day) / 2

        # 3-day accumulated differential
        # Fetch history for both sides
        n_payments = int(avg_ppd * 3)
        long_hist = self.exchange_manager.fetch_funding_history(
            symbol, long_fr.exchange, limit=max(n_payments + 5, 15)
        )
        short_hist = self.exchange_manager.fetch_funding_history(
            symbol, short_fr.exchange, limit=max(n_payments + 5, 15)
        )

        # Calculate accumulated differential over 3 days
        if long_hist.rates and short_hist.rates:
            # Use min length available
            n = min(len(long_hist.rates), len(short_hist.rates), n_payments)
            long_sum = sum(long_hist.rates[-n:])
            short_sum = sum(short_hist.rates[-n:])
            accumulated_3d = short_sum - long_sum
        else:
            accumulated_3d = differential * n_payments

        # Fees (both sides)
        fees = calculate_cross_exchange_fees(
            long_fr.exchange, short_fr.exchange,
            NOTIONAL, min(long_fr.volume_24h, short_fr.volume_24h)
        )

        # Revenue
        revenue_3d_usd = NOTIONAL / 2 * abs(accumulated_3d)
        net_3d = revenue_3d_usd - fees["total_cost"]

        daily_income = abs(differential) * avg_ppd * (NOTIONAL / 2)
        apr = abs(differential) * avg_ppd * 365 * 100

        # Break-even
        hourly_income = daily_income / 24
        break_even_h = calculate_break_even_hours(fees["total_cost"], hourly_income)

        # Consistency of differential
        consistency = 0
        if long_hist.rates and short_hist.rates:
            n_check = min(len(long_hist.rates), len(short_hist.rates))
            favorable = sum(
                1 for i in range(n_check)
                if short_hist.rates[-(i+1)] - long_hist.rates[-(i+1)] > 0
            )
            consistency = (favorable / n_check * 100) if n_check > 0 else 0

        # Score
        fee_ratio = fees["total_cost"] / revenue_3d_usd if revenue_3d_usd > 0 else 1
        sc = score_cross_exchange(
            differential, consistency,
            min(long_fr.volume_24h, short_fr.volume_24h),
            fee_ratio,
        )

        # Liquidation risk assessment
        liq_risk = "LOW"
        if NOTIONAL < 2000:
            liq_risk = "HIGH"  # Small capital = high liq risk on cross-ex
        elif NOTIONAL < 5000:
            liq_risk = "MEDIUM"

        return CrossExchangeOpportunity(
            symbol=symbol,
            long_exchange=long_fr.exchange,
            short_exchange=short_fr.exchange,
            long_rate=long_fr.rate,
            short_rate=short_fr.rate,
            rate_differential=differential,
            long_price=long_fr.price,
            short_price=short_fr.price,
            accumulated_3d_pct=accumulated_3d * 100,
            apr=apr,
            daily_income_per_1000=daily_income,
            net_3d_revenue_per_1000=net_3d,
            total_fees=fees["total_cost"],
            break_even_hours=break_even_h,
            score=sc,
            liquidation_risk=liq_risk,
            mins_to_next=min(long_fr.mins_to_next, short_fr.mins_to_next),
        )
