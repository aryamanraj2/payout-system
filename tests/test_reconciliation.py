"""Reconciliation and final payout calculation."""

from decimal import Decimal

import pytest

from app.enums import LedgerEntryType, SaleStatus
from app.errors import InvalidTransition, NotFound
from app.models import LedgerEntry, Sale
from app.services.advance_service import run_advance_payout_job
from app.services.balance_service import get_withdrawable_balance
from app.services.reconciliation_service import reconcile_sale


def test_example_from_assignment_totals_68(db, three_sales):
    """3 sales of Rs 40, Rs 4 advanced each; reject one, approve two:
    -4 + 36 + 36 = 68."""
    run_advance_payout_job(db)

    reconcile_sale(db, "sale_0", SaleStatus.REJECTED)
    reconcile_sale(db, "sale_1", SaleStatus.APPROVED)
    reconcile_sale(db, "sale_2", SaleStatus.APPROVED)

    assert get_withdrawable_balance(db, "john_doe") == Decimal("68.00")

    amounts = sorted(e.amount for e in db.query(LedgerEntry).all())
    assert amounts == [Decimal("-4.00"), Decimal("36.00"), Decimal("36.00")]


def test_approved_sale_credits_earning_minus_advance(db, three_sales):
    run_advance_payout_job(db)

    reconcile_sale(db, "sale_0", SaleStatus.APPROVED)

    entry = db.query(LedgerEntry).filter_by(sale_id="sale_0").one()
    assert entry.entry_type == LedgerEntryType.FINAL_CREDIT
    assert entry.amount == Decimal("36.00")
    assert entry.idempotency_key == "final:sale_0"


def test_rejected_sale_creates_negative_adjustment(db, three_sales):
    run_advance_payout_job(db)

    reconcile_sale(db, "sale_0", SaleStatus.REJECTED)

    entry = db.query(LedgerEntry).filter_by(sale_id="sale_0").one()
    assert entry.entry_type == LedgerEntryType.REJECTION_ADJUSTMENT
    assert entry.amount == Decimal("-4.00")
    assert entry.idempotency_key == "reject:sale_0"


def test_approved_sale_without_advance_credits_full_earning(db, three_sales):
    # reconciled before the advance job ever ran
    reconcile_sale(db, "sale_0", SaleStatus.APPROVED)

    assert get_withdrawable_balance(db, "john_doe") == Decimal("40.00")


def test_rejected_sale_without_advance_writes_no_ledger_row(db, three_sales):
    # nothing was paid out, so nothing is owed back
    reconcile_sale(db, "sale_0", SaleStatus.REJECTED)

    assert db.query(LedgerEntry).count() == 0
    assert get_withdrawable_balance(db, "john_doe") == Decimal("0.00")
    assert db.get(Sale, "sale_0").status == SaleStatus.REJECTED


def test_sale_reconciles_exactly_once(db, three_sales):
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
    reconcile_sale(db, "sale_0", SaleStatus.APPROVED)

    result = run_advance_payout_job(db)

    assert result["advances_paid"] == 2  # only the two still pending
    assert get_withdrawable_balance(db, "john_doe") == Decimal("40.00")


def test_all_rejected_leaves_negative_balance(db, three_sales):
    """Every sale rejected after advances were paid: the user holds Rs 12 they
    weren't entitled to, so the balance goes negative and stays there."""
    run_advance_payout_job(db)
    for i in range(3):
        reconcile_sale(db, f"sale_{i}", SaleStatus.REJECTED)

    assert get_withdrawable_balance(db, "john_doe") == Decimal("-12.00")


def test_debt_nets_against_later_earnings(db, three_sales):
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
    # advances are money that already left; only reconciliation moves the balance
    run_advance_payout_job(db)

    assert get_withdrawable_balance(db, "john_doe") == Decimal("0.00")
    assert db.query(LedgerEntry).count() == 0
