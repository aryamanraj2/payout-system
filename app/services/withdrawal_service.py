"""Withdrawals: the concurrency hot path.

Two rules meet here.

Business rule #3 -- one withdrawal per 24 hours.

    Failed, cancelled and rejected payouts are deliberately EXCLUDED from that
    count. Question 2 requires that a user whose payout failed can "initiate
    another withdrawal for that amount"; if a failed payout still occupied the
    24h slot, that requirement would be unsatisfiable for a whole day. This is
    an interpretation, and it is documented in the README as such.

Double-spend prevention -- the balance check and the balance change are one
atomic unit.

    The debit is written at INITIATION, not at completion. That is what makes
    the check meaningful: two racing requests cannot both read a balance of 68
    and both pass, because the first to commit has already written its -68 row
    and the second re-reads inside its own serialised transaction.

    On SQLite, `BEGIN IMMEDIATE` (see db.py) takes the write lock at the start
    of every transaction, so the balance read is already inside the lock. On
    Postgres the equivalent is `SELECT ... FOR UPDATE` on the user row, which
    is why the lock below is taken explicitly rather than left implicit.

    The cost is money "in flight" while a payout processes. A failure returns
    it via a compensating reversal entry (Stage 5), never by deleting the
    debit.
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

# Module-level so tests can shrink it; production value is the business rule.
WITHDRAWAL_COOLDOWN = timedelta(hours=24)


def _lock_user(db: Session, user_id: str) -> User:
    """Serialise concurrent withdrawals for this user.

    SQLite: BEGIN IMMEDIATE has already taken a database-wide write lock, so
    this is just the existence check. Postgres: SELECT ... FOR UPDATE locks the
    user row for the rest of the transaction.
    """
    if IS_SQLITE:
        user = db.get(User, user_id)
    else:
        user = db.execute(
            select(User).where(User.id == user_id).with_for_update()
        ).scalar_one_or_none()

    if user is None:
        raise NotFound(f"user {user_id} not found")
    return user


def find_blocking_payout(db: Session, user_id: str) -> Payout | None:
    """The most recent payout inside the cooldown window that still counts.

    Ordered newest-first so `retry_after` is computed from the payout that
    actually blocks, not an arbitrary row.
    """
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
    """Initiate a withdrawal: create the payout and debit the ledger atomically."""
    if amount is None or amount <= ZERO:
        raise DomainError(f"withdrawal amount must be positive, got {amount}")

    # Normalise at the boundary. A JSON body of {"amount": 68.00} parses to
    # Decimal("68.0"), and because the session does not expire on commit the
    # returned object keeps that unquantised value -- so the API would answer
    # "68.0" here while the ledger, which quantises on write, says "68.00".
    # Quantising up front makes the in-memory object and the stored row agree.
    amount = amount.quantize(TWO_PLACES, rounding=ROUND_HALF_UP)

    _lock_user(db, user_id)

    # Rate limit before balance: the 24h rule is an absolute precondition, and
    # telling a user to earn more when they could not withdraw today anyway
    # would be misleading.
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
    # Flush so the payout row exists before the ledger entry references it.
    # SQLAlchemy orders inserts by mapper *relationships*, and payout_id is a
    # bare ForeignKey column with no relationship() behind it -- without this
    # the ledger insert is emitted first and trips the FK constraint. Both
    # statements are still in one transaction, so atomicity is unaffected.
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
