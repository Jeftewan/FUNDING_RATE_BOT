#!/usr/bin/env python3
"""
Funding Rate Arbitrage Bot v10.0
4 CEX (Binance, Bybit, OKX, Bitget) + 5 DeFi via CCXT
Multi-user SaaS with PostgreSQL + magic link auth
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
app.secret_key = Config.SECRET_KEY

# ── Database + Auth (SaaS mode) ──────────────────────────────
db_enabled = False
if Config.USE_DB and Config.DATABASE_URL:
    try:
        from core.database import init_db
        db_enabled = init_db(app)
        if db_enabled:
            from flask_login import LoginManager
            from core.db_models import User

            login_manager = LoginManager()
            login_manager.init_app(app)
            login_manager.login_view = "auth.login_page"  # auth/page

            @login_manager.user_loader
            def load_user(user_id):
                return User.query.get(int(user_id))

            @login_manager.unauthorized_handler
            def unauthorized():
                from flask import request as req, jsonify as jfy, redirect as rdr
                if req.path.startswith("/api/"):
                    return jfy({"ok": False, "msg": "No autenticado"}), 401
                return rdr("/auth/page")

            from auth.routes import init_auth, auth_bp

            # Login page (GET only — POST /auth/login is the API endpoint)
            @auth_bp.route("/page")
            def login_page():
                from flask import render_template
                return render_template("login.html", error=None)

            init_auth(app, Config)
            log.info("SaaS mode enabled: PostgreSQL + email/password auth")
    except Exception as e:
        log.error(f"Failed to initialize SaaS mode: {e}")
        db_enabled = False

if not db_enabled:
    log.info("Running in single-user mode (JSON persistence)")

# Give scanner access to flask app for rate snapshot storage
if db_enabled:
    scanner_worker._flask_app = app

# ── API Routes ────────────────────────────────────────────────
from api.routes import init_routes
init_routes(app, state_manager, scanner_worker, Config,
            defi_manager=defi_manager, db_enabled=db_enabled)

s = state_manager.state
mode = "SaaS" if db_enabled else "Single-user"
log.info(
    f"Bot v10.0 [{mode}]: ${s['total_capital']:,.0f} | "
    f"{s['scan_interval']//60}min | "
    f"Exchanges: {', '.join(Config.ENABLED_EXCHANGES)} | "
    f"{Config.DATA_DIR}"
)

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False)
