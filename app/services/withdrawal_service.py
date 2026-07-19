"""Withdrawals.

The debit is written at initiation, in the same transaction as the balance
check, so two racing requests can't both pass the check (SQLite: BEGIN
IMMEDIATE serialises writers; Postgres: FOR UPDATE on the user row).

Failed/cancelled/rejected payouts don't count toward the 24h limit —
otherwise a user whose payout failed couldn't retry for a day, which
contradicts Q2.
"""

from datetime import timedelta
from decimal import ROUND_HALF_UP, Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db import IS_SQLITE
from app.enums import LedgerEntryType, PayoutStatus
from app.errors import (
    DomainError,
    InsufficientBalance,
    NotFound,
    WithdrawalRateLimited,
)
from app.models import LedgerEntry, Payout, User, new_id, utcnow
from app.services.balance_service import get_withdrawable_balance
from app.state_machine import TERMINAL_FAILURE

ZERO = Decimal("0.00")
TWO_PLACES = Decimal("0.01")

WITHDRAWAL_COOLDOWN = timedelta(hours=24)


def _lock_user(db: Session, user_id: str) -> User:
    if IS_SQLITE:
        # BEGIN IMMEDIATE already holds the write lock
        user = db.get(User, user_id)
    else:
        user = db.execute(
            select(User).where(User.id == user_id).with_for_update()
        ).scalar_one_or_none()

    if user is None:
        raise NotFound(f"user {user_id} not found")
    return user


def find_blocking_payout(db: Session, user_id: str) -> Payout | None:
    """Most recent payout in the 24h window that still counts. Newest first
    so retry_after comes from the payout that actually blocks."""
    cutoff = utcnow() - WITHDRAWAL_COOLDOWN
    return db.execute(
        select(Payout)
        .where(
            Payout.user_id == user_id,
            Payout.status.notin_([s.value for s in TERMINAL_FAILURE]),
            Payout.created_at > cutoff,
        )
        .order_by(Payout.created_at.desc())
        .limit(1)
    ).scalar_one_or_none()


def request_withdrawal(db: Session, user_id: str, amount: Decimal) -> Payout:
    if amount is None or amount <= ZERO:
        raise DomainError(f"withdrawal amount must be positive, got {amount}")

    # a JSON 68.00 parses as Decimal("68.0"); quantise so the returned object
    # matches what gets stored
    amount = amount.quantize(TWO_PLACES, rounding=ROUND_HALF_UP)

    _lock_user(db, user_id)

    blocking = find_blocking_payout(db, user_id)
    if blocking is not None:
        raise WithdrawalRateLimited(retry_after=blocking.created_at + WITHDRAWAL_COOLDOWN)

    balance = get_withdrawable_balance(db, user_id)
    if amount > balance:
        raise InsufficientBalance(requested=amount, available=balance)

    payout = Payout(
        id=new_id(),
        user_id=user_id,
        amount=amount,
        status=PayoutStatus.INITIATED,
        created_at=utcnow(),
        updated_at=utcnow(),
    )
    db.add(payout)
    # flush so the payout row exists before the ledger entry references it —
    # payout_id is a plain FK column with no relationship(), so SQLAlchemy
    # doesn't know to order the inserts itself
    db.flush()

    db.add(
        LedgerEntry(
            id=new_id(),
            user_id=user_id,
            entry_type=LedgerEntryType.WITHDRAWAL_DEBIT,
            amount=-amount,
            payout_id=payout.id,
            idempotency_key=f"withdraw:{payout.id}",
            created_at=utcnow(),
        )
    )

    db.commit()
    return payout


def list_payouts(db: Session, user_id: str) -> list[Payout]:
    return list(
        db.execute(
            select(Payout)
            .where(Payout.user_id == user_id)
            .order_by(Payout.created_at.desc())
        )
        .scalars()
        .all()
    )
