import uuid
from datetime import datetime, timezone
from decimal import ROUND_HALF_UP, Decimal

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    String,
    TypeDecorator,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base
from app.enums import LedgerEntryType, PayoutStatus, SaleStatus

TWO_PLACES = Decimal("0.01")


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def new_id() -> str:
    return uuid.uuid4().hex


class UTCDateTime(TypeDecorator):
    """SQLite drops timezone info, so aware datetimes come back naive and
    datetime arithmetic breaks. Normalise to aware UTC in both directions."""

    impl = DateTime
    cache_ok = True

    def process_bind_param(self, value: datetime | None, dialect):
        if value is None:
            return None
        if value.tzinfo is None:
            return value
        return value.astimezone(timezone.utc).replace(tzinfo=None)

    def process_result_value(self, value: datetime | None, dialect):
        if value is None:
            return None
        return value.replace(tzinfo=timezone.utc)


class Money(TypeDecorator):
    """Decimal stored as TEXT. SQLAlchemy's Numeric goes through float on
    SQLite, which is exactly what we're trying to avoid with money. Quantised
    to 2 places on write. For Postgres, swap impl to NUMERIC(12,2)."""

    impl = String(20)
    cache_ok = True

    def process_bind_param(self, value, dialect):
        if value is None:
            return None
        return str(Decimal(value).quantize(TWO_PLACES, rounding=ROUND_HALF_UP))

    def process_result_value(self, value, dialect):
        if value is None:
            return None
        return Decimal(value)


class User(Base):
    __tablename__ = "users"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)  # e.g. "john_doe"
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow, nullable=False)

    sales: Mapped[list["Sale"]] = relationship(back_populates="user")


class Sale(Base):
    __tablename__ = "sales"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id"), nullable=False)
    brand: Mapped[str] = mapped_column(String(32), nullable=False)
    earning: Mapped[Decimal] = mapped_column(Money, nullable=False)
    status: Mapped[SaleStatus] = mapped_column(
        String(16), default=SaleStatus.PENDING, nullable=False
    )
    reconciled_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow, nullable=False)

    user: Mapped[User] = relationship(back_populates="sales")
    advance: Mapped["AdvancePayout | None"] = relationship(
        back_populates="sale", uselist=False
    )

    __table_args__ = (
        # money is stored as TEXT, so cast for the numeric comparison
        CheckConstraint("CAST(earning AS REAL) >= 0", name="ck_sales_earning_non_negative"),
        Index("idx_sales_user_status", "user_id", "status"),
    )


class AdvancePayout(Base):
    """Money already transferred out against a pending sale. Not a ledger
    entry on purpose — it left the system, so it's never withdrawable."""

    __tablename__ = "advance_payouts"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    # unique: a sale can never receive a second advance, however many
    # job instances race
    sale_id: Mapped[str] = mapped_column(ForeignKey("sales.id"), nullable=False, unique=True)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id"), nullable=False)
    amount: Mapped[Decimal] = mapped_column(Money, nullable=False)
    transferred_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow, nullable=False)

    sale: Mapped[Sale] = relationship(back_populates="advance")


class Payout(Base):
    __tablename__ = "payouts"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id"), nullable=False)
    amount: Mapped[Decimal] = mapped_column(Money, nullable=False)
    status: Mapped[PayoutStatus] = mapped_column(
        String(16), default=PayoutStatus.INITIATED, nullable=False
    )
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow, nullable=False)

    __table_args__ = (
        CheckConstraint("CAST(amount AS REAL) > 0", name="ck_payouts_amount_positive"),
        Index("idx_payouts_user_created", "user_id", "created_at"),
    )


class LedgerEntry(Base):
    """Append-only — never update or delete rows here.
    withdrawable balance = SUM(amount) per user."""

    __tablename__ = "ledger_entries"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id"), nullable=False)
    entry_type: Mapped[LedgerEntryType] = mapped_column(String(32), nullable=False)
    amount: Mapped[Decimal] = mapped_column(Money, nullable=False)  # signed
    sale_id: Mapped[str | None] = mapped_column(ForeignKey("sales.id"), nullable=True)
    payout_id: Mapped[str | None] = mapped_column(ForeignKey("payouts.id"), nullable=True)
    # e.g. "final:<sale_id>", "reversal:<payout_id>" — duplicates collide here
    idempotency_key: Mapped[str] = mapped_column(String(80), nullable=False, unique=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow, nullable=False)

    __table_args__ = (Index("idx_ledger_user", "user_id"),)
