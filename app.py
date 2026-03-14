#!/usr/bin/env python3
"""
Funding Rate Arbitrage Bot v8.0
4 Exchanges (Binance, Bybit, OKX, Bitget) via CCXT
Unified opportunity scanner — no safe/aggressive split
User-driven position management with real payment tracking
"""
import os
import logging
from flask import Flask

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger("bot")

# ── Configuration ──────────────────────────────────────────────
from config import Config

if not os.path.exists(Config.DATA_DIR):
    try:
        os.makedirs(Config.DATA_DIR, exist_ok=True)
    except OSError:
        Config.DATA_DIR = "."

# ── Core components ────────────────────────────────────────────
from core.persistence import JSONPersistence
from core.state import StateManager

persistence = JSONPersistence(os.path.join(Config.DATA_DIR, "portfolio_state.json"))
state_manager = StateManager(persistence)
state_manager.load()

# Apply initial config if first run
if state_manager.get("scan_count") == 0:
    state_manager.update(
        total_capital=float(os.environ.get("CAPITAL", "1000")),
        scan_interval=Config.SCAN_INTERVAL_MINUTES * 60,
        min_volume=Config.MIN_VOLUME,
    )

# ── Exchange Manager (CCXT) ───────────────────────────────────
from exchanges.manager import ExchangeManager

exchange_manager = ExchangeManager(Config)

# ── DeFi Exchange Manager ─────────────────────────────────────
from exchanges.defi_manager import DefiExchangeManager

defi_manager = DefiExchangeManager(Config)

# ── Arbitrage Scanner ─────────────────────────────────────────
from analysis.arbitrage import ArbitrageScanner

arb_scanner = ArbitrageScanner(exchange_manager, Config)

# ── Coinglass Client (optional) ───────────────────────────────
from coinglass.client import CoinglassClient

coinglass_client = None
if Config.COINGLASS_API_KEY:
    coinglass_client = CoinglassClient(Config.COINGLASS_API_KEY)
    log.info("Coinglass API configured")

# ── Email Notifications ───────────────────────────────────────
from notifications.email import EmailNotifier

email_notifier = EmailNotifier(state_manager)

# ── Scanner Worker ────────────────────────────────────────────
from scanner.worker import ScannerWorker

scanner_worker = ScannerWorker(
    exchange_manager, arb_scanner, state_manager, coinglass_client, Config,
    email_notifier=email_notifier, defi_manager=defi_manager,
)

# ── Flask App ─────────────────────────────────────────────────
app = Flask(__name__,
            template_folder="templates",
            static_folder="static")

from api.routes import init_routes
init_routes(app, state_manager, scanner_worker, Config, defi_manager=defi_manager)

s = state_manager.state
log.info(
    f"Bot v8.0: ${s['total_capital']:,.0f} | "
    f"{s['scan_interval']//60}min | "
    f"Exchanges: {', '.join(Config.ENABLED_EXCHANGES)} | "
    f"{Config.DATA_DIR}"
)

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False)
