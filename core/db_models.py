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
    # CSV of exchange names to show in the opportunities list ("" = all).
    allowed_exchanges = db.Column(db.String(256), default="")
    # Auto-transfer USDT spot↔futuros (Binance) antes de abrir, si la key lo permite.
    auto_transfer = db.Column(db.Boolean, default=False)
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
    # True when the position was opened by placing REAL orders via the user's
    # API keys (trade_executor), vs manual bookkeeping. Drives the AUTO badge.
    auto_executed = db.Column(db.Boolean, default=False)
    # Cantidades reales por pierna (base). El cierre auto usa SOLO estas
    # cantidades — nunca exposure/precio — para no tocar saldo ajeno.
    spot_qty = db.Column(db.Float, nullable=True)
    perp_qty = db.Column(db.Float, nullable=True)
    spot_venue = db.Column(db.String(20), default="")  # "binance" | "bsc" | ""
    entry_basis_pct = db.Column(db.Float, nullable=True)
    entry_mode = db.Column(db.String(30), default="")
    source = db.Column(db.String(20), default="manual")  # manual | auto | imported
    job_id = db.Column(db.Integer, nullable=True)
    # Seguros (SL del short + venta límite spot): ver trade_executor.place_protection.
    protection_json = db.Column(db.JSON, nullable=True)
    # "real" = earnings desde el income FUNDING_FEE del exchange (sync);
    # "estimated" = exposure × tasa pública (scanner).
    earnings_source = db.Column(db.String(20), default="estimated")
    created_at = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc))
    closed_at = db.Column(db.DateTime, nullable=True)
    close_reason = db.Column(db.String(50), default="")


    user = db.relationship("User", back_populates="positions")


class UserAccountSnapshot(db.Model):
    """Último estado real sincronizado de la cuenta de un exchange (o wallet).

    Una fila por (user, exchange). data_json guarda saldos, posiciones,
    permisos, modo de cuenta, importables y drift. Lo escribe el sync.
    """
    __tablename__ = "user_account_snapshots"

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False, index=True)
    exchange = db.Column(db.String(50), nullable=False)
    data_json = db.Column(db.JSON, default=dict)
    ok = db.Column(db.Boolean, default=True)
    error = db.Column(db.Text, nullable=True)
    synced_at = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc))

    __table_args__ = (
        db.UniqueConstraint("user_id", "exchange", name="uq_snapshot_user_exchange"),
    )


class ExecutionJob(db.Model):
    """Ejecución de órdenes reales en background (open/close, TWAP pareado).

    Corre fuera del request (gunicorn --timeout 120) y es reconciliable tras
    un reinicio vía clientOrderId = bsy{id}-...
    """
    __tablename__ = "execution_jobs"

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False, index=True)
    kind = db.Column(db.String(10), nullable=False)  # open | close
    status = db.Column(db.String(20), default="queued", index=True)
    # queued | running | done | failed | canceled | interrupted
    params_json = db.Column(db.JSON, default=dict)
    progress_json = db.Column(db.JSON, default=dict)
    result_json = db.Column(db.JSON, default=dict)
    error = db.Column(db.Text, nullable=True)
    position_id = db.Column(db.Integer, nullable=True)
    cancel_requested = db.Column(db.Boolean, default=False)
    created_at = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc))
    updated_at = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc))


class UserWallet(db.Model):
    """Wallet on-chain DEDICADA del usuario (BSC) para la pierna spot Alpha.

    La private key se guarda cifrada (Fernet, fail-closed) y nunca vuelve al
    frontend. Riesgo acotado al saldo de esa wallet.
    """
    __tablename__ = "user_wallets"

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False, index=True)
    chain = db.Column(db.String(20), nullable=False, default="bsc")
    address = db.Column(db.String(64), nullable=False)
    pk_encrypted = db.Column(db.String(512), nullable=False)
    created_at = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc))

    __table_args__ = (
        db.UniqueConstraint("user_id", "chain", name="uq_user_wallet_chain"),
    )


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
    # Free-form audit trail (e.g. "manual earnings edit at <ts>"). NULL = no edits.
    notes = db.Column(db.Text, nullable=True)

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
    # ML model audit: predicción cruda (net_apr) + versión del modelo que la
    # generó. Null cuando el scan corrió con el heurístico (sin modelo). Alimenta
    # la validación de predicciones previas en scripts/ml_train.py.
    model_prediction = db.Column(db.Float)
    model_version = db.Column(db.String(50))
    scan_number = db.Column(db.Integer, default=0)
    captured_at = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc),
                            nullable=False, index=True)

    __table_args__ = (
        db.Index("idx_ss_symbol_exchange", "symbol", "exchange", "captured_at"),
    )
