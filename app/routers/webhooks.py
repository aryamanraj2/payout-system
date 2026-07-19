"""Gateway webhook surface.

In production this endpoint would verify a gateway signature (HMAC over the
raw body) before trusting the payload -- anyone who can reach an unsigned
webhook can mark payouts failed and mint reversals. Out of scope for the
assignment, noted in the README.

The interesting property is what is NOT here: no idempotency handling, no
duplicate detection, no ordering logic. Redeliveries and illegal reports are
the service layer's problem, already solved by the same-status no-op, the
state machine, and the reversal's unique key. The router stays a thin
translation layer.
"""

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app.db import get_db
from app.schemas import PayoutOut, PayoutWebhook
from app.services.recovery_service import handle_payout_status_update

router = APIRouter(prefix="/webhooks", tags=["webhooks"])


@router.post("/payouts/{payout_id}", response_model=PayoutOut)
def payout_status_webhook(
    payout_id: str, payload: PayoutWebhook, db: Session = Depends(get_db)
):
    """Gateway callback: a payout's status changed.

    200 with the payout (redeliveries included), 404 unknown payout,
    409 illegal transition. A terminal failure credits the amount back to the
    user's withdrawable balance exactly once.
    """
    return handle_payout_status_update(db, payout_id, payload.status)
