"""Data models for the funding rate arbitrage bot."""
from dataclasses import dataclass, field
import time
import uuid


@dataclass
class FundingRate:
    symbol: str           # "BTC"
    pair: str             # "BTCUSDT"
    exchange: str         # "Binance"
    rate: float           # Current funding rate (e.g., 0.0003)
    price: float          # Mark price
    volume_24h: float     # Quote volume 24h
    interval_hours: int   # 1, 4, 8
    payments_per_day: float  # 24, 6, 3
    next_funding_ts: int = 0
    mins_to_next: float = -1

    @property
    def annualized_rate(self) -> float:
        return abs(self.rate) * self.payments_per_day * 365

    @property
    def daily_rate(self) -> float:
        return abs(self.rate) * self.payments_per_day

    def to_dict(self) -> dict:
        return {
            "symbol": self.symbol, "pair": self.pair, "exchange": self.exchange,
            "fr": self.rate, "price": self.price, "vol24h": self.volume_24h,
            "ih": self.interval_hours, "ipd": self.payments_per_day,
            "next_funding_ts": self.next_funding_ts,
            "mins_next": self.mins_to_next,
        }


@dataclass
class FundingHistory:
    symbol: str
    exchange: str
    rates: list = field(default_factory=list)
    timestamps: list = field(default_factory=list)
    avg: float = 0
    stddev: float = 999
    consistency_pct: float = 0
    streak: int = 0
    favorable_pct: float = 0

    def to_dict(self) -> dict:
        return {
            "avg": self.avg, "pct": self.favorable_pct, "streak": self.streak,
            "ok": self.favorable_pct >= 70 and self.streak >= 3,
            "stddev": self.stddev, "_rates": self.rates,
        }


@dataclass
class SpotPerpOpportunity:
    """Mode 1: Same-exchange spot+perp hedge."""
    symbol: str
    exchange: str
    funding_rate: float
    interval_hours: int
    payments_per_day: float
    price: float
    volume_24h: float
    accumulated_3d_pct: float    # 3-day accumulated funding (%)
    apr: float
    daily_income_per_1000: float
    net_3d_revenue_per_1000: float
    fees_total: float
    spread_cost: float = 0
    break_even_hours: float = 0
    score: int = 0
    has_spot: bool = True
    spot_volume: float = 0
    mins_to_next: float = -1
    next_funding_ts: int = 0
    history: dict = field(default_factory=dict)
    rsi: float = -1
    stability_grade: str = "D"
    estimated_hold_days: int = 0

    def to_dict(self) -> dict:
        return {
            "mode": "spot_perp",
            "symbol": self.symbol, "exchange": self.exchange,
            "funding_rate": self.funding_rate,
            "interval_hours": self.interval_hours,
            "payments_per_day": self.payments_per_day,
            "price": self.price, "volume_24h": self.volume_24h,
            "accumulated_3d_pct": self.accumulated_3d_pct,
            "apr": self.apr,
            "daily_income_per_1000": self.daily_income_per_1000,
            "net_3d_revenue_per_1000": self.net_3d_revenue_per_1000,
            "fees_total": self.fees_total,
            "break_even_hours": self.break_even_hours,
            "score": self.score, "has_spot": self.has_spot,
            "mins_to_next": self.mins_to_next,
            "next_funding_ts": self.next_funding_ts,
            "rsi": self.rsi,
            "stability_grade": self.stability_grade,
            "estimated_hold_days": self.estimated_hold_days,
        }


@dataclass
class CrossExchangeOpportunity:
    """Mode 2: Long Exchange A + Short Exchange B."""
    symbol: str
    long_exchange: str
    short_exchange: str
    long_rate: float
    short_rate: float
    rate_differential: float
    long_price: float
    short_price: float
    accumulated_3d_pct: float
    apr: float
    daily_income_per_1000: float
    net_3d_revenue_per_1000: float
    total_fees: float
    long_interval_hours: int = 8
    short_interval_hours: int = 8
    long_ppd: float = 3
    short_ppd: float = 3
    break_even_hours: float = 0
    score: int = 0
    margin_required_per_1000: float = 2000  # Need margin on both sides
    liquidation_risk: str = "MEDIUM"
    mins_to_next: float = -1
    next_funding_ts: int = 0
    stability_grade: str = "D"

    def to_dict(self) -> dict:
        return {
            "mode": "cross_exchange",
            "symbol": self.symbol,
            "long_exchange": self.long_exchange,
            "short_exchange": self.short_exchange,
            "long_rate": self.long_rate, "short_rate": self.short_rate,
            "rate_differential": self.rate_differential,
            "long_interval_hours": self.long_interval_hours,
            "short_interval_hours": self.short_interval_hours,
            "long_ppd": self.long_ppd,
            "short_ppd": self.short_ppd,
            "accumulated_3d_pct": self.accumulated_3d_pct,
            "apr": self.apr,
            "daily_income_per_1000": self.daily_income_per_1000,
            "net_3d_revenue_per_1000": self.net_3d_revenue_per_1000,
            "total_fees": self.total_fees,
            "break_even_hours": self.break_even_hours,
            "score": self.score,
            "margin_required_per_1000": self.margin_required_per_1000,
            "liquidation_risk": self.liquidation_risk,
            "mins_to_next": self.mins_to_next,
            "next_funding_ts": self.next_funding_ts,
            "stability_grade": self.stability_grade,
        }


@dataclass
class Position:
    symbol: str
    exchange: str
    entry_fr: float
    entry_price: float
    entry_time: int  # ms timestamp
    carry: str       # "Positive" or "Reverse"
    capital_used: float
    ih: int = 8
    sl_pct: float = 0
    earned_real: float = 0
    last_earn_update: float = 0
    last_fr_used: float = 0
    mode: str = "spot_perp"
    long_exchange: str = ""
    short_exchange: str = ""
    id: str = field(default_factory=lambda: str(uuid.uuid4())[:8])

    def to_dict(self) -> dict:
        return {
            "id": self.id, "symbol": self.symbol, "exchange": self.exchange,
            "entry_fr": self.entry_fr, "entry_price": self.entry_price,
            "entry_time": self.entry_time, "carry": self.carry,
            "capital_used": self.capital_used, "ih": self.ih,
            "sl_pct": self.sl_pct, "earned_real": self.earned_real,
            "last_earn_update": self.last_earn_update,
            "last_fr_used": self.last_fr_used,
            "mode": self.mode,
            "long_exchange": self.long_exchange,
            "short_exchange": self.short_exchange,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "Position":
        return cls(
            symbol=d["symbol"], exchange=d["exchange"],
            entry_fr=d["entry_fr"], entry_price=d["entry_price"],
            entry_time=d["entry_time"], carry=d["carry"],
            capital_used=d["capital_used"], ih=d.get("ih", 8),
            sl_pct=d.get("sl_pct", 0), earned_real=d.get("earned_real", 0),
            last_earn_update=d.get("last_earn_update", 0),
            last_fr_used=d.get("last_fr_used", 0),
            mode=d.get("mode", "spot_perp"),
            long_exchange=d.get("long_exchange", ""),
            short_exchange=d.get("short_exchange", ""),
            id=d.get("id", str(uuid.uuid4())[:8]),
        )
