"""Payout lifecycle updates from the payment gateway (Q2).

Two layers:

1. The transition core. The gateway reports status changes -- possibly late,
   duplicated, or out of order -- and the state machine decides which are
   legal:

    same status again   -> no-op, return the payout unchanged. Gateways
                           redeliver webhooks; a redelivery is not an error.
    legal transition    -> apply it and bump updated_at.
    anything else       -> InvalidTransition (409). Terminal states have no
                           outgoing edges, so a completed payout can never
                           become failed.

2. The recovery (Q2). When a payout lands in a terminal failure --
   failed, cancelled, or rejected -- the debited amount is credited back to
   the withdrawable balance as a WITHDRAWAL_REVERSAL entry.

   The reversal is a COMPENSATING ENTRY, never a mutation or deletion of the
   original debit. The audit trail permanently shows both: debit -68,
   reversal +68. A balance that says 68 again is the *sum* telling the truth,
   not history being rewritten.

   Double-credit is impossible twice over, by two independent mechanisms:
     - the state machine: a payout already in a terminal state accepts no
       further transitions, so the reversal code is unreachable a second time;
     - the ledger's UNIQUE idempotency key `reversal:{payout_id}`: even if the
       state machine were somehow bypassed (bug, manual DB edit, a future
       refactor), the second INSERT collides and is swallowed as a no-op.
   Belt and braces, because this is the one place the system creates balance
   out of thin air.
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

    if new_status in TERMINAL_FAILURE:
        _write_reversal(db, payout)

    db.commit()
    return payout


def _write_reversal(db: Session, payout: Payout) -> None:
    """Credit the failed payout back, exactly once.

    The savepoint + explicit flush matter: flush is what forces the INSERT to
    the database *inside* begin_nested(), so a duplicate `reversal:{id}` key
    raises IntegrityError here, rolls back only the savepoint, and leaves the
    status change (made outside it) intact. Without the flush the INSERT would
    ride along with the outer commit, where the IntegrityError would abort the
    whole transaction, status change included.
    """
    try:
        with db.begin_nested():
            db.add(
                LedgerEntry(
                    id=new_id(),
                    user_id=payout.user_id,
                    entry_type=LedgerEntryType.WITHDRAWAL_REVERSAL,
                    amount=payout.amount,  # positive: money returns
                    payout_id=payout.id,
                    idempotency_key=f"reversal:{payout.id}",
                    created_at=utcnow(),
                )
            )
            db.flush()
    except IntegrityError:
        # A reversal for this payout already exists. The user has their money;
        # doing nothing is the correct outcome.
        pass
