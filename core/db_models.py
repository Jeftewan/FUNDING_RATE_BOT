"""SQLAlchemy models for multi-user SaaS mode."""
from datetime import datetime, timezone
from core.database import db


class User(db.Model):
    __tablename__ = "users"

    id = db.Column(db.Integer, primary_key=True)
    email = db.Column(db.String(255), unique=True, nullable=False, index=True)
    password_hash = db.Column(db.String(256), nullable=True)
    magic_link_token = db.Column(db.String(512), nullable=True)
    token_expires_at = db.Column(db.DateTime, nullable=True)
    created_at = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc))
    is_admin = db.Column(db.Boolean, default=False)
    is_active = db.Column(db.Boolean, default=True)
    terms_accepted_at = db.Column(db.DateTime, nullable=True)

    # Relationships
    config = db.relationship("UserConfig", uselist=False, back_populates="user",
                             cascade="all, delete-orphan")
    positions = db.relationship("UserPosition", back_populates="user",
                                cascade="all, delete-orphan")
    history = db.relationship("UserHistory", back_populates="user",
                              cascade="all, delete-orphan")
    exchange_keys = db.relationship("UserExchangeKey", back_populates="user",
                                    cascade="all, delete-orphan")

    # Flask-Login interface
    @property
    def is_authenticated(self):
        return True

    def get_id(self):
        return str(self.id)


class UserConfig(db.Model):
    __tablename__ = "user_configs"

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id"), unique=True, nullable=False)
    total_capital = db.Column(db.Float, default=1000)
    scan_interval = db.Column(db.Integer, default=300)  # seconds
    min_volume = db.Column(db.Float, default=1000000)
    min_apr = db.Column(db.Float, default=10)
    min_score = db.Column(db.Integer, default=40)
    min_stability_days = db.Column(db.Integer, default=3)
    max_positions = db.Column(db.Integer, default=5)
    alert_minutes_before = db.Column(db.Integer, default=10)
    email_enabled = db.Column(db.Boolean, default=False)
    tg_chat_id = db.Column(db.String(64), default="")
    tg_bot_token_encrypted = db.Column(db.String(512), default="")
    # Legacy WhatsApp columns kept for migration; unused by new code.
    wa_phone = db.Column(db.String(20), default="")
    wa_apikey_encrypted = db.Column(db.String(512), default="")

    user = db.relationship("User", back_populates="config")


class UserExchangeKey(db.Model):
    __tablename__ = "user_exchange_keys"

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False)
    exchange_name = db.Column(db.String(50), nullable=False)
    api_key_encrypted = db.Column(db.String(512), default="")
    api_secret_encrypted = db.Column(db.String(512), default="")
    passphrase_encrypted = db.Column(db.String(512), default="")

    user = db.relationship("User", back_populates="exchange_keys")

    __table_args__ = (
        db.UniqueConstraint("user_id", "exchange_name", name="uq_user_exchange"),
    )


class UserPosition(db.Model):
    __tablename__ = "user_positions"

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False, index=True)
    symbol = db.Column(db.String(20), nullable=False)
    exchange = db.Column(db.String(50), default="")
    mode = db.Column(db.String(20), default="spot_perp")
    entry_fr = db.Column(db.Float, default=0)
    entry_price = db.Column(db.Float, default=0)
    entry_time = db.Column(db.Float, default=0)  # unix timestamp
    capital_used = db.Column(db.Float, default=0)
    leverage = db.Column(db.Integer, default=1)
    exposure = db.Column(db.Float, default=0)  # notional exposure per side
    ih = db.Column(db.Float, default=8)  # interval hours
    earned_real = db.Column(db.Float, default=0)
    last_earn_update = db.Column(db.Float, default=0)
    last_fr_used = db.Column(db.Float, default=0)
    long_exchange = db.Column(db.String(50), default="")
    short_exchange = db.Column(db.String(50), default="")
    payment_count = db.Column(db.Integer, default=0)
    avg_rate = db.Column(db.Float, default=0)
    status = db.Column(db.String(10), default="active", index=True)  # active / closed
    # Fee accounting: `entry_fees` now stores the ENTRY-only estimate
    # (half of the old round-trip value).  `exit_fees_est` is the symmetric
    # exit estimate. `entry_fees_real` / `exit_fees_real` are user-entered
    # actuals that, when present, override the estimates in every PnL calc.
    entry_fees = db.Column(db.Float, default=0)
    exit_fees_est = db.Column(db.Float, default=0)
    entry_fees_real = db.Column(db.Float, nullable=True)
    exit_fees_real = db.Column(db.Float, nullable=True)
    payments_json = db.Column(db.JSON, default=list)  # [{ts, rate, earned, cumulative}]
    created_at = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc))
    closed_at = db.Column(db.DateTime, nullable=True)
    close_reason = db.Column(db.String(50), default="")


    user = db.relationship("User", back_populates="positions")


class UserHistory(db.Model):
    __tablename__ = "user_history"

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False, index=True)
    symbol = db.Column(db.String(20), nullable=False)
    exchange = db.Column(db.String(50), default="")
    mode = db.Column(db.String(20), default="spot_perp")
    capital_used = db.Column(db.Float, default=0)
    exposure = db.Column(db.Float, default=0)
    leverage = db.Column(db.Integer, default=1)
    hours = db.Column(db.Float, default=0)
    payment_count = db.Column(db.Integer, default=0)
    earned = db.Column(db.Float, default=0)
    fees = db.Column(db.Float, default=0)
    net_earned = db.Column(db.Float, default=0)
    avg_rate = db.Column(db.Float, default=0)
    reason = db.Column(db.String(50), default="")
    closed_at = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc))

    user = db.relationship("User", back_populates="history")


class ScanCache(db.Model):
    __tablename__ = "scan_cache"

    id = db.Column(db.Integer, primary_key=True)
    scan_time = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc))
    opportunities_json = db.Column(db.JSON, default=list)
    defi_json = db.Column(db.JSON, default=list)
    all_data_json = db.Column(db.JSON, default=dict)
    scan_count = db.Column(db.Integer, default=0)


class FundingRateSnapshot(db.Model):
    """Historical funding rate snapshots for data accumulation.

    Stores rate data from each scan for future analysis and ML training.
    Retention: 90 days (~180K rows, ~18MB).
    """
    __tablename__ = "funding_rate_snapshots"

    id = db.Column(db.Integer, primary_key=True)
    symbol = db.Column(db.String(20), nullable=False)
    exchange = db.Column(db.String(50), nullable=False)
    rate = db.Column(db.Float, nullable=False)
    volume_24h = db.Column(db.Float, default=0)
    open_interest = db.Column(db.Float, nullable=True)
    mark_price = db.Column(db.Float, default=0)
    interval_hours = db.Column(db.Integer, default=8)
    funding_ts = db.Column(db.BigInteger, default=0)  # funding payment timestamp
    captured_at = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc),
                            nullable=False, index=True)

    __table_args__ = (
        db.UniqueConstraint("symbol", "exchange", "funding_ts",
                            name="uq_snapshot_symbol_exchange_ts"),
        db.Index("idx_frs_symbol_exchange", "symbol", "exchange", "captured_at"),
    )


class ScoreSnapshot(db.Model):
    """Rolling score history per opportunity.

    Tracks how each opportunity's score evolves across scans.
    Rolling window: max 30 entries per symbol+exchange pair (~10 days).
    Older entries are overwritten to keep DB lean.
    Estimated size: ~50 symbols × 30 rows × ~200 bytes = ~300KB total.
    """
    __tablename__ = "score_snapshots"

    id = db.Column(db.Integer, primary_key=True)
    symbol = db.Column(db.String(20), nullable=False)
    exchange = db.Column(db.String(50), nullable=False)
    mode = db.Column(db.String(20), default="spot_perp")
    score = db.Column(db.Integer, nullable=False)
    funding_rate = db.Column(db.Float, default=0)
    apr = db.Column(db.Float, default=0)
    volume_24h = db.Column(db.Float, default=0)
    z_score = db.Column(db.Float, default=0)
    momentum_signal = db.Column(db.String(20), default="flat")
    scan_number = db.Column(db.Integer, default=0)
    captured_at = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc),
                            nullable=False, index=True)

    __table_args__ = (
        db.Index("idx_ss_symbol_exchange", "symbol", "exchange", "captured_at"),
    )
