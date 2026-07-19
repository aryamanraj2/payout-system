"""Gateway webhook. Production would verify a gateway signature (HMAC) before
trusting the payload — skipped here, noted in the README."""

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
    """200 on success (including redeliveries), 404 unknown payout, 409
    illegal transition. Terminal failures credit the amount back exactly once."""
    return handle_payout_status_update(db, payout_id, payload.status)
