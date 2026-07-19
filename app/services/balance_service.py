"""Withdrawable balance = SUM(ledger_entries.amount) for the user.

The sum is done in Python rather than SQL because SQLite's SUM() over our
TEXT-stored amounts returns a float, and a float balance would flow silently
into the Decimal comparison in the withdrawal check. O(n) per read is fine at
this scale; production would cache a balance column and keep this as the
consistency check.
"""

from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import LedgerEntry

ZERO = Decimal("0.00")


def get_withdrawable_balance(db: Session, user_id: str) -> Decimal:
    amounts = (
        db.execute(select(LedgerEntry.amount).where(LedgerEntry.user_id == user_id))
        .scalars()
        .all()
    )
    return sum(amounts, ZERO)


def get_ledger(db: Session, user_id: str) -> list[LedgerEntry]:
    return list(
        db.execute(
            select(LedgerEntry)
            .where(LedgerEntry.user_id == user_id)
            .order_by(LedgerEntry.created_at, LedgerEntry.id)
        )
        .scalars()
        .all()
    )
