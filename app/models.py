"""ORM models.

The two unique constraints in this file ARE business rules, not hygiene:

  advance_payouts.sale_id UNIQUE
      "the same sale must never receive another advance payout, even if the
      advance payout job runs multiple times" -- enforced by the database, so
      it holds no matter how many job instances race.

  ledger_entries.idempotency_key UNIQUE
      Duplicate webhook, re-run job, retried request: all collide here and are
      swallowed as a no-op. Replaces every "did we already do this?" check.
"""

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


class Money(TypeDecorator):
    """Exact decimal currency, stored as TEXT.

    SQLAlchemy's Numeric type round-trips through float on SQLite and warns
    about the lost precision. Storing the decimal as a zero-padded string keeps
    the value exact and makes it portable: swap the impl for NUMERIC(12, 2) on
    Postgres and nothing above this line changes.
    """

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

    id: Mapped[str] = mapped_column(String(64), primary_key=True)  # "john_doe"
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, nullable=False)

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
    reconciled_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, nullable=False)

    user: Mapped[User] = relationship(back_populates="sales")
    advance: Mapped["AdvancePayout | None"] = relationship(
        back_populates="sale", uselist=False
    )

    __table_args__ = (
        CheckConstraint("CAST(earning AS REAL) >= 0", name="ck_sales_earning_non_negative"),
        Index("idx_sales_user_status", "user_id", "status"),
    )


class AdvancePayout(Base):
    """Money already transferred out against a pending sale.

    Deliberately NOT a ledger entry: this money has left the system, so it must
    never appear in the withdrawable balance. Reconciliation nets it out.
    """

    __tablename__ = "advance_payouts"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    sale_id: Mapped[str] = mapped_column(
        ForeignKey("sales.id"), nullable=False, unique=True  # business rule #1
    )
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id"), nullable=False)
    amount: Mapped[Decimal] = mapped_column(Money, nullable=False)
    transferred_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, nullable=False)

    sale: Mapped[Sale] = relationship(back_populates="advance")


class Payout(Base):
    """An outbound withdrawal request with a gateway-driven lifecycle."""

    __tablename__ = "payouts"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id"), nullable=False)
    amount: Mapped[Decimal] = mapped_column(Money, nullable=False)
    status: Mapped[PayoutStatus] = mapped_column(
        String(16), default=PayoutStatus.INITIATED, nullable=False
    )
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, nullable=False)

    __table_args__ = (
        CheckConstraint("CAST(amount AS REAL) > 0", name="ck_payouts_amount_positive"),
        Index("idx_payouts_user_created", "user_id", "created_at"),
    )


class LedgerEntry(Base):
    """Append-only. Never UPDATE or DELETE a row here.

    withdrawable_balance(user) = SUM(amount) WHERE user_id = ?
    """

    __tablename__ = "ledger_entries"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id"), nullable=False)
    entry_type: Mapped[LedgerEntryType] = mapped_column(String(32), nullable=False)
    amount: Mapped[Decimal] = mapped_column(Money, nullable=False)  # signed
    sale_id: Mapped[str | None] = mapped_column(ForeignKey("sales.id"), nullable=True)
    payout_id: Mapped[str | None] = mapped_column(ForeignKey("payouts.id"), nullable=True)
    idempotency_key: Mapped[str] = mapped_column(String(80), nullable=False, unique=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, nullable=False)

    __table_args__ = (Index("idx_ledger_user", "user_id"),)
