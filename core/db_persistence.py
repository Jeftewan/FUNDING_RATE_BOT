"""DB-backed persistence for multi-user SaaS mode."""
import logging
from datetime import datetime, timezone

log = logging.getLogger("bot.db_persist")


class DBPersistence:
    """Reads/writes user state from PostgreSQL via SQLAlchemy models."""

    def load_user_state(self, user_id: int) -> dict:
        """Load per-user state as a dict (matching StateManager format)."""
        from core.database import db
        from core.db_models import UserConfig, UserPosition, UserHistory

        config = UserConfig.query.filter_by(user_id=user_id).first()
        positions = UserPosition.query.filter_by(user_id=user_id, status="active").all()
        history = UserHistory.query.filter_by(user_id=user_id).order_by(
            UserHistory.closed_at.desc()).limit(100).all()

        state = {
            "total_capital": config.total_capital if config else 1000,
            "scan_interval": config.scan_interval if config else 300,
            "min_volume": config.min_volume if config else 1000000,
            "min_apr": config.min_apr if config else 10,
            "min_score": config.min_score if config else 40,
            "min_stability_days": config.min_stability_days if config else 3,
            "max_positions": config.max_positions if config else 5,
            "alert_minutes_before": config.alert_minutes_before if config else 10,
            "email_enabled": config.email_enabled if config else False,
            "wa_phone": config.wa_phone if config else "",
            "wa_apikey": "",  # Never load decrypted apikey into state
            "positions": [self._pos_to_dict(p) for p in positions],
            "history": [self._hist_to_dict(h) for h in history],
            "total_earned": sum(p.earned_real for p in positions),
        }
        return state

    def save_user_config(self, user_id: int, data: dict):
        """Save user config fields to DB."""
        from core.database import db
        from core.db_models import UserConfig

        config = UserConfig.query.filter_by(user_id=user_id).first()
        if not config:
            config = UserConfig(user_id=user_id)
            db.session.add(config)

        for key in ("total_capital", "scan_interval", "min_volume", "min_apr",
                     "min_score", "min_stability_days", "max_positions",
                     "alert_minutes_before", "email_enabled", "wa_phone"):
            if key in data:
                setattr(config, key, data[key])

        db.session.commit()

    def save_position(self, user_id: int, pos_dict: dict) -> int:
        """Create a new position in DB. Returns position ID."""
        from core.database import db
        from core.db_models import UserPosition
        import time as _time

        capital = pos_dict.get("capital_used", 0)
        leverage = pos_dict.get("leverage", 1)
        mode = pos_dict.get("mode", "spot_perp")

        # Calculate exposure if not provided
        exposure = pos_dict.get("exposure", 0)
        if not exposure and capital > 0:
            if mode == "spot_perp":
                exposure = capital * leverage / (leverage + 1)
            else:
                exposure = (capital / 2) * leverage

        pos = UserPosition(
            user_id=user_id,
            symbol=pos_dict.get("symbol", ""),
            exchange=pos_dict.get("exchange", ""),
            mode=mode,
            entry_fr=pos_dict.get("entry_fr", 0),
            entry_price=pos_dict.get("entry_price", 0),
            entry_time=pos_dict.get("entry_time", 0),
            capital_used=capital,
            leverage=leverage,
            exposure=exposure,
            ih=pos_dict.get("ih", 8),
            earned_real=0,
            last_earn_update=_time.time(),
            last_fr_used=0,
            long_exchange=pos_dict.get("long_exchange", ""),
            short_exchange=pos_dict.get("short_exchange", ""),
            entry_fees=pos_dict.get("entry_fees", 0),
            payments_json=[],
        )
        db.session.add(pos)
        db.session.commit()
        return pos.id

    def close_position(self, position_id: int, result: dict):
        """Close a position and create history record."""
        from core.database import db
        from core.db_models import UserPosition, UserHistory

        pos = UserPosition.query.get(position_id)
        if not pos:
            return

        pos.status = "closed"
        pos.closed_at = datetime.now(timezone.utc)
        pos.close_reason = result.get("reason", "manual")

        hist = UserHistory(
            user_id=pos.user_id,
            symbol=pos.symbol,
            exchange=pos.exchange,
            mode=pos.mode,
            capital_used=pos.capital_used,
            hours=result.get("hours", 0),
            payment_count=pos.payment_count,
            earned=pos.earned_real,
            fees=result.get("fees", 0),
            net_earned=result.get("net_earned", 0),
            avg_rate=pos.avg_rate,
            reason=result.get("reason", "manual"),
        )
        db.session.add(hist)
        db.session.commit()

    def update_position_earnings(self, position_id: int, earned: float,
                                  payment_count: int, avg_rate: float,
                                  last_fr: float, payments: list):
        """Update earnings for a position during monitoring."""
        from core.database import db
        from core.db_models import UserPosition

        pos = UserPosition.query.get(position_id)
        if not pos:
            return
        pos.earned_real = earned
        pos.payment_count = payment_count
        pos.avg_rate = avg_rate
        pos.last_fr_used = last_fr
        pos.last_earn_update = __import__("time").time()
        pos.payments_json = payments
        db.session.commit()

    def get_all_active_positions(self) -> list:
        """Get all active positions across all users (for scanner)."""
        from core.db_models import UserPosition
        return UserPosition.query.filter_by(status="active").all()

    def save_scan_cache(self, opportunities: list, defi_opps: list,
                        all_data: dict, scan_count: int):
        """Save shared scan results."""
        from core.database import db
        from core.db_models import ScanCache

        cache = ScanCache.query.first()
        if not cache:
            cache = ScanCache()
            db.session.add(cache)

        cache.opportunities_json = opportunities
        cache.defi_json = defi_opps
        cache.all_data_json = all_data
        cache.scan_count = scan_count
        cache.scan_time = datetime.now(timezone.utc)
        db.session.commit()

    def load_scan_cache(self) -> dict:
        """Load shared scan results."""
        from core.db_models import ScanCache
        cache = ScanCache.query.first()
        if not cache:
            return {"opportunities": [], "defi": [], "all_data": {}, "scan_count": 0}
        return {
            "opportunities": cache.opportunities_json or [],
            "defi": cache.defi_json or [],
            "all_data": cache.all_data_json or {},
            "scan_count": cache.scan_count or 0,
        }

    @staticmethod
    def _pos_to_dict(pos) -> dict:
        return {
            "id": str(pos.id),
            "symbol": pos.symbol,
            "exchange": pos.exchange,
            "mode": pos.mode,
            "entry_fr": pos.entry_fr,
            "entry_price": pos.entry_price,
            "entry_time": pos.entry_time,
            "capital_used": pos.capital_used,
            "leverage": pos.leverage or 1,
            "exposure": pos.exposure or (pos.capital_used / 2),
            "ih": pos.ih,
            "earned_real": pos.earned_real,
            "last_earn_update": pos.last_earn_update or (pos.entry_time / 1000 if pos.entry_time else 0),
            "last_fr_used": pos.last_fr_used,
            "long_exchange": pos.long_exchange,
            "short_exchange": pos.short_exchange,
            "payment_count": pos.payment_count,
            "avg_rate": pos.avg_rate,
            "entry_fees": pos.entry_fees,
            "payments": pos.payments_json or [],
        }

    @staticmethod
    def _hist_to_dict(h) -> dict:
        return {
            "symbol": h.symbol,
            "exchange": h.exchange,
            "mode": h.mode,
            "capital_used": h.capital_used,
            "hours": h.hours,
            "payment_count": h.payment_count,
            "earned": h.earned,
            "fees": h.fees,
            "net_earned": h.net_earned,
            "avg_rate": h.avg_rate,
            "reason": h.reason,
            "closed_at": h.closed_at.isoformat() if h.closed_at else "",
        }
