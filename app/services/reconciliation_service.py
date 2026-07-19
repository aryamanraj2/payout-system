"""Reconciliation: admin moves a pending sale to approved/rejected and the
result is netted against any advance already paid.

    approved:  FINAL_CREDIT          +(earning - advance)
    rejected:  REJECTION_ADJUSTMENT  -advance

Assignment example (3 x Rs 40, Rs 4 advanced each, one rejected):
-4 + 36 + 36 = 68.
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
    """Status change and ledger entry commit together. A sale reconciles
    exactly once — the second attempt is a 409."""
    # row lock on Postgres; no-op on SQLite (BEGIN IMMEDIATE covers it)
    sale = db.get(Sale, sale_id, with_for_update=True)
    if sale is None:
        raise NotFound(f"sale {sale_id} not found")

    if new_status not in TERMINAL_SALE_STATUSES:
        raise InvalidTransition(
            f"cannot reconcile to {new_status}; expected approved or rejected"
        )

    if sale.status != SaleStatus.PENDING:
        # .value: f-strings on str-enums print "SaleStatus.APPROVED" on 3.11+
        raise InvalidTransition(
            f"sale {sale_id} is already {SaleStatus(sale.status).value}; "
            f"sales reconcile exactly once"
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

    # rejected sale that never got an advance: nothing to record
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
