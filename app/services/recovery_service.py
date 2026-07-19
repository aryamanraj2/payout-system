"""Payout status updates from the gateway, including Q2 failure recovery.

Same status again -> no-op (gateways redeliver webhooks). Legal transition ->
apply. Anything else -> 409. When a payout lands in failed/cancelled/rejected,
the amount is credited back as a WITHDRAWAL_REVERSAL entry — the original
debit is never touched, the ledger keeps both rows.

A double credit is blocked twice over: terminal states have no exits in the
state machine, and the reversal:{payout_id} unique key rejects a second
insert even if the state were somehow rewound.
"""

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.enums import LedgerEntryType, PayoutStatus
from app.errors import InvalidTransition, NotFound
from app.models import LedgerEntry, Payout, new_id, utcnow
from app.state_machine import TERMINAL_FAILURE, can_transition


def handle_payout_status_update(
    db: Session, payout_id: str, new_status: PayoutStatus
) -> Payout:
    # row lock on Postgres; no-op on SQLite
    payout = db.get(Payout, payout_id, with_for_update=True)
    if payout is None:
        raise NotFound(f"payout {payout_id} not found")

    if new_status == payout.status:
        return payout  # duplicate webhook

    if not can_transition(payout.status, new_status):
        # .value: f-strings on str-enums print "PayoutStatus.X" on 3.11+
        raise InvalidTransition(
            f"payout {payout_id} cannot go "
            f"{PayoutStatus(payout.status).value} -> {new_status.value}"
        )

    payout.status = new_status
    payout.updated_at = utcnow()

    if new_status in TERMINAL_FAILURE:
        _write_reversal(db, payout)

    db.commit()
    return payout


def _write_reversal(db: Session, payout: Payout) -> None:
    """Credit the failed payout back, exactly once. The savepoint means a
    duplicate reversal key rolls back only the reversal, not the status
    change made above it."""
    try:
        with db.begin_nested():
            db.add(
                LedgerEntry(
                    id=new_id(),
                    user_id=payout.user_id,
                    entry_type=LedgerEntryType.WITHDRAWAL_REVERSAL,
                    amount=payout.amount,
                    payout_id=payout.id,
                    idempotency_key=f"reversal:{payout.id}",
                    created_at=utcnow(),
                )
            )
            db.flush()
    except IntegrityError:
        pass  # reversal already exists, user already has the money
