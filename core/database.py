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

    # Railway uses postgres:// / postgresql://. Force the psycopg2 driver
    # explicitly: SQLAlchemy 2.1 changed the default to psycopg (v3).
    for prefix in ("postgres://", "postgresql://"):
        if database_url.startswith(prefix):
            database_url = "postgresql+psycopg2://" + database_url[len(prefix):]
            break

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
        # Terms of Service acceptance timestamp (legal audit)
        "ALTER TABLE users ADD COLUMN IF NOT EXISTS terms_accepted_at TIMESTAMP",
        # One-shot backfill: legacy rows stored round-trip fees under
        # entry_fees.  Split them 50/50 so the new PnL helper (which adds
        # entry + exit) stays consistent with historical numbers.
        "UPDATE user_positions SET exit_fees_est = entry_fees / 2, "
        "entry_fees = entry_fees / 2 "
        "WHERE (exit_fees_est IS NULL OR exit_fees_est = 0) "
        "AND entry_fees > 0 AND entry_fees_real IS NULL",
        # Dedup log for Telegram notifications.  Survives process restarts so
        # alerts already sent within the last few hours are not re-sent when
        # Railway redeploys.
        "CREATE TABLE IF NOT EXISTS notification_log ("
        "  id SERIAL PRIMARY KEY,"
        "  user_id INTEGER NOT NULL,"
        "  dedup_key VARCHAR(256) NOT NULL,"
        "  sent_at TIMESTAMP NOT NULL DEFAULT NOW()"
        ")",
        "CREATE UNIQUE INDEX IF NOT EXISTS uq_notification_log_user_key "
        "ON notification_log (user_id, dedup_key)",
        "CREATE INDEX IF NOT EXISTS idx_notification_log_sent_at "
        "ON notification_log (sent_at)",
        # Retention: keep 7 days, drop the rest each boot.
        "DELETE FROM notification_log WHERE sent_at < NOW() - INTERVAL '7 days'",
        # Manual edit audit trail on closed positions.
        "ALTER TABLE user_history ADD COLUMN IF NOT EXISTS notes TEXT",
        # Auto-execution flag: position opened via real API orders (vs manual
        # bookkeeping). Drives the AUTO badge in the UI.
        "ALTER TABLE user_positions ADD COLUMN IF NOT EXISTS auto_executed BOOLEAN DEFAULT FALSE",
        # Opportunity-list exchange filter (CSV of exchange names, "" = all).
        "ALTER TABLE user_configs ADD COLUMN IF NOT EXISTS allowed_exchanges VARCHAR(256) DEFAULT ''",
        # Filtro por APR NETO esperado + ocultar predicciones ≤ 0 (auditoría 2026-10-22).
        "ALTER TABLE user_configs ADD COLUMN IF NOT EXISTS min_net_apr FLOAT DEFAULT 10",
        "ALTER TABLE user_configs ADD COLUMN IF NOT EXISTS hide_negative BOOLEAN DEFAULT TRUE",
        # ML scoring: predicción cruda del modelo + versión, para auditar y
        # validar predicciones previas contra el resultado real (ml_train.py).
        "ALTER TABLE score_snapshots ADD COLUMN IF NOT EXISTS model_prediction FLOAT",
        "ALTER TABLE score_snapshots ADD COLUMN IF NOT EXISTS model_version VARCHAR(50)",
        # Confianza por oportunidad (analysis/ml_confidence) + filtro de display.
        "ALTER TABLE score_snapshots ADD COLUMN IF NOT EXISTS confidence FLOAT",
        "ALTER TABLE user_configs ADD COLUMN IF NOT EXISTS min_confidence FLOAT DEFAULT 0",
        # Binance live: cantidades reales por pierna, origen y earnings reales.
        "ALTER TABLE user_positions ADD COLUMN IF NOT EXISTS spot_qty FLOAT",
        "ALTER TABLE user_positions ADD COLUMN IF NOT EXISTS perp_qty FLOAT",
        "ALTER TABLE user_positions ADD COLUMN IF NOT EXISTS spot_venue VARCHAR(20) DEFAULT ''",
        "ALTER TABLE user_positions ADD COLUMN IF NOT EXISTS entry_basis_pct FLOAT",
        "ALTER TABLE user_positions ADD COLUMN IF NOT EXISTS entry_mode VARCHAR(30) DEFAULT ''",
        "ALTER TABLE user_positions ADD COLUMN IF NOT EXISTS source VARCHAR(20) DEFAULT 'manual'",
        "ALTER TABLE user_positions ADD COLUMN IF NOT EXISTS job_id INTEGER",
        "ALTER TABLE user_positions ADD COLUMN IF NOT EXISTS protection_json JSON",
        "ALTER TABLE user_positions ADD COLUMN IF NOT EXISTS spot_entry_price FLOAT",
        "ALTER TABLE user_positions ADD COLUMN IF NOT EXISTS realized_price_pnl FLOAT",
        "ALTER TABLE user_positions ADD COLUMN IF NOT EXISTS entry_slippage_usd FLOAT",
        "ALTER TABLE user_history ADD COLUMN IF NOT EXISTS price_pnl FLOAT",
        "ALTER TABLE user_history ADD COLUMN IF NOT EXISTS slippage_usd FLOAT",
        "ALTER TABLE user_history ADD COLUMN IF NOT EXISTS exec_json JSON",
        "ALTER TABLE user_positions ADD COLUMN IF NOT EXISTS earnings_source VARCHAR(20) DEFAULT 'estimated'",
        "UPDATE user_positions SET source = 'auto' WHERE auto_executed = TRUE AND (source IS NULL OR source = 'manual')",
        "ALTER TABLE user_configs ADD COLUMN IF NOT EXISTS auto_transfer BOOLEAN DEFAULT FALSE",
        # Señales de mercado horarias (Fase 2): create_all ya crea la tabla; esto la
        # deja idempotente si se aplica a mano o el modelo no se importó.
        "CREATE TABLE IF NOT EXISTS market_feature_snapshots ("
        "  id SERIAL PRIMARY KEY,"
        "  symbol VARCHAR(20) NOT NULL,"
        "  exchange VARCHAR(50) NOT NULL,"
        "  ts_hour BIGINT NOT NULL,"
        "  funding_rate_est FLOAT, mark_price FLOAT, index_price FLOAT,"
        "  next_funding_ts BIGINT, spot_mid FLOAT, perp_mid FLOAT, basis_pct FLOAT,"
        "  open_interest FLOAT, open_interest_usd FLOAT, oi_change_1h_pct FLOAT,"
        "  taker_buy_sell_ratio FLOAT, top_long_short_pos_ratio FLOAT,"
        "  funding_interval_hours INTEGER, funding_cap FLOAT, funding_floor FLOAT,"
        "  volatility_7d FLOAT,"
        "  captured_at TIMESTAMP NOT NULL DEFAULT NOW(),"
        "  CONSTRAINT uq_mfs_symbol_exchange_hour UNIQUE (symbol, exchange, ts_hour)"
        ")",
        "CREATE INDEX IF NOT EXISTS idx_mfs_ts_hour ON market_feature_snapshots (ts_hour)",
        "ALTER TABLE market_feature_snapshots ADD COLUMN IF NOT EXISTS spot_source VARCHAR(8)",
        "ALTER TABLE market_feature_snapshots ADD COLUMN IF NOT EXISTS alpha_liquidity_usd FLOAT",
    ]
    for sql in migrations:
        try:
            db.session.execute(db.text(sql))
        except Exception as e:
            log.debug(f"Migration skipped (likely already applied): {e}")
    db.session.commit()
