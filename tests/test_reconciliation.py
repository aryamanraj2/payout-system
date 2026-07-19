"""Stage 3: reconciliation and the final payout calculation."""

from decimal import Decimal

import pytest

from app.enums import LedgerEntryType, SaleStatus
from app.errors import InvalidTransition, NotFound
from app.models import LedgerEntry, Sale
from app.services.advance_service import run_advance_payout_job
from app.services.balance_service import get_withdrawable_balance
from app.services.reconciliation_service import reconcile_sale


def test_example_from_assignment_totals_68(db, three_sales):
    """THE WORKED EXAMPLE FROM THE PDF.

    3 sales of Rs 40, Rs 4 advanced on each (Rs 12 total).
    One rejected, two approved:

        rejected  -4
        approved  +36
        approved  +36
        --------------
        total      68
    """
    run_advance_payout_job(db)

    reconcile_sale(db, "sale_0", SaleStatus.REJECTED)
    reconcile_sale(db, "sale_1", SaleStatus.APPROVED)
    reconcile_sale(db, "sale_2", SaleStatus.APPROVED)

    assert get_withdrawable_balance(db, "john_doe") == Decimal("68.00")

    amounts = sorted(e.amount for e in db.query(LedgerEntry).all())
    assert amounts == [Decimal("-4.00"), Decimal("36.00"), Decimal("36.00")]


def test_approved_sale_credits_earning_minus_advance(db, three_sales):
    """PDF Case 1: earning 30, advance 3, approved -> 27. Here 40 - 4 = 36."""
    run_advance_payout_job(db)

    reconcile_sale(db, "sale_0", SaleStatus.APPROVED)

    entry = db.query(LedgerEntry).filter_by(sale_id="sale_0").one()
    assert entry.entry_type == LedgerEntryType.FINAL_CREDIT
    assert entry.amount == Decimal("36.00")
    assert entry.idempotency_key == "final:sale_0"


def test_rejected_sale_creates_negative_adjustment(db, three_sales):
    """PDF Case 2: earning 50, advance 5, rejected -> adjustment of -5.

    The user received money they were not entitled to, so it is clawed back
    against future earnings rather than forgiven.
    """
    run_advance_payout_job(db)

    reconcile_sale(db, "sale_0", SaleStatus.REJECTED)

    entry = db.query(LedgerEntry).filter_by(sale_id="sale_0").one()
    assert entry.entry_type == LedgerEntryType.REJECTION_ADJUSTMENT
    assert entry.amount == Decimal("-4.00")
    assert entry.idempotency_key == "reject:sale_0"


def test_approved_sale_without_advance_credits_full_earning(db, three_sales):
    """Reconciled before the advance job ever ran: nothing to net off."""
    reconcile_sale(db, "sale_0", SaleStatus.APPROVED)

    assert get_withdrawable_balance(db, "john_doe") == Decimal("40.00")


def test_rejected_sale_without_advance_writes_no_ledger_row(db, three_sales):
    """Nothing was paid out, so nothing is owed back. A zero-value ledger row
    would be noise in the audit trail."""
    reconcile_sale(db, "sale_0", SaleStatus.REJECTED)

    assert db.query(LedgerEntry).count() == 0
    assert get_withdrawable_balance(db, "john_doe") == Decimal("0.00")
    assert db.get(Sale, "sale_0").status == SaleStatus.REJECTED


def test_sale_reconciles_exactly_once(db, three_sales):
    """Re-reconciling would double-count the adjustment, so it is a 409."""
    run_advance_payout_job(db)
    reconcile_sale(db, "sale_0", SaleStatus.APPROVED)

    with pytest.raises(InvalidTransition, match="already"):
        reconcile_sale(db, "sale_0", SaleStatus.APPROVED)

    with pytest.raises(InvalidTransition, match="already"):
        reconcile_sale(db, "sale_0", SaleStatus.REJECTED)

    assert db.query(LedgerEntry).count() == 1
    assert get_withdrawable_balance(db, "john_doe") == Decimal("36.00")


def test_cannot_reconcile_back_to_pending(db, three_sales):
    with pytest.raises(InvalidTransition, match="approved or rejected"):
        reconcile_sale(db, "sale_0", SaleStatus.PENDING)


def test_reconciling_unknown_sale_is_not_found(db, john):
    with pytest.raises(NotFound):
        reconcile_sale(db, "does_not_exist", SaleStatus.APPROVED)


def test_reconciled_sale_is_not_eligible_for_advance(db, three_sales):
    """A sale reconciled before the job runs must not receive an advance --
    the money is already settled."""
    reconcile_sale(db, "sale_0", SaleStatus.APPROVED)

    result = run_advance_payout_job(db)

    assert result["advances_paid"] == 2  # only the two still pending
    assert get_withdrawable_balance(db, "john_doe") == Decimal("40.00")


def test_all_rejected_leaves_negative_balance(db, three_sales):
    """EDGE CASE: every sale rejected after advances were paid.

    The user is genuinely in debt -- they hold Rs 12 they were not entitled
    to. The balance goes negative and stays there, netting against future
    earnings. Withdrawals are blocked while it is <= 0 (Stage 4).
    """
    run_advance_payout_job(db)
    for i in range(3):
        reconcile_sale(db, f"sale_{i}", SaleStatus.REJECTED)

    assert get_withdrawable_balance(db, "john_doe") == Decimal("-12.00")


def test_debt_nets_against_later_earnings(db, three_sales):
    """The negative balance is not forgiven; a later approved sale absorbs it."""
    run_advance_payout_job(db)
    for i in range(3):
        reconcile_sale(db, f"sale_{i}", SaleStatus.REJECTED)
    assert get_withdrawable_balance(db, "john_doe") == Decimal("-12.00")

    db.add(Sale(id="later", user_id="john_doe", brand="brand_2", earning=Decimal("100.00")))
    db.commit()
    run_advance_payout_job(db)          # advances 10.00
    reconcile_sale(db, "later", SaleStatus.APPROVED)  # credits 90.00

    # -12 + 90 = 78
    assert get_withdrawable_balance(db, "john_doe") == Decimal("78.00")


def test_balance_excludes_advances(db, three_sales):
    """An advance is money that already left the system. It must never appear
    in the withdrawable balance -- only reconciliation puts it there."""
    run_advance_payout_job(db)

    assert get_withdrawable_balance(db, "john_doe") == Decimal("0.00")
    assert db.query(LedgerEntry).count() == 0
