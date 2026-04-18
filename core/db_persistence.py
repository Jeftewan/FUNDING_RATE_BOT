"""DB-backed persistence for multi-user SaaS mode."""
import logging
import time as _time_mod
from datetime import datetime, timezone

log = logging.getLogger("bot.db_persist")

# Cache for get_historical_stats (changes slowly, avoid hammering DB)
_hist_stats_cache = {}  # key -> (timestamp, result)
_HIST_STATS_TTL = 300  # 5 minutes

# Cache for global score threshold
_global_score_cache = {"ts": 0, "p95": None, "count": 0}


class DBPersistence:
    """Reads/writes user state from PostgreSQL via SQLAlchemy models."""

    def load_user_state(self, user_id: int) -> dict:
        """Load per-user state as a dict (matching StateManager format)."""
        from core.database import db
        from core.db_models import UserConfig, UserPosition, UserHistory
        from core.encryption import decrypt_value

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
            "tg_chat_id": config.tg_chat_id if config else "",
            "tg_bot_token": decrypt_value(config.tg_bot_token_encrypted) if config and config.tg_bot_token_encrypted else "",
            "positions": [self._pos_to_dict(p) for p in positions],
            "history": [self._hist_to_dict(h) for h in history],
            "total_earned": sum(p.earned_real for p in positions),
        }
        return state

    def save_user_config(self, user_id: int, data: dict):
        """Save user config fields to DB."""
        from core.database import db
        from core.db_models import UserConfig
        from core.encryption import encrypt_value

        config = UserConfig.query.filter_by(user_id=user_id).first()
        if not config:
            config = UserConfig(user_id=user_id)
            db.session.add(config)

        for key in ("total_capital", "scan_interval", "min_volume", "min_apr",
                     "min_score", "min_stability_days", "max_positions",
                     "alert_minutes_before", "email_enabled", "tg_chat_id"):
            if key in data:
                setattr(config, key, data[key])

        if "tg_bot_token" in data:
            plain = str(data["tg_bot_token"]).strip()
            config.tg_bot_token_encrypted = encrypt_value(plain) if plain else ""

        db.session.commit()

    def get_all_users_telegram(self) -> list:
        """Return Telegram credentials for every user that has notifications enabled.

        Result: [{"user_id": int, "tg_chat_id": str, "tg_bot_token": str}, ...]
        Only includes rows where email_enabled=True and both chat_id and token are set.
        """
        from core.db_models import UserConfig
        from core.encryption import decrypt_value

        configs = UserConfig.query.filter_by(email_enabled=True).all()
        result = []
        for cfg in configs:
            chat_id = cfg.tg_chat_id or ""
            token = decrypt_value(cfg.tg_bot_token_encrypted) if cfg.tg_bot_token_encrypted else ""
            if chat_id and token:
                result.append({
                    "user_id": cfg.user_id,
                    "tg_chat_id": chat_id,
                    "tg_bot_token": token,
                })
        return result

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
            exit_fees_est=pos_dict.get("exit_fees_est", 0),
            entry_fees_real=pos_dict.get("entry_fees_real"),
            exit_fees_real=pos_dict.get("exit_fees_real"),
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
            exposure=pos.exposure or 0,
            leverage=pos.leverage or 1,
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
            "exit_fees_est": pos.exit_fees_est or 0,
            "entry_fees_real": pos.entry_fees_real,
            "exit_fees_real": pos.exit_fees_real,
            "payments": pos.payments_json or [],
        }

    @staticmethod
    def _hist_to_dict(h) -> dict:
        return {
            "symbol": h.symbol,
            "exchange": h.exchange,
            "mode": h.mode,
            "capital_used": h.capital_used,
            "exposure": h.exposure or 0,
            "leverage": h.leverage or 1,
            "hours": h.hours,
            "payment_count": h.payment_count,
            "earned": h.earned,
            "fees": h.fees,
            "net_earned": h.net_earned,
            "avg_rate": h.avg_rate,
            "reason": h.reason,
            "closed_at": h.closed_at.isoformat() if h.closed_at else "",
        }

    def get_score_trend(self, symbol: str, exchange: str, limit: int = 10) -> dict:
        """Get score trend for a symbol+exchange pair.

        Returns:
          - scores: list of recent scores (oldest first)
          - trend: 'rising', 'falling', 'stable', 'new'
          - avg_score: average score over history
          - delta: score change (latest - oldest)
          - count: number of data points
        """
        from core.db_models import ScoreSnapshot

        snapshots = ScoreSnapshot.query.filter_by(
            symbol=symbol, exchange=exchange
        ).order_by(
            ScoreSnapshot.captured_at.desc()
        ).limit(limit).all()

        if not snapshots:
            return {"scores": [], "trend": "new", "avg_score": 0, "delta": 0, "count": 0}

        # Reverse to chronological order (oldest first)
        snapshots.reverse()
        scores = [s.score for s in snapshots]
        avg_score = sum(scores) / len(scores)

        if len(scores) < 3:
            trend = "new"
            delta = 0
        else:
            delta = scores[-1] - scores[0]
            # Compare recent half vs older half
            mid = len(scores) // 2
            older_avg = sum(scores[:mid]) / mid
            recent_avg = sum(scores[mid:]) / (len(scores) - mid)
            diff = recent_avg - older_avg

            if diff > 3:
                trend = "rising"
            elif diff < -3:
                trend = "falling"
            else:
                trend = "stable"

        return {
            "scores": scores,
            "trend": trend,
            "avg_score": round(avg_score, 1),
            "delta": delta,
            "count": len(scores),
        }

    def get_score_trends_batch(self, pairs: list, limit: int = 10) -> dict:
        """Get score trends for multiple symbol+exchange pairs at once.

        Args:
            pairs: list of (symbol, exchange) tuples
        Returns:
            dict keyed by "symbol_exchange" with trend info
        """
        from core.db_models import ScoreSnapshot
        from sqlalchemy import or_, and_

        if not pairs:
            return {}

        # Fetch all relevant snapshots in one query
        conditions = [
            and_(ScoreSnapshot.symbol == sym, ScoreSnapshot.exchange == ex)
            for sym, ex in pairs
        ]
        all_snapshots = ScoreSnapshot.query.filter(
            or_(*conditions)
        ).order_by(
            ScoreSnapshot.captured_at.desc()
        ).all()

        # Group by symbol+exchange
        grouped = {}
        for s in all_snapshots:
            key = f"{s.symbol}_{s.exchange}"
            grouped.setdefault(key, []).append(s)

        results = {}
        for key, snapshots in grouped.items():
            # Keep only most recent `limit` and reverse to chronological
            snapshots = snapshots[:limit]
            snapshots.reverse()
            scores = [s.score for s in snapshots]
            avg_score = sum(scores) / len(scores)

            if len(scores) < 3:
                trend = "new"
                delta = 0
            else:
                delta = scores[-1] - scores[0]
                mid = len(scores) // 2
                older_avg = sum(scores[:mid]) / mid
                recent_avg = sum(scores[mid:]) / (len(scores) - mid)
                diff = recent_avg - older_avg
                if diff > 3:
                    trend = "rising"
                elif diff < -3:
                    trend = "falling"
                else:
                    trend = "stable"

            results[key] = {
                "scores": scores,
                "trend": trend,
                "avg_score": round(avg_score, 1),
                "delta": delta,
                "count": len(scores),
            }

        return results

    def get_historical_stats(self, symbol: str, exchange: str,
                              days: int = 90) -> dict:
        """Get historical stats for a symbol+exchange from DB snapshots.

        Queries FundingRateSnapshot (90 days) and ScoreSnapshot (30 entries).
        Cached for 5 minutes to avoid excessive DB queries.

        Returns:
          - avg_rate: average funding rate over period
          - stddev_rate: std deviation
          - p90_rate: 90th percentile rate
          - rates: list of historical rates (for percentile calc)
          - avg_score: average score from score snapshots
          - score_history: list of historical scores
          - avg_apr: estimated average APR
          - apr_history: list of estimated APRs
        """
        cache_key = f"{symbol}_{exchange}_{days}"
        now = _time_mod.time()

        if cache_key in _hist_stats_cache:
            cached_ts, cached_result = _hist_stats_cache[cache_key]
            if now - cached_ts < _HIST_STATS_TTL:
                return cached_result

        import math

        try:
            from core.db_models import FundingRateSnapshot, ScoreSnapshot
            from datetime import timedelta

            cutoff = datetime.now(timezone.utc) - timedelta(days=days)

            # Funding rate snapshots (90 days)
            rate_snaps = FundingRateSnapshot.query.filter(
                FundingRateSnapshot.symbol == symbol,
                FundingRateSnapshot.exchange == exchange,
                FundingRateSnapshot.captured_at >= cutoff,
            ).order_by(FundingRateSnapshot.captured_at.asc()).all()

            rates = [s.rate for s in rate_snaps if s.rate is not None]

            if rates:
                avg_rate = sum(rates) / len(rates)
                variance = sum((r - avg_rate) ** 2 for r in rates) / len(rates)
                stddev_rate = math.sqrt(variance)
                sorted_rates = sorted(abs(r) for r in rates)
                p90_idx = int(len(sorted_rates) * 0.9)
                p90_rate = sorted_rates[min(p90_idx, len(sorted_rates) - 1)]
            else:
                avg_rate = 0
                stddev_rate = 0
                p90_rate = 0

            # Estimate APR from rate snapshots
            apr_history = []
            for s in rate_snaps:
                if s.rate and s.interval_hours:
                    ppd = 24 / s.interval_hours
                    apr = abs(s.rate) * ppd * 365 * 100
                    apr_history.append(apr)
            avg_apr = sum(apr_history) / len(apr_history) if apr_history else 0

            # Score snapshots (last 30)
            score_snaps = ScoreSnapshot.query.filter_by(
                symbol=symbol, exchange=exchange,
            ).order_by(
                ScoreSnapshot.captured_at.desc()
            ).limit(30).all()

            score_history = [s.score for s in reversed(score_snaps)]
            avg_score = sum(score_history) / len(score_history) if score_history else 0

            result = {
                "avg_rate": avg_rate,
                "stddev_rate": stddev_rate,
                "p90_rate": p90_rate,
                "rates": rates,
                "avg_score": avg_score,
                "score_history": score_history,
                "avg_apr": avg_apr,
                "apr_history": apr_history,
                "data_points": len(rates),
            }

            _hist_stats_cache[cache_key] = (now, result)
            return result

        except Exception as e:
            log.warning(f"get_historical_stats failed for {symbol}@{exchange}: {e}")
            return {
                "avg_rate": 0, "stddev_rate": 0, "p90_rate": 0, "rates": [],
                "avg_score": 0, "score_history": [], "avg_apr": 0,
                "apr_history": [], "data_points": 0,
            }

    def get_global_score_p95(self) -> float | None:
        """Return the 95th percentile score across ALL tokens.

        Cached for 5 minutes. Returns None if insufficient data (<50 scores).
        """
        now = _time_mod.time()
        if _global_score_cache["p95"] is not None and now - _global_score_cache["ts"] < _HIST_STATS_TTL:
            return _global_score_cache["p95"]

        try:
            from core.db_models import ScoreSnapshot

            all_scores = [s.score for s in
                          ScoreSnapshot.query.with_entities(ScoreSnapshot.score).all()]
            if len(all_scores) < 50:
                log.debug(f"Global score p95: only {len(all_scores)} scores, need 50+")
                return None

            all_scores.sort()
            idx = int(len(all_scores) * 0.95)
            p95 = all_scores[min(idx, len(all_scores) - 1)]
            _global_score_cache.update({"ts": now, "p95": p95, "count": len(all_scores)})
            log.info(f"Global score p95={p95} (from {len(all_scores)} scores)")
            return p95
        except Exception as e:
            log.warning(f"get_global_score_p95 failed: {e}")
            return None
