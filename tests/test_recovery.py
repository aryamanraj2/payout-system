"""Stage 5.1: payout state transitions.

Only the transition rules are under test here -- which status changes the
gateway is allowed to report. The reversal (money coming back) is 5.2.
"""

from decimal import Decimal

import pytest

from app.enums import LedgerEntryType, PayoutStatus, SaleStatus
from app.errors import InvalidTransition, NotFound
from app.models import LedgerEntry
from app.services.advance_service import run_advance_payout_job
from app.services.balance_service import get_withdrawable_balance
from app.services.reconciliation_service import reconcile_sale
from app.services.recovery_service import handle_payout_status_update
from app.services.withdrawal_service import request_withdrawal
from app.state_machine import PAYOUT_TRANSITIONS


@pytest.fixture
def payout(db, three_sales):
    """A live initiated payout of Rs 68, built through the real flow."""
    run_advance_payout_job(db)
    reconcile_sale(db, "sale_0", SaleStatus.REJECTED)
    reconcile_sale(db, "sale_1", SaleStatus.APPROVED)
    reconcile_sale(db, "sale_2", SaleStatus.APPROVED)
    return request_withdrawal(db, "john_doe", Decimal("68.00"))


def test_initiated_payout_can_start_processing(db, payout):
    updated = handle_payout_status_update(db, payout.id, PayoutStatus.PROCESSING)
    assert updated.status == PayoutStatus.PROCESSING


def test_processing_payout_can_complete(db, payout):
    handle_payout_status_update(db, payout.id, PayoutStatus.PROCESSING)
    updated = handle_payout_status_update(db, payout.id, PayoutStatus.COMPLETED)
    assert updated.status == PayoutStatus.COMPLETED


def test_duplicate_webhook_is_a_silent_noop(db, payout):
    """Gateways redeliver. Reporting the current status again must succeed
    without changing anything -- including updated_at."""
    handle_payout_status_update(db, payout.id, PayoutStatus.PROCESSING)
    stamp = db.get(type(payout), payout.id).updated_at

    again = handle_payout_status_update(db, payout.id, PayoutStatus.PROCESSING)

    assert again.status == PayoutStatus.PROCESSING
    assert again.updated_at == stamp


def test_completed_cannot_become_failed(db, payout):
    """EDGE CASE from the plan: a late or malicious 'failed' webhook after
    completion must not un-complete the payout (which would trigger a refund
    for money the user actually received)."""
    handle_payout_status_update(db, payout.id, PayoutStatus.PROCESSING)
    handle_payout_status_update(db, payout.id, PayoutStatus.COMPLETED)

    with pytest.raises(InvalidTransition, match="completed -> failed"):
        handle_payout_status_update(db, payout.id, PayoutStatus.FAILED)


def test_initiated_cannot_skip_to_completed(db, payout):
    """A payout must pass through processing; a completion report for a payout
    the gateway was never asked to process is suspect."""
    with pytest.raises(InvalidTransition):
        handle_payout_status_update(db, payout.id, PayoutStatus.COMPLETED)


def test_no_terminal_state_has_an_exit(db, payout):
    """Exhaustive: from every terminal state, every other status is rejected.

    The terminal set is HARD-CODED here, not derived from PAYOUT_TRANSITIONS.
    Deriving it would make the test self-referential: sabotaging the map (e.g.
    giving completed an exit to failed) silently removes that state from the
    derived list and the test still passes. Verified by exactly that sabotage.
    """
    terminals = [
        PayoutStatus.COMPLETED,
        PayoutStatus.FAILED,
        PayoutStatus.CANCELLED,
        PayoutStatus.REJECTED,
    ]
    for t in terminals:
        assert not PAYOUT_TRANSITIONS[t], f"{t.value} must be terminal but has exits"

    for terminal in terminals:
        payout.status = terminal
        db.commit()
        for target in PayoutStatus:
            if target == terminal:
                continue
            with pytest.raises(InvalidTransition):
                handle_payout_status_update(db, payout.id, target)


def test_unknown_payout_is_not_found(db, john):
    with pytest.raises(NotFound):
        handle_payout_status_update(db, "nope", PayoutStatus.FAILED)


# --------------------------------------------------------------------------
# Stage 5.2: the reversal -- Q2's "credit the failed payout amount back".
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "route",
    [
        [PayoutStatus.FAILED],                              # initiated -> failed
        [PayoutStatus.CANCELLED],                           # initiated -> cancelled
        [PayoutStatus.PROCESSING, PayoutStatus.FAILED],     # via processing
        [PayoutStatus.PROCESSING, PayoutStatus.REJECTED],   # via processing
    ],
    ids=["failed", "cancelled", "processing-failed", "processing-rejected"],
)
def test_terminal_failure_credits_amount_back(db, payout, route):
    """Q2: every terminal-failure route restores the withdrawable balance."""
    assert get_withdrawable_balance(db, "john_doe") == Decimal("0.00")

    for status in route:
        handle_payout_status_update(db, payout.id, status)

    assert get_withdrawable_balance(db, "john_doe") == Decimal("68.00")

    reversal = db.query(LedgerEntry).filter_by(
        idempotency_key=f"reversal:{payout.id}"
    ).one()
    assert reversal.entry_type == LedgerEntryType.WITHDRAWAL_REVERSAL
    assert reversal.amount == Decimal("68.00")


def test_reversal_compensates_rather_than_deletes(db, payout):
    """The audit trail keeps both movements forever: debit -68, reversal +68.

    Recovery must never rewrite history -- a regulator (or a confused user)
    can always see the money leave and come back.
    """
    handle_payout_status_update(db, payout.id, PayoutStatus.FAILED)

    entries = db.query(LedgerEntry).filter_by(payout_id=payout.id).all()
    by_type = {e.entry_type: e.amount for e in entries}
    assert by_type == {
        LedgerEntryType.WITHDRAWAL_DEBIT: Decimal("-68.00"),
        LedgerEntryType.WITHDRAWAL_REVERSAL: Decimal("68.00"),
    }


def test_completed_payout_is_never_reversed(db, payout):
    """The money genuinely left: no reversal, and the late 'failed' webhook
    that would have created one is rejected."""
    handle_payout_status_update(db, payout.id, PayoutStatus.PROCESSING)
    handle_payout_status_update(db, payout.id, PayoutStatus.COMPLETED)

    with pytest.raises(InvalidTransition):
        handle_payout_status_update(db, payout.id, PayoutStatus.FAILED)

    assert get_withdrawable_balance(db, "john_doe") == Decimal("0.00")
    assert (
        db.query(LedgerEntry)
        .filter_by(idempotency_key=f"reversal:{payout.id}")
        .count()
        == 0
    )


def test_duplicate_failure_webhook_credits_once(db, payout):
    """A redelivered 'failed' webhook is a same-status no-op: one reversal."""
    handle_payout_status_update(db, payout.id, PayoutStatus.FAILED)
    handle_payout_status_update(db, payout.id, PayoutStatus.FAILED)
    handle_payout_status_update(db, payout.id, PayoutStatus.FAILED)

    assert get_withdrawable_balance(db, "john_doe") == Decimal("68.00")
    assert db.query(LedgerEntry).filter_by(payout_id=payout.id).count() == 2


def test_reversal_unique_key_holds_even_if_state_machine_is_bypassed(db, payout):
    """DEFENSE IN DEPTH. The state machine makes a second reversal unreachable;
    this test asks what happens if that layer is lost -- a bug, a manual DB
    edit, a future refactor. Simulate it by resetting the payout to processing
    behind the service's back, then failing it again. The UNIQUE
    `reversal:{payout_id}` key must swallow the second credit, and the status
    change itself must survive (that is what the savepoint+flush protect).
    """
    handle_payout_status_update(db, payout.id, PayoutStatus.FAILED)
    assert get_withdrawable_balance(db, "john_doe") == Decimal("68.00")

    payout.status = PayoutStatus.PROCESSING  # bypass: rewind terminal state
    db.commit()

    updated = handle_payout_status_update(db, payout.id, PayoutStatus.FAILED)

    assert updated.status == PayoutStatus.FAILED  # transition survived
    assert get_withdrawable_balance(db, "john_doe") == Decimal("68.00")  # no 2nd credit
    assert (
        db.query(LedgerEntry)
        .filter_by(idempotency_key=f"reversal:{payout.id}")
        .count()
        == 1
    )
