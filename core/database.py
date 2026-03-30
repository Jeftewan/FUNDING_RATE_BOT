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
    ]
    for sql in migrations:
        try:
            db.session.execute(db.text(sql))
        except Exception as e:
            log.debug(f"Migration skipped (likely already applied): {e}")
    db.session.commit()
