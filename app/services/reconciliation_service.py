"""Reconciliation: the final payout calculation from the assignment.

An administrator moves a pending sale to approved or rejected, and the system
nets the result against whatever advance was already transferred.

    Sale with earning E, advance A already paid out:

      approved  ->  FINAL_CREDIT          +(E - A)   "owed E, already got A"
      rejected  ->  REJECTION_ADJUSTMENT  -A         "owed 0, already got A"

The PDF's worked example, 3 sales of Rs 40 with Rs 4 advanced on each:

      rejected:  -4
      approved:  +36
      approved:  +36
                 ---
                  68

A sale is reconciled exactly once. The second attempt is a 409, not a silent
overwrite -- re-reconciling would double-count the adjustment and there is no
legitimate reason to do it.
"""

from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.enums import LedgerEntryType, SaleStatus
from app.errors import InvalidTransition, NotFound
from app.models import AdvancePayout, LedgerEntry, Sale, new_id, utcnow

ZERO = Decimal("0.00")

TERMINAL_SALE_STATUSES = (SaleStatus.APPROVED, SaleStatus.REJECTED)


def reconcile_sale(db: Session, sale_id: str, new_status: SaleStatus) -> Sale:
    """Move a pending sale to approved/rejected and write its ledger entry.

    The status change and the ledger entry commit together, so there is no
    window in which a sale reads as approved but its credit is missing.
    """
    # with_for_update is a no-op on SQLite (BEGIN IMMEDIATE already serialises
    # writers) and a real row lock on Postgres.
    sale = db.get(Sale, sale_id, with_for_update=True)
    if sale is None:
        raise NotFound(f"sale {sale_id} not found")

    if new_status not in TERMINAL_SALE_STATUSES:
        raise InvalidTransition(
            f"cannot reconcile to {new_status}; expected approved or rejected"
        )

    if sale.status != SaleStatus.PENDING:
        raise InvalidTransition(
            f"sale {sale_id} is already {sale.status}; sales reconcile exactly once"
        )

    advance = db.execute(
        select(AdvancePayout).where(AdvancePayout.sale_id == sale_id)
    ).scalar_one_or_none()
    advance_amount = advance.amount if advance else ZERO

    if new_status == SaleStatus.APPROVED:
        entry_type = LedgerEntryType.FINAL_CREDIT
        amount = sale.earning - advance_amount
        key = f"final:{sale_id}"
    else:
        entry_type = LedgerEntryType.REJECTION_ADJUSTMENT
        amount = -advance_amount
        key = f"reject:{sale_id}"

    sale.status = new_status
    sale.reconciled_at = utcnow()

    # A rejected sale that never received an advance owes nothing back, so
    # there is nothing to record. Writing a zero row would be noise in the
    # audit trail.
    if amount != ZERO:
        db.add(
            LedgerEntry(
                id=new_id(),
                user_id=sale.user_id,
                entry_type=entry_type,
                amount=amount,
                sale_id=sale_id,
                idempotency_key=key,
                created_at=utcnow(),
            )
        )

    db.commit()
    return sale
