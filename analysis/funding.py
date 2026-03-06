"""Funding rate aggregation and calculations."""
import logging

log = logging.getLogger("bot")


class FundingAggregator:
    def calculate_apr(self, rate: float, payments_per_day: float) -> float:
        """APR = |rate| * payments_per_day * 365 * 100"""
        return abs(rate) * payments_per_day * 365 * 100

    def calculate_3day_accumulated(self, rates: list,
                                   payments_per_day: float) -> float:
        """Sum the last 3 days of funding payments.
        For 8h intervals: last 9 payments.
        For 4h intervals: last 18 payments.
        For 1h intervals: last 72 payments.
        """
        n_payments = int(payments_per_day * 3)
        # Use most recent payments (end of list if chronological)
        recent = rates[-n_payments:] if len(rates) >= n_payments else rates
        return sum(recent)

    def calculate_3day_revenue_usd(self, accumulated_rate: float,
                                   notional: float) -> float:
        """Dollar revenue for a given notional over 3 days."""
        return notional * abs(accumulated_rate)

    def calculate_daily_income(self, rate: float, payments_per_day: float,
                               notional: float) -> float:
        """Daily funding income in USD for given notional."""
        return abs(rate) * payments_per_day * notional

    def aggregate_rates_by_symbol(self, all_rates: dict) -> dict:
        """Group funding rates by symbol across exchanges.
        Input: {exchange: [FundingRate, ...]}
        Output: {symbol: [FundingRate, ...]} (one per exchange)
        """
        by_symbol = {}
        for exchange, rates in all_rates.items():
            for fr in rates:
                if fr.symbol not in by_symbol:
                    by_symbol[fr.symbol] = []
                by_symbol[fr.symbol].append(fr)
        return by_symbol

    def rank_by_3day_revenue(self, opportunities: list) -> list:
        """Sort opportunities by net 3-day revenue descending."""
        return sorted(
            opportunities,
            key=lambda o: getattr(o, "net_3d_revenue_per_1000", 0),
            reverse=True,
        )
