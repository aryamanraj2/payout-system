from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app.db import get_db
from app.schemas import AdvanceJobResult
from app.services.advance_service import run_advance_payout_job

router = APIRouter(prefix="/jobs", tags=["jobs"])


@router.post("/advance-payouts/run", response_model=AdvanceJobResult)
def trigger_advance_payout_job(db: Session = Depends(get_db)):
    """Pay 10% advances on all eligible pending sales.

    Safe to call repeatedly and concurrently: already-advanced sales come back
    under `skipped`, never as a second payment.
    """
    return run_advance_payout_job(db)
