"""Withdrawable balance, derived from the ledger.

    withdrawable_balance(user) = SUM(ledger_entries.amount) WHERE user_id = ?

Advances are deliberately not in the ledger: that money already left the
system, so it is never withdrawable. The ledger records only what
reconciliation and withdrawals do.

Why the sum happens in Python rather than in SQL
------------------------------------------------
Money is stored as TEXT (see models.Money) to keep decimals exact. SQLite's
SUM() over a TEXT column coerces to REAL and hands back a Python float --
verified: `SELECT typeof(SUM(amount))` returns 'real'.

To be precise about the risk, since it is easy to overstate: I could not
produce actual drift at paise scale, not even summing 5000 random ledger rows.
SQLite's shortest-round-trip float printing hides it well. So the argument here
is about *type*, not observed error.

A float balance is still wrong, because Python compares Decimal and float
without complaining. `Decimal("10.00") > 9.999999999999998` just quietly
returns a result, so a float leaking out of the balance function would flow
straight into the withdrawal comparison with no error to alert anyone -- the
one place in this system where a wrong comparison means paying out money that
does not exist. Summing Decimals keeps a single exact type end to end and
removes the question.

The cost is O(n) in ledger rows per read, which is irrelevant at this scale.
The production path is a cached `balance` column updated in the same
transaction as each ledger insert, with this function kept as the invariant
check that proves the cache has not drifted.
"""

from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import LedgerEntry

ZERO = Decimal("0.00")


def get_withdrawable_balance(db: Session, user_id: str) -> Decimal:
    """Exact sum of the user's ledger. Zero when the user has no entries."""
    amounts = (
        db.execute(select(LedgerEntry.amount).where(LedgerEntry.user_id == user_id))
        .scalars()
        .all()
    )
    return sum(amounts, ZERO)


def get_ledger(db: Session, user_id: str) -> list[LedgerEntry]:
    """Full audit trail, oldest first."""
    return list(
        db.execute(
            select(LedgerEntry)
            .where(LedgerEntry.user_id == user_id)
            .order_by(LedgerEntry.created_at, LedgerEntry.id)
        )
        .scalars()
        .all()
    )
