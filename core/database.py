"""SQLAlchemy database setup for multi-user SaaS mode."""
import os
import logging
from flask_sqlalchemy import SQLAlchemy

log = logging.getLogger("bot.db")

db = SQLAlchemy()


def init_db(app):
    """Initialize SQLAlchemy with the Flask app."""
    database_url = os.environ.get("DATABASE_URL", "")

    if not database_url:
        log.warning("DATABASE_URL not set — DB features disabled")
        return False

    # Railway uses postgres:// but SQLAlchemy needs postgresql://
    if database_url.startswith("postgres://"):
        database_url = database_url.replace("postgres://", "postgresql://", 1)

    app.config["SQLALCHEMY_DATABASE_URI"] = database_url
    app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False
    app.config["SQLALCHEMY_ENGINE_OPTIONS"] = {
        "pool_size": 5,
        "pool_recycle": 300,
        "pool_pre_ping": True,
    }

    db.init_app(app)

    with app.app_context():
        # Import models so they are registered with SQLAlchemy
        from core import db_models  # noqa: F401
        db.create_all()

        # Auto-migrate: add columns that db.create_all() won't add to existing tables
        _run_migrations(db)
        log.info("Database initialized successfully")

    return True


def _run_migrations(db):
    """Add missing columns to existing tables.

    db.create_all() creates new tables but won't alter existing ones.
    This runs safe ALTER TABLE ADD COLUMN IF NOT EXISTS statements.
    """
    migrations = [
        "ALTER TABLE users ADD COLUMN IF NOT EXISTS password_hash VARCHAR(256)",
        "ALTER TABLE funding_rate_snapshots ADD COLUMN IF NOT EXISTS open_interest FLOAT",
        "ALTER TABLE user_positions ADD COLUMN IF NOT EXISTS leverage INTEGER DEFAULT 1",
        "ALTER TABLE user_positions ADD COLUMN IF NOT EXISTS exposure FLOAT DEFAULT 0",
        # Backfill exposure for existing positions (1x leverage: exposure = capital/2)
        "UPDATE user_positions SET exposure = capital_used / 2 WHERE exposure = 0 OR exposure IS NULL",
        # History: add exposure and leverage columns
        "ALTER TABLE user_history ADD COLUMN IF NOT EXISTS exposure FLOAT DEFAULT 0",
        "ALTER TABLE user_history ADD COLUMN IF NOT EXISTS leverage INTEGER DEFAULT 1",
        # Fee accounting v2: split round-trip entry_fees into entry + exit
        # halves and expose per-position overrides so the user can enter
        # the real fees once a position is open.
        "ALTER TABLE user_positions ADD COLUMN IF NOT EXISTS exit_fees_est FLOAT DEFAULT 0",
        "ALTER TABLE user_positions ADD COLUMN IF NOT EXISTS entry_fees_real FLOAT",
        "ALTER TABLE user_positions ADD COLUMN IF NOT EXISTS exit_fees_real FLOAT",
        # Telegram notification columns (replaces CallMeBot WhatsApp)
        "ALTER TABLE user_configs ADD COLUMN IF NOT EXISTS tg_chat_id VARCHAR(64) DEFAULT ''",
        "ALTER TABLE user_configs ADD COLUMN IF NOT EXISTS tg_bot_token_encrypted VARCHAR(512) DEFAULT ''",
        # One-shot backfill: legacy rows stored round-trip fees under
        # entry_fees.  Split them 50/50 so the new PnL helper (which adds
        # entry + exit) stays consistent with historical numbers.
        "UPDATE user_positions SET exit_fees_est = entry_fees / 2, "
        "entry_fees = entry_fees / 2 "
        "WHERE (exit_fees_est IS NULL OR exit_fees_est = 0) "
        "AND entry_fees > 0 AND entry_fees_real IS NULL",
        # Billing / subscription columns (provider-agnostic).
        # RENAMEs run first so a prior deploy's stripe_* column is preserved
        # rather than replaced by an empty provider_* column.
        "ALTER TABLE users RENAME COLUMN stripe_customer_id TO provider_customer_id",
        "ALTER TABLE users RENAME COLUMN stripe_subscription_id TO provider_subscription_id",
        "ALTER TABLE users ADD COLUMN IF NOT EXISTS plan VARCHAR(20) DEFAULT 'none'",
        "ALTER TABLE users ADD COLUMN IF NOT EXISTS plan_billing_period VARCHAR(10)",
        "ALTER TABLE users ADD COLUMN IF NOT EXISTS plan_expires_at TIMESTAMP",
        "ALTER TABLE users ADD COLUMN IF NOT EXISTS trial_ends_at TIMESTAMP",
        "ALTER TABLE users ADD COLUMN IF NOT EXISTS provider_customer_id VARCHAR(100)",
        "ALTER TABLE users ADD COLUMN IF NOT EXISTS provider_subscription_id VARCHAR(100)",
        "ALTER TABLE users ADD COLUMN IF NOT EXISTS customer_portal_url VARCHAR(512)",
        "ALTER TABLE users ADD COLUMN IF NOT EXISTS plan_override BOOLEAN DEFAULT FALSE",
        "ALTER TABLE users ADD COLUMN IF NOT EXISTS plan_override_note VARCHAR(255)",
    ]
    for sql in migrations:
        try:
            db.session.execute(db.text(sql))
            db.session.commit()
        except Exception as e:
            db.session.rollback()
            log.debug(f"Migration skipped (likely already applied): {e}")
