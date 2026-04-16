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

    # Groq AI (free tier — Llama 3.3 70B) — multiple keys for rotation
    GROQ_API_KEY_1 = os.environ.get("GROQ_API_KEY_1", "")
    GROQ_API_KEY_2 = os.environ.get("GROQ_API_KEY_2", "")
    GROQ_API_KEY_3 = os.environ.get("GROQ_API_KEY_3", "")

    # App
    BOT_PASSWORD = os.environ.get("BOT_PASSWORD", "")
    DATA_DIR = os.environ.get("DATA_DIR", "/app/data")

    # Fee structure per exchange (maker/taker in %).
    # NEW shape exposes maker+taker for spot and futures; legacy `spot`/`fut`
    # keys are kept as aliases of the taker values so existing callers don't
    # break.  Values are sourced from exchanges' public fee pages at the base
    # (non-VIP) tier; they are overridden at runtime by analysis/fee_loader.py
    # which pulls real values via CCXT when available.
    FEES = {
        "Binance": {
            "spot_maker": 0.10, "spot_taker": 0.10,
            "fut_maker": 0.02, "fut_taker": 0.05,
            "spot": 0.10, "fut": 0.05,
        },
        "Bybit": {
            "spot_maker": 0.10, "spot_taker": 0.10,
            "fut_maker": 0.02, "fut_taker": 0.055,
            "spot": 0.10, "fut": 0.055,
        },
        "OKX": {
            "spot_maker": 0.08, "spot_taker": 0.10,
            "fut_maker": 0.02, "fut_taker": 0.05,
            "spot": 0.10, "fut": 0.05,
        },
        "Bitget": {
            "spot_maker": 0.10, "spot_taker": 0.10,
            "fut_maker": 0.02, "fut_taker": 0.06,
            "spot": 0.10, "fut": 0.06,
        },
        # DeFi exchanges (perp-only, no spot fees)
        "Hyperliquid": {
            "spot_maker": 0, "spot_taker": 0,
            "fut_maker": 0.01, "fut_taker": 0.035,
            "spot": 0, "fut": 0.035,
        },
        "GMX": {
            "spot_maker": 0, "spot_taker": 0,
            "fut_maker": 0.07, "fut_taker": 0.07,
            "spot": 0, "fut": 0.07,
        },
        "Aster": {
            "spot_maker": 0, "spot_taker": 0,
            "fut_maker": 0.02, "fut_taker": 0.05,
            "spot": 0, "fut": 0.05,
        },
        "Lighter": {
            "spot_maker": 0, "spot_taker": 0,
            "fut_maker": 0, "fut_taker": 0.04,
            "spot": 0, "fut": 0.04,
        },
        "Extended": {
            "spot_maker": 0, "spot_taker": 0,
            "fut_maker": 0.02, "fut_taker": 0.05,
            "spot": 0, "fut": 0.05,
        },
    }

    # DeFi exchanges
    DEFI_EXCHANGES = ["Hyperliquid", "GMX", "Aster", "Lighter", "Extended"]

    # SaaS / Multi-user
    USE_DB = os.environ.get("USE_DB", "false").lower() in ("true", "1", "yes")
    DATABASE_URL = os.environ.get("DATABASE_URL", "")
    SECRET_KEY = os.environ.get("SECRET_KEY", os.urandom(32).hex())
    FERNET_KEY = os.environ.get("FERNET_KEY", "")
    ADMIN_EMAIL = os.environ.get("ADMIN_EMAIL", "")

    # Email for magic links
    SENDGRID_API_KEY = os.environ.get("SENDGRID_API_KEY", "")
    SMTP_HOST = os.environ.get("SMTP_HOST", "")
    SMTP_PORT = int(os.environ.get("SMTP_PORT", "587"))
    SMTP_USER = os.environ.get("SMTP_USER", "")
    SMTP_PASS = os.environ.get("SMTP_PASS", "")
    MAIL_FROM = os.environ.get("MAIL_FROM", "noreply@fundingbot.app")
