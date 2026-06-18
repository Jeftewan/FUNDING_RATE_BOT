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
# Cache for global Net APR (model_prediction) threshold
_global_netapr_cache = {"ts": 0, "p95": None, "count": 0}


class DBPersistence:
    """Reads/writes user state from PostgreSQL via SQLAlchemy models."""

    # ── Telegram notification dedup ───────────────────────────────

    def was_alert_sent(self, user_id, dedup_key: str,
                       window_seconds: int = 24 * 3600) -> bool:
        """Return True if (user_id, dedup_key) was logged within the window."""
        if not user_id or not dedup_key:
            return False
        try:
            from core.database import db
            cutoff = datetime.now(timezone.utc).timestamp() - window_seconds
            row = db.session.execute(
                db.text(
                    "SELECT 1 FROM notification_log "
                    "WHERE user_id = :uid AND dedup_key = :k "
                    "AND EXTRACT(EPOCH FROM sent_at) > :cutoff LIMIT 1"
                ),
                {"uid": int(user_id), "k": dedup_key[:256], "cutoff": cutoff},
            ).first()
            return row is not None
        except Exception as e:
            log.debug(f"was_alert_sent failed (allowing send): {e}")
            return False

    def record_alert_sent(self, user_id, dedup_key: str) -> None:
        """Persist that (user_id, dedup_key) was just sent. Idempotent."""
        if not user_id or not dedup_key:
            return
        try:
            from core.database import db
            db.session.execute(
                db.text(
                    "INSERT INTO notification_log (user_id, dedup_key, sent_at) "
                    "VALUES (:uid, :k, NOW()) "
                    "ON CONFLICT (user_id, dedup_key) "
                    "DO UPDATE SET sent_at = EXCLUDED.sent_at"
                ),
                {"uid": int(user_id), "k": dedup_key[:256]},
            )
            db.session.commit()
        except Exception as e:
            log.warning(f"record_alert_sent failed: {e}")
            try:
                from core.database import db
                db.session.rollback()
            except Exception:
                pass

    def load_user_state(self, user_id: int) -> dict:
        """Load per-user state as a dict (matching StateManager format)."""
        from core.database import db
        from core.db_models import UserConfig, UserPosition, UserHistory
        from core.encryption import decrypt_value

        config = UserConfig.query.filter_by(user_id=user_id).first()
        positions = UserPosition.query.filter_by(user_id=user_id, status="active").all()
        history = UserHistory.query.filter_by(user_id=user_id).order_by(
            UserHistory.closed_at.desc()).limit(500).all()

        state = {
            "total_capital": config.total_capital if config else 1000,
            "min_volume": config.min_volume if config else 1000000,
            "min_apr": config.min_apr if config else 10,
            "min_score": config.min_score if config else 40,
            "min_stability_days": config.min_stability_days if config else 3,
            "max_positions": config.max_positions if config else 5,
            "alert_minutes_before": config.alert_minutes_before if config else 10,
            "email_enabled": config.email_enabled if config else False,
            "tg_chat_id": config.tg_chat_id if config else "",
            "tg_bot_token": decrypt_value(config.tg_bot_token_encrypted) if config and config.tg_bot_token_encrypted else "",
            "allowed_exchanges": config.allowed_exchanges if config else "",
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

        for key in ("total_capital", "min_volume", "min_apr",
                     "min_score", "min_stability_days", "max_positions",
                     "alert_minutes_before", "email_enabled", "tg_chat_id",
                     "allowed_exchanges"):
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

    def load_user_exchange_keys(self, user_id: int, exchange_name: str) -> dict:
        """Return decrypted API creds for one exchange, or None if absent.

        Shape: {"api_key", "api_secret", "passphrase"}. Used by the order
        executor to build an authenticated per-user CCXT client.
        """
        from core.db_models import UserExchangeKey
        from core.encryption import decrypt_value

        row = UserExchangeKey.query.filter_by(
            user_id=user_id, exchange_name=exchange_name).first()
        if not row or not row.api_key_encrypted:
            return None
        return {
            "api_key": decrypt_value(row.api_key_encrypted),
            "api_secret": decrypt_value(row.api_secret_encrypted),
            "passphrase": decrypt_value(row.passphrase_encrypted),
        }

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
            auto_executed=bool(pos_dict.get("auto_executed", False)),
            payments_json=pos_dict.get("payments_json") or [],
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
            "auto_executed": bool(getattr(pos, "auto_executed", False)),
            "payments": pos.payments_json or [],
        }

    def aggregate_daily_earnings(self, user_id: int, days: int = 30) -> dict:
        """Per-day earnings rollup combining active + closed positions.

        Active positions: sum `earned` from each entry in payments_json bucketed
        by the day of its `ts` (Unix seconds). Manual-adjust entries are
        included (their delta is real money the user reconciled).

        Closed positions: if payments_json is populated, use it the same way.
        Otherwise, attribute the whole `earned` to `closed_at` (best-effort
        fallback for legacy rows).

        Returns:
          {
            today, yesterday, last_7d, last_30d, all_time: {earned, fees, net},
            today_positions_active: int,
            realized_apr_7d: float (annualized, %),
            series: [{date "YYYY-MM-DD", earned, fees, net}, ...]  (length=days)
          }
        Fees are not bucketed per-day (no per-day fee data exists); they are
        included only in `all_time` (active + closed) and as a flat number.
        """
        from core.db_models import UserPosition, UserHistory

        if days < 1:
            days = 1
        if days > 365:
            days = 365

        positions = UserPosition.query.filter_by(
            user_id=user_id, status="active"
        ).all()
        closed = UserHistory.query.filter_by(user_id=user_id).all()

        now_dt = datetime.now()
        today_str = now_dt.strftime("%Y-%m-%d")
        today_start = datetime(now_dt.year, now_dt.month, now_dt.day).timestamp()
        day_secs = 86400

        # Build empty series (oldest -> newest)
        from datetime import timedelta
        series = []
        for i in range(days - 1, -1, -1):
            d = (now_dt - timedelta(days=i)).strftime("%Y-%m-%d")
            series.append({"date": d, "earned": 0.0, "fees": 0.0, "net": 0.0})
        by_date = {row["date"]: row for row in series}

        all_time_earned = 0.0
        all_time_fees = 0.0

        def _bucket(ts_seconds: float, earned: float):
            nonlocal all_time_earned
            all_time_earned += earned
            d = datetime.fromtimestamp(ts_seconds).strftime("%Y-%m-%d")
            row = by_date.get(d)
            if row:
                row["earned"] += earned

        # Active: iterate payments
        for pos in positions:
            for pay in (pos.payments_json or []):
                try:
                    ts = float(pay.get("ts", 0))
                    earned = float(pay.get("earned", 0))
                except (TypeError, ValueError):
                    continue
                if ts <= 0:
                    continue
                _bucket(ts, earned)

        # Closed: prefer payments_json, fall back to closed_at + earned
        for h in closed:
            h_fees = float(h.fees or 0)
            all_time_fees += h_fees
            # No payments_json on UserHistory; attribute to closed_at day.
            if h.closed_at:
                ts = h.closed_at.timestamp()
                earned = float(h.earned or 0)
                _bucket(ts, earned)

        # Active fees (estimates + reals merged elsewhere — use stored values)
        for pos in positions:
            entry_real = pos.entry_fees_real
            exit_real = pos.exit_fees_real
            entry = entry_real if entry_real is not None else (pos.entry_fees or 0)
            exit_ = exit_real if exit_real is not None else (pos.exit_fees_est or 0)
            all_time_fees += float(entry) + float(exit_)

        # Fill nets in series
        for row in series:
            row["net"] = row["earned"]  # day-level fees not tracked separately

        # Period sums
        def _sum_last(n_days: int) -> dict:
            if n_days <= 0:
                rows = []
            else:
                rows = series[-n_days:]
            e = sum(r["earned"] for r in rows)
            return {"earned": e, "fees": 0.0, "net": e}

        today = {
            "earned": by_date[today_str]["earned"] if today_str in by_date else 0.0,
            "fees": 0.0,
            "net": by_date[today_str]["earned"] if today_str in by_date else 0.0,
        }
        yest_dt = (now_dt - timedelta(days=1)).strftime("%Y-%m-%d")
        yesterday = {
            "earned": by_date.get(yest_dt, {}).get("earned", 0.0),
            "fees": 0.0,
            "net": by_date.get(yest_dt, {}).get("earned", 0.0),
        }
        last_7d = _sum_last(min(7, days))
        last_30d = _sum_last(min(30, days))
        all_time = {
            "earned": all_time_earned,
            "fees": all_time_fees,
            "net": all_time_earned - all_time_fees,
        }

        # Realized APR (last 7d): annualize the 7d earnings against current
        # capital in use. Approximation — documented limitation.
        capital_in_use = sum(float(p.capital_used or 0) for p in positions)
        if capital_in_use > 0 and last_7d["earned"] != 0:
            realized_apr_7d = (last_7d["earned"] / capital_in_use) * (365.0 / 7.0) * 100.0
        else:
            realized_apr_7d = 0.0

        return {
            "today": today,
            "yesterday": yesterday,
            "last_7d": last_7d,
            "last_30d": last_30d,
            "all_time": all_time,
            "today_positions_active": len(positions),
            "realized_apr_7d": realized_apr_7d,
            "series": series,
        }

    @staticmethod
    def _hist_to_dict(h) -> dict:
        return {
            "id": str(h.id),
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
            "notes": getattr(h, "notes", None),
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
            # Cap cache size: drop the oldest half when over 300 entries.
            if len(_hist_stats_cache) > 300:
                sorted_keys = sorted(
                    _hist_stats_cache, key=lambda k: _hist_stats_cache[k][0]
                )
                for k in sorted_keys[:150]:
                    del _hist_stats_cache[k]
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

    def get_global_netapr_p95(self) -> float | None:
        """Return the 95th percentile of the predicted Net APR across ALL tokens.

        Sobre la columna ScoreSnapshot.model_prediction (net APR predicho por el
        modelo ML). Cached for 5 minutes. Returns None si hay pocos datos ML
        (<50 predicciones no nulas) → la detección de excepcionales se salta.
        """
        now = _time_mod.time()
        if _global_netapr_cache["p95"] is not None and now - _global_netapr_cache["ts"] < _HIST_STATS_TTL:
            return _global_netapr_cache["p95"]

        try:
            from core.db_models import ScoreSnapshot

            all_preds = [s.model_prediction for s in
                         ScoreSnapshot.query.with_entities(ScoreSnapshot.model_prediction)
                         .filter(ScoreSnapshot.model_prediction.isnot(None)).all()]
            if len(all_preds) < 50:
                log.debug(f"Global net APR p95: only {len(all_preds)} preds, need 50+")
                return None

            all_preds.sort()
            idx = int(len(all_preds) * 0.95)
            p95 = all_preds[min(idx, len(all_preds) - 1)]
            _global_netapr_cache.update({"ts": now, "p95": p95, "count": len(all_preds)})
            log.info(f"Global net APR p95={p95:.1f}% (from {len(all_preds)} preds)")
            return p95
        except Exception as e:
            log.warning(f"get_global_netapr_p95 failed: {e}")
            return None
