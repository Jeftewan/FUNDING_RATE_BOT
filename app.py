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

def _auto_migrate_json_to_db(app, state_mgr, config):
    """Auto-migrate positions/history from JSON to DB on startup.

    Runs once: if there are positions or history in the JSON state AND
    an ADMIN_EMAIL is set, copy them to the admin user's DB records,
    then clear them from the JSON state so they don't leak to all users.
    """
    with state_mgr.lock:
        s = state_mgr.state
        positions = s.get("positions", [])
        history = s.get("history", [])

    if not positions and not history:
        return  # Nothing to migrate

    admin_email = config.ADMIN_EMAIL
    if not admin_email:
        log.warning("JSON has positions/history but ADMIN_EMAIL not set — skipping auto-migration")
        return

    try:
        with app.app_context():
            from core.database import db
            from core.db_models import User, UserConfig, UserPosition, UserHistory
            from werkzeug.security import generate_password_hash

            user = User.query.filter_by(email=admin_email).first()
            if not user:
                log.info(f"Auto-migrate: admin user {admin_email} not found — skipping")
                return

            # Migrate positions
            migrated_pos = 0
            for pos in positions:
                existing = UserPosition.query.filter_by(
                    user_id=user.id, symbol=pos.get("symbol", ""),
                    entry_time=pos.get("entry_time", 0)).first()
                if existing:
                    continue
                db_pos = UserPosition(
                    user_id=user.id,
                    symbol=pos.get("symbol", ""),
                    exchange=pos.get("exchange", ""),
                    mode=pos.get("mode", "spot_perp"),
                    entry_fr=pos.get("entry_fr", 0),
                    entry_price=pos.get("entry_price", 0),
                    entry_time=pos.get("entry_time", 0),
                    capital_used=pos.get("capital_used", 0),
                    ih=pos.get("ih", 8),
                    earned_real=pos.get("earned_real", 0),
                    last_earn_update=pos.get("last_earn_update", 0),
                    last_fr_used=pos.get("last_fr_used", 0),
                    long_exchange=pos.get("long_exchange", ""),
                    short_exchange=pos.get("short_exchange", ""),
                    payment_count=pos.get("payment_count", 0),
                    avg_rate=pos.get("avg_rate", 0),
                    status="active",
                    entry_fees=pos.get("entry_fees", 0),
                    payments_json=pos.get("payments", []),
                )
                db.session.add(db_pos)
                migrated_pos += 1

            # Migrate history
            migrated_hist = 0
            for h in history:
                existing = UserHistory.query.filter_by(
                    user_id=user.id, symbol=h.get("symbol", ""),
                    earned=h.get("earned", 0)).first()
                if existing:
                    continue
                db_hist = UserHistory(
                    user_id=user.id,
                    symbol=h.get("symbol", ""),
                    exchange=h.get("exchange", ""),
                    mode=h.get("mode", "spot_perp"),
                    capital_used=h.get("capital_used", 0),
                    hours=h.get("hours", 0),
                    payment_count=h.get("payment_count", h.get("intervals", 0)),
                    earned=h.get("earned", 0),
                    fees=h.get("fees", 0),
                    net_earned=h.get("net_earned", h.get("earned", 0)),
                    avg_rate=h.get("avg_rate", 0),
                    reason=h.get("reason", ""),
                )
                db.session.add(db_hist)
                migrated_hist += 1

            # Migrate config
            user_config = UserConfig.query.filter_by(user_id=user.id).first()
            if user_config:
                with state_mgr.lock:
                    s = state_mgr.state
                    user_config.total_capital = s.get("total_capital", 1000)
                    user_config.scan_interval = s.get("scan_interval", 300)
                    user_config.min_volume = s.get("min_volume", 1000000)
                    user_config.min_apr = s.get("min_apr", 10)
                    user_config.min_score = s.get("min_score", 40)
                    user_config.max_positions = s.get("max_positions", 5)
                    user_config.email_enabled = s.get("email_enabled", False)
                    user_config.wa_phone = s.get("wa_phone", "")

            db.session.commit()

            # Clear positions/history from JSON state so they don't show to all users
            with state_mgr.lock:
                s = state_mgr.state
                s["positions"] = []
                s["history"] = []
                s["total_earned"] = 0
                state_mgr.save()

            log.info(f"Auto-migrated to DB ({admin_email}): "
                     f"{migrated_pos} positions, {migrated_hist} history entries — JSON cleared")

    except Exception as e:
        log.error(f"Auto-migration failed: {e}")


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

            # Auto-migrate JSON → DB if positions/history exist in JSON
            _auto_migrate_json_to_db(app, state_manager, Config)

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
