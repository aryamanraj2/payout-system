"""Advance payout job: 10% of earnings for every pending sale, exactly once.

The eligibility SELECT is just an optimisation — the real guarantee is the
unique constraint on advance_payouts.sale_id. If two job instances race, the
loser gets an IntegrityError and skips.
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
    return (earning * ADVANCE_RATE).quantize(TWO_PLACES, rounding=ROUND_HALF_UP)


def find_eligible_sales(db: Session) -> list[Sale]:
    """Pending sales that don't have an advance yet (anti-join)."""
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
    """Idempotent; safe to run repeatedly and concurrently."""
    paid, skipped, failed = 0, 0, 0

    for sale in find_eligible_sales(db):
        amount = advance_amount_for(sale.earning)
        try:
            # savepoint per sale so one failure doesn't kill the batch
            with db.begin_nested():
                db.add(
                    AdvancePayout(
                        id=new_id(),
                        sale_id=sale.id,
                        user_id=sale.user_id,
                        amount=amount,
                        transferred_at=utcnow(),
                    )
                )
                # flush BEFORE the transfer: if another instance already
                # advanced this sale, the IntegrityError fires here — before
                # any money moves. The other order would pay the user and then
                # roll back the record of it.
                db.flush()
                transfer_funds_external(sale.user_id, amount, reference=sale.id)
            paid += 1
        except IntegrityError:
            skipped += 1  # another instance won the race, fine
        except TransferFailed:
            # savepoint rolled back, sale stays eligible for the next run
            logger.warning("advance transfer failed for sale %s", sale.id)
            failed += 1

    db.commit()
    return {"advances_paid": paid, "skipped": skipped, "failed": failed}
