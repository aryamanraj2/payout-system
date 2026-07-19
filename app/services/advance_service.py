"""Advance payout job.

Business rule #1: every pending sale is eligible for an advance of 10% of its
earnings, and once transferred the same sale must NEVER receive another advance
-- however many times the job runs, and however many instances run at once.

The SELECT below filters out sales that already have an advance, but that is
only an optimisation. The actual guarantee is the UNIQUE constraint on
advance_payouts.sale_id: between our SELECT and our INSERT another job instance
may insert the same row, and when it does we take an IntegrityError and skip.
Run this job fifty times concurrently and each sale still gets exactly one
advance.
"""

import logging
from decimal import ROUND_HALF_UP, Decimal

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.enums import SaleStatus
from app.gateway import TransferFailed, transfer_funds_external
from app.models import AdvancePayout, Sale, new_id, utcnow

logger = logging.getLogger(__name__)

ADVANCE_RATE = Decimal("0.10")
TWO_PLACES = Decimal("0.01")


def advance_amount_for(earning: Decimal) -> Decimal:
    """10% of earnings, rounded half-up to paise. Decimal throughout: a float
    here would make 10% of 40 come out as 4.000000000000001."""
    return (earning * ADVANCE_RATE).quantize(TWO_PLACES, rounding=ROUND_HALF_UP)


def find_eligible_sales(db: Session) -> list[Sale]:
    """Pending sales with no advance yet.

    An anti-join (LEFT JOIN ... WHERE right IS NULL) rather than a NOT IN
    subquery: it uses the index on advance_payouts.sale_id and does not have
    NOT IN's NULL-handling trap.
    """
    return list(
        db.execute(
            select(Sale)
            .outerjoin(AdvancePayout, AdvancePayout.sale_id == Sale.id)
            .where(Sale.status == SaleStatus.PENDING, AdvancePayout.id.is_(None))
        )
        .scalars()
        .all()
    )


def run_advance_payout_job(db: Session) -> dict:
    """Pay a 10% advance on every eligible pending sale. Idempotent.

    Returns counts of {advances_paid, skipped, failed}.

    Ordering inside the savepoint matters: we INSERT and FLUSH *before* calling
    the gateway. Flushing is what surfaces the UNIQUE violation, so a job
    instance that loses the race finds out before it moves any money. Doing the
    transfer first would mean the loser pays the user, then rolls back its own
    record of having done so -- a real double-payment.
    """
    paid, skipped, failed = 0, 0, 0

    for sale in find_eligible_sales(db):
        amount = advance_amount_for(sale.earning)
        try:
            with db.begin_nested():  # savepoint: one bad sale can't poison the batch
                db.add(
                    AdvancePayout(
                        id=new_id(),
                        sale_id=sale.id,
                        user_id=sale.user_id,
                        amount=amount,
                        transferred_at=utcnow(),
                    )
                )
                db.flush()  # raises IntegrityError here if another instance won
                transfer_funds_external(sale.user_id, amount, reference=sale.id)
            paid += 1
        except IntegrityError:
            # Another job instance already advanced this sale. Exactly the
            # outcome we want; not an error.
            skipped += 1
        except TransferFailed:
            # Savepoint rolled back, so no advance row exists and the sale
            # stays eligible for the next run.
            logger.warning("advance transfer failed for sale %s", sale.id)
            failed += 1

    db.commit()
    return {"advances_paid": paid, "skipped": skipped, "failed": failed}
