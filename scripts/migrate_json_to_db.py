#!/usr/bin/env python3
"""
Migrate existing JSON state to PostgreSQL database.
Idempotent: safe to run multiple times.

Usage:
  ADMIN_EMAIL=admin@example.com DATABASE_URL=postgresql://... python scripts/migrate_json_to_db.py
"""
import os
import sys
import json
import logging

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("migrate")

# Add project root to path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def main():
    from config import Config

    if not Config.DATABASE_URL:
        log.error("DATABASE_URL not set")
        sys.exit(1)

    admin_email = Config.ADMIN_EMAIL or os.environ.get("ADMIN_EMAIL", "")
    if not admin_email:
        log.error("ADMIN_EMAIL not set")
        sys.exit(1)

    # Load JSON state
    json_path = os.path.join(Config.DATA_DIR, "portfolio_state.json")
    if not os.path.exists(json_path):
        log.info(f"No JSON file found at {json_path} — nothing to migrate")
        sys.exit(0)

    with open(json_path, "r") as f:
        state = json.load(f)

    log.info(f"Loaded JSON: {len(state.get('positions', []))} positions, "
             f"{len(state.get('history', []))} history entries")

    # Initialize Flask app + DB
    from flask import Flask
    app = Flask(__name__)
    app.secret_key = Config.SECRET_KEY

    from core.database import init_db
    if not init_db(app):
        log.error("Failed to initialize database")
        sys.exit(1)

    with app.app_context():
        from core.database import db
        from core.db_models import User, UserConfig, UserPosition, UserHistory

        # Create or get admin user
        user = User.query.filter_by(email=admin_email).first()
        if not user:
            user = User(email=admin_email, is_admin=True)
            db.session.add(user)
            db.session.flush()
            log.info(f"Created admin user: {admin_email}")
        else:
            log.info(f"Admin user already exists: {admin_email}")

        # Migrate config
        config = UserConfig.query.filter_by(user_id=user.id).first()
        if not config:
            config = UserConfig(
                user_id=user.id,
                total_capital=state.get("total_capital", 1000),
                scan_interval=state.get("scan_interval", 300),
                min_volume=state.get("min_volume", 1000000),
                min_apr=state.get("min_apr", 10),
                min_score=state.get("min_score", 40),
                min_stability_days=state.get("min_stability_days", 3),
                max_positions=state.get("max_positions", 5),
                alert_minutes_before=state.get("alert_minutes_before", 10),
                email_enabled=state.get("email_enabled", False),
                wa_phone=state.get("wa_phone", ""),
            )
            db.session.add(config)
            log.info("Migrated config")
        else:
            log.info("Config already exists — skipped")

        # Migrate positions
        migrated_pos = 0
        for pos in state.get("positions", []):
            # Check if already migrated by matching symbol + entry_time
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
        for h in state.get("history", []):
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

        db.session.commit()

        log.info(f"Migration complete: {migrated_pos} positions, {migrated_hist} history entries")

        # Rename JSON file as backup
        backup_path = json_path + ".migrated"
        if not os.path.exists(backup_path):
            os.rename(json_path, backup_path)
            log.info(f"JSON file renamed to {backup_path}")


if __name__ == "__main__":
    main()
