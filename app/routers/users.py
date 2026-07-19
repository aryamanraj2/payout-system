from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app.db import get_db
from app.errors import NotFound
from app.models import User
from app.schemas import BalanceOut, LedgerEntryOut, PayoutOut, WithdrawalRequest
from app.services.balance_service import get_ledger, get_withdrawable_balance
from app.services.withdrawal_service import list_payouts, request_withdrawal

router = APIRouter(prefix="/users", tags=["users"])


def _require_user(db: Session, user_id: str) -> User:
    user = db.get(User, user_id)
    if user is None:
        raise NotFound(f"user {user_id} not found")
    return user


@router.get("/{user_id}/balance", response_model=BalanceOut)
def read_balance(user_id: str, db: Session = Depends(get_db)):
    """Withdrawable balance = SUM(ledger). Advances are excluded by design:
    that money already left the system."""
    _require_user(db, user_id)
    return BalanceOut(
        user_id=user_id, withdrawable_balance=get_withdrawable_balance(db, user_id)
    )


@router.get("/{user_id}/ledger", response_model=list[LedgerEntryOut])
def read_ledger(user_id: str, db: Session = Depends(get_db)):
    """Full append-only audit trail, oldest first."""
    _require_user(db, user_id)
    return get_ledger(db, user_id)


@router.post("/{user_id}/withdrawals", response_model=PayoutOut, status_code=201)
def create_withdrawal(
    user_id: str, payload: WithdrawalRequest, db: Session = Depends(get_db)
):
    """Initiate a withdrawal.

    422 if the balance is insufficient, 429 if one was already made in the last
    24 hours (failed/cancelled/rejected payouts do not count).
    """
    return request_withdrawal(db, user_id, payload.amount)


@router.get("/{user_id}/payouts", response_model=list[PayoutOut])
def read_payouts(user_id: str, db: Session = Depends(get_db)):
    _require_user(db, user_id)
    return list_payouts(db, user_id)
