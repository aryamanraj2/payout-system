"""Payout state transitions and failed payout recovery (Q2)."""

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
    """An initiated payout of Rs 68, built through the real flow."""
    run_advance_payout_job(db)
    reconcile_sale(db, "sale_0", SaleStatus.REJECTED)
    reconcile_sale(db, "sale_1", SaleStatus.APPROVED)
    reconcile_sale(db, "sale_2", SaleStatus.APPROVED)
    return request_withdrawal(db, "john_doe", Decimal("68.00"))


# --- transitions ---


def test_initiated_payout_can_start_processing(db, payout):
    updated = handle_payout_status_update(db, payout.id, PayoutStatus.PROCESSING)
    assert updated.status == PayoutStatus.PROCESSING


def test_processing_payout_can_complete(db, payout):
    handle_payout_status_update(db, payout.id, PayoutStatus.PROCESSING)
    updated = handle_payout_status_update(db, payout.id, PayoutStatus.COMPLETED)
    assert updated.status == PayoutStatus.COMPLETED


def test_duplicate_webhook_is_a_silent_noop(db, payout):
    handle_payout_status_update(db, payout.id, PayoutStatus.PROCESSING)
    stamp = db.get(type(payout), payout.id).updated_at

    again = handle_payout_status_update(db, payout.id, PayoutStatus.PROCESSING)

    assert again.status == PayoutStatus.PROCESSING
    assert again.updated_at == stamp


def test_completed_cannot_become_failed(db, payout):
    # a late "failed" webhook must not un-complete a payout
    handle_payout_status_update(db, payout.id, PayoutStatus.PROCESSING)
    handle_payout_status_update(db, payout.id, PayoutStatus.COMPLETED)

    with pytest.raises(InvalidTransition, match="completed -> failed"):
        handle_payout_status_update(db, payout.id, PayoutStatus.FAILED)


def test_initiated_cannot_skip_to_completed(db, payout):
    with pytest.raises(InvalidTransition):
        handle_payout_status_update(db, payout.id, PayoutStatus.COMPLETED)


def test_no_terminal_state_has_an_exit(db, payout):
    """The terminal list is hard-coded on purpose: deriving it from
    PAYOUT_TRANSITIONS would make this test agree with whatever the map says,
    including a broken edit to it."""
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


# --- reversal ---


@pytest.mark.parametrize(
    "route",
    [
        [PayoutStatus.FAILED],
        [PayoutStatus.CANCELLED],
        [PayoutStatus.PROCESSING, PayoutStatus.FAILED],
        [PayoutStatus.PROCESSING, PayoutStatus.REJECTED],
    ],
    ids=["failed", "cancelled", "processing-failed", "processing-rejected"],
)
def test_terminal_failure_credits_amount_back(db, payout, route):
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
    # the ledger keeps both movements: debit -68 and reversal +68
    handle_payout_status_update(db, payout.id, PayoutStatus.FAILED)

    entries = db.query(LedgerEntry).filter_by(payout_id=payout.id).all()
    by_type = {e.entry_type: e.amount for e in entries}
    assert by_type == {
        LedgerEntryType.WITHDRAWAL_DEBIT: Decimal("-68.00"),
        LedgerEntryType.WITHDRAWAL_REVERSAL: Decimal("68.00"),
    }


def test_completed_payout_is_never_reversed(db, payout):
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
    handle_payout_status_update(db, payout.id, PayoutStatus.FAILED)
    handle_payout_status_update(db, payout.id, PayoutStatus.FAILED)
    handle_payout_status_update(db, payout.id, PayoutStatus.FAILED)

    assert get_withdrawable_balance(db, "john_doe") == Decimal("68.00")
    assert db.query(LedgerEntry).filter_by(payout_id=payout.id).count() == 2


def test_reversal_unique_key_holds_even_if_state_machine_is_bypassed(db, payout):
    """If the terminal state were somehow rewound (bug, manual DB edit), the
    unique reversal key must still block a second credit — and the status
    change itself must survive the rejected insert."""
    handle_payout_status_update(db, payout.id, PayoutStatus.FAILED)
    assert get_withdrawable_balance(db, "john_doe") == Decimal("68.00")

    payout.status = PayoutStatus.PROCESSING  # rewind behind the service's back
    db.commit()

    updated = handle_payout_status_update(db, payout.id, PayoutStatus.FAILED)

    assert updated.status == PayoutStatus.FAILED
    assert get_withdrawable_balance(db, "john_doe") == Decimal("68.00")
    assert (
        db.query(LedgerEntry)
        .filter_by(idempotency_key=f"reversal:{payout.id}")
        .count()
        == 1
    )


# --- Q2 end to end ---


def test_user_can_withdraw_again_after_failure(db, payout):
    """Withdraw 68 -> gateway fails it -> balance restored -> withdraw the
    same 68 again immediately. Needs both the reversal and the 24h exclusion
    of failed payouts to work together."""
    handle_payout_status_update(db, payout.id, PayoutStatus.FAILED)
    assert get_withdrawable_balance(db, "john_doe") == Decimal("68.00")

    retry = request_withdrawal(db, "john_doe", Decimal("68.00"))

    assert retry.id != payout.id
    assert retry.status == PayoutStatus.INITIATED
    assert get_withdrawable_balance(db, "john_doe") == Decimal("0.00")

    entries = db.query(LedgerEntry).filter(LedgerEntry.payout_id.isnot(None)).all()
    types = sorted(e.entry_type for e in entries)
    assert types == [
        LedgerEntryType.WITHDRAWAL_DEBIT,
        LedgerEntryType.WITHDRAWAL_DEBIT,
        LedgerEntryType.WITHDRAWAL_REVERSAL,
    ]


def test_retry_payout_can_complete_normally(db, payout):
    handle_payout_status_update(db, payout.id, PayoutStatus.FAILED)
    retry = request_withdrawal(db, "john_doe", Decimal("68.00"))

    handle_payout_status_update(db, retry.id, PayoutStatus.PROCESSING)
    handle_payout_status_update(db, retry.id, PayoutStatus.COMPLETED)

    assert get_withdrawable_balance(db, "john_doe") == Decimal("0.00")
    assert (
        db.query(LedgerEntry)
        .filter_by(idempotency_key=f"reversal:{retry.id}")
        .count()
        == 0
    )


def test_partial_rewithdrawal_after_failure(db, payout):
    handle_payout_status_update(db, payout.id, PayoutStatus.CANCELLED)

    request_withdrawal(db, "john_doe", Decimal("30.00"))

    assert get_withdrawable_balance(db, "john_doe") == Decimal("38.00")


def test_failure_loop_is_repeatable(db, payout):
    # withdraw -> fail -> withdraw -> fail -> withdraw
    current = payout
    for _ in range(3):
        handle_payout_status_update(db, current.id, PayoutStatus.FAILED)
        assert get_withdrawable_balance(db, "john_doe") == Decimal("68.00")
        current = request_withdrawal(db, "john_doe", Decimal("68.00"))
        assert get_withdrawable_balance(db, "john_doe") == Decimal("0.00")

    assert db.query(LedgerEntry).filter_by(
        entry_type=LedgerEntryType.WITHDRAWAL_DEBIT
    ).count() == 4
    assert db.query(LedgerEntry).filter_by(
        entry_type=LedgerEntryType.WITHDRAWAL_REVERSAL
    ).count() == 3
