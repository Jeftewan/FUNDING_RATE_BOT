"""SQLAlchemy models for multi-user SaaS mode."""
from datetime import datetime, timezone
from core.database import db


class User(db.Model):
    __tablename__ = "users"

    id = db.Column(db.Integer, primary_key=True)
    email = db.Column(db.String(255), unique=True, nullable=False, index=True)
    magic_link_token = db.Column(db.String(512), nullable=True)
    token_expires_at = db.Column(db.DateTime, nullable=True)
    created_at = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc))
    is_admin = db.Column(db.Boolean, default=False)
    is_active = db.Column(db.Boolean, default=True)

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
    ih = db.Column(db.Float, default=8)  # interval hours
    earned_real = db.Column(db.Float, default=0)
    last_earn_update = db.Column(db.Float, default=0)
    last_fr_used = db.Column(db.Float, default=0)
    long_exchange = db.Column(db.String(50), default="")
    short_exchange = db.Column(db.String(50), default="")
    payment_count = db.Column(db.Integer, default=0)
    avg_rate = db.Column(db.Float, default=0)
    status = db.Column(db.String(10), default="active", index=True)  # active / closed
    entry_fees = db.Column(db.Float, default=0)
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
