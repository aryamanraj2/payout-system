"""Payout lifecycle updates from the payment gateway (Q2).

Part 1 of this service: the transition core. The gateway reports status
changes -- possibly late, duplicated, or out of order -- and this function
decides which of them are legal before anything else happens.

Three cases:

    same status again   -> no-op, return the payout unchanged. Gateways
                           redeliver webhooks; a redelivery is not an error.
    legal transition    -> apply it and bump updated_at.
    anything else       -> InvalidTransition (409). Terminal states have no
                           outgoing edges in the state machine, so a
                           completed payout can never become failed, and a
                           failed one can never be re-failed into a second
                           refund.

The money side of recovery -- crediting a failed payout back to the
withdrawable balance -- is layered on top of this in part 2. Keeping the
transition rules separate means they can be tested exhaustively without any
ledger involvement.
"""

from sqlalchemy.orm import Session

from app.enums import PayoutStatus
from app.errors import InvalidTransition, NotFound
from app.models import Payout, utcnow
from app.state_machine import can_transition


def handle_payout_status_update(
    db: Session, payout_id: str, new_status: PayoutStatus
) -> Payout:
    """Apply a gateway-reported status change to a payout.

    Idempotent for redeliveries: reporting the current status again is a
    no-op, not a conflict.
    """
    # with_for_update: no-op on SQLite (BEGIN IMMEDIATE already holds the
    # write lock), real row lock on Postgres -- same pattern as reconciliation.
    payout = db.get(Payout, payout_id, with_for_update=True)
    if payout is None:
        raise NotFound(f"payout {payout_id} not found")

    if new_status == payout.status:
        return payout  # duplicate webhook: swallow silently

    if not can_transition(payout.status, new_status):
        # .value on both sides: an f-string on a str-Enum member renders
        # "PayoutStatus.COMPLETED" on Python 3.11+, and payout.status is a
        # plain str or an enum depending on whether the row came from the DB
        # or this session. Normalise so clients always see "completed".
        raise InvalidTransition(
            f"payout {payout_id} cannot go "
            f"{PayoutStatus(payout.status).value} -> {new_status.value}"
        )

    payout.status = new_status
    payout.updated_at = utcnow()

    db.commit()
    return payout
