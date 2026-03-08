"""Centralized configuration from environment variables."""
import os


class Config:
    # Scanning defaults (overridden by state from frontend)
    SCAN_INTERVAL_MINUTES = int(os.environ.get("SCAN_MINUTES", "5"))
    MIN_VOLUME = float(os.environ.get("MIN_VOLUME", "1000000"))

    # Arbitrage v7/v8
    ARBITRAGE_MODES = os.environ.get("ARBITRAGE_MODES", "spot_perp,cross_exchange").split(",")
    ENABLED_EXCHANGES = os.environ.get("ENABLED_EXCHANGES", "binance,bybit,okx,bitget").split(",")
    MIN_3DAY_REVENUE_PCT = float(os.environ.get("MIN_3DAY_REVENUE_PCT", "0.03"))
    MIN_FUNDING_DIFFERENTIAL = float(os.environ.get("MIN_FUNDING_DIFFERENTIAL", "0.0002"))
    MAX_LEVERAGE = int(os.environ.get("MAX_LEVERAGE", "1"))

    # Exchange API keys (optional — public data only if empty)
    BINANCE_API_KEY = os.environ.get("BINANCE_API_KEY", "")
    BINANCE_API_SECRET = os.environ.get("BINANCE_API_SECRET", "")
    BYBIT_API_KEY = os.environ.get("BYBIT_API_KEY", "")
    BYBIT_API_SECRET = os.environ.get("BYBIT_API_SECRET", "")
    OKX_API_KEY = os.environ.get("OKX_API_KEY", "")
    OKX_API_SECRET = os.environ.get("OKX_API_SECRET", "")
    OKX_PASSPHRASE = os.environ.get("OKX_PASSPHRASE", "")
    BITGET_API_KEY = os.environ.get("BITGET_API_KEY", "")
    BITGET_API_SECRET = os.environ.get("BITGET_API_SECRET", "")
    BITGET_PASSPHRASE = os.environ.get("BITGET_PASSPHRASE", "")

    # Coinglass
    COINGLASS_API_KEY = os.environ.get("COINGLASS_API_KEY", "")

    # App
    BOT_PASSWORD = os.environ.get("BOT_PASSWORD", "")
    DATA_DIR = os.environ.get("DATA_DIR", "/app/data")

    # Fee structure per exchange (maker/taker in %)
    FEES = {
        "Binance": {"spot": 0.10, "fut": 0.05},
        "Bybit": {"spot": 0.10, "fut": 0.06},
        "OKX": {"spot": 0.10, "fut": 0.05},
        "Bitget": {"spot": 0.10, "fut": 0.06},
    }
