"""Stage 5.1: payout state transitions.

Only the transition rules are under test here -- which status changes the
gateway is allowed to report. The reversal (money coming back) is 5.2.
"""

from decimal import Decimal

import pytest

from app.enums import PayoutStatus, SaleStatus
from app.errors import InvalidTransition, NotFound
from app.services.advance_service import run_advance_payout_job
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
