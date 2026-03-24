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
        log.info("Database initialized successfully")

    return True
