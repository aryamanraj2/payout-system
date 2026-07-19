"""Stage 2: the advance payout job.

Tests are named after the business rules they prove.
"""

import threading
from decimal import Decimal

import pytest

from app import gateway
from app.enums import SaleStatus
from app.models import AdvancePayout, Sale
from app.services.advance_service import (
    advance_amount_for,
    run_advance_payout_job,
)


def test_advance_is_ten_percent_of_earnings(db, three_sales):
    result = run_advance_payout_job(db)

    assert result["advances_paid"] == 3
    advances = db.query(AdvancePayout).all()
    assert len(advances) == 3
    # The PDF: 3 sales of Rs 40, advance of Rs 4 each, Rs 12 total.
    assert all(a.amount == Decimal("4.00") for a in advances)
    assert sum(a.amount for a in advances) == Decimal("12.00")


def test_advance_never_paid_twice(db, three_sales):
    """BUSINESS RULE #1: running the job repeatedly must not re-pay a sale."""
    first = run_advance_payout_job(db)
    second = run_advance_payout_job(db)
    third = run_advance_payout_job(db)

    assert first == {"advances_paid": 3, "skipped": 0, "failed": 0}
    assert second == {"advances_paid": 0, "skipped": 0, "failed": 0}
    assert third == {"advances_paid": 0, "skipped": 0, "failed": 0}

    assert db.query(AdvancePayout).count() == 3


def test_only_pending_sales_are_eligible(db, john):
    """Approved and rejected sales have already been reconciled; an advance on
    them would be paying out against a settled sale."""
    db.add_all(
        [
            Sale(id="p", user_id="john_doe", brand="brand_1",
                 earning=Decimal("40.00"), status=SaleStatus.PENDING),
            Sale(id="a", user_id="john_doe", brand="brand_1",
                 earning=Decimal("40.00"), status=SaleStatus.APPROVED),
            Sale(id="r", user_id="john_doe", brand="brand_1",
                 earning=Decimal("40.00"), status=SaleStatus.REJECTED),
        ]
    )
    db.commit()

    result = run_advance_payout_job(db)

    assert result["advances_paid"] == 1
    assert {a.sale_id for a in db.query(AdvancePayout).all()} == {"p"}


def test_new_sale_added_later_gets_its_advance(db, three_sales):
    """The job is incremental, not one-shot."""
    run_advance_payout_job(db)

    db.add(Sale(id="late", user_id="john_doe", brand="brand_2", earning=Decimal("50.00")))
    db.commit()

    result = run_advance_payout_job(db)

    assert result["advances_paid"] == 1
    assert db.query(AdvancePayout).filter_by(sale_id="late").one().amount == Decimal("5.00")
    assert db.query(AdvancePayout).count() == 4


def test_failed_transfer_leaves_sale_eligible_for_retry(db, three_sales, monkeypatch):
    """If the gateway declines, the savepoint rolls back and no advance row is
    written -- so the sale is retried on the next run rather than being
    silently marked as paid."""
    def always_fails(user_id, amount, reference):
        raise gateway.TransferFailed("gateway down")

    monkeypatch.setattr("app.services.advance_service.transfer_funds_external", always_fails)

    result = run_advance_payout_job(db)
    assert result == {"advances_paid": 0, "skipped": 0, "failed": 3}
    assert db.query(AdvancePayout).count() == 0

    # Gateway recovers; the next run pays them.
    monkeypatch.undo()
    result = run_advance_payout_job(db)
    assert result["advances_paid"] == 3
    assert db.query(AdvancePayout).count() == 3


def test_partial_failure_does_not_poison_the_batch(db, john, monkeypatch):
    """One failing sale must not prevent the others from being paid."""
    db.add_all(
        [
            Sale(id=f"s{i}", user_id="john_doe", brand="brand_1", earning=Decimal("40.00"))
            for i in range(3)
        ]
    )
    db.commit()

    def fails_on_s1(user_id, amount, reference):
        if reference == "s1":
            raise gateway.TransferFailed("declined")
        return f"gw_{reference}"

    monkeypatch.setattr("app.services.advance_service.transfer_funds_external", fails_on_s1)

    result = run_advance_payout_job(db)

    assert result == {"advances_paid": 2, "skipped": 0, "failed": 1}
    assert {a.sale_id for a in db.query(AdvancePayout).all()} == {"s0", "s2"}


def test_rounding_is_half_up_to_paise():
    """Money never touches float. 10% of 33.33 is 3.33, not 3.3330000000000002."""
    assert advance_amount_for(Decimal("40.00")) == Decimal("4.00")
    assert advance_amount_for(Decimal("33.33")) == Decimal("3.33")
    assert advance_amount_for(Decimal("0.05")) == Decimal("0.01")  # 0.005 -> half up
    assert advance_amount_for(Decimal("0.00")) == Decimal("0.00")


def test_losing_racer_skips_instead_of_double_paying(db, three_sales, monkeypatch):
    """THE RACE, forced deterministically.

    Two job instances can both SELECT a sale as eligible before either INSERTs.
    Under SQLite's BEGIN IMMEDIATE the writers serialise so tightly that this
    window almost never opens naturally -- the threaded test below confirmed
    `skipped` stays 0 -- which would leave the IntegrityError handler untested.

    So we simulate the stale read directly: advance the sales, then hand the
    job a SELECT result computed *before* that happened. This is exactly what a
    losing instance sees, and it must skip rather than pay a second time.
    """
    run_advance_payout_job(db)
    assert db.query(AdvancePayout).count() == 3

    stale_view = list(three_sales)  # what an instance that SELECTed earlier holds
    monkeypatch.setattr(
        "app.services.advance_service.find_eligible_sales",
        lambda _db: stale_view,
    )

    paid_calls: list[str] = []
    monkeypatch.setattr(
        "app.services.advance_service.transfer_funds_external",
        lambda user_id, amount, reference: paid_calls.append(reference),
    )

    result = run_advance_payout_job(db)

    assert result == {"advances_paid": 0, "skipped": 3, "failed": 0}
    assert paid_calls == [], "a losing racer moved money before hitting the constraint"
    assert db.query(AdvancePayout).count() == 3


def test_concurrent_job_runs_pay_each_sale_exactly_once(session_factory, engine):
    """Several job instances running at once, for real.

    This asserts the end-state invariant only: exactly one advance per sale.
    It does NOT reliably exercise the IntegrityError path -- write
    serialisation usually means one worker claims everything and the rest find
    nothing eligible. `test_losing_racer_skips_instead_of_double_paying` covers
    that branch deterministically.
    """
    from app.models import User

    setup = session_factory()
    setup.add(User(id="john_doe"))
    setup.add_all(
        [
            Sale(id=f"s{i}", user_id="john_doe", brand="brand_1", earning=Decimal("40.00"))
            for i in range(5)
        ]
    )
    setup.commit()
    setup.close()

    barrier = threading.Barrier(4)
    errors: list[Exception] = []

    def worker():
        session = session_factory()
        try:
            barrier.wait()  # maximise overlap
            run_advance_payout_job(session)
        except Exception as exc:  # noqa: BLE001 - surfaced via assertion below
            errors.append(exc)
        finally:
            session.close()

    threads = [threading.Thread(target=worker) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)

    assert not errors, f"workers raised: {errors}"

    verify = session_factory()
    advances = verify.query(AdvancePayout).all()
    verify.close()

    # The invariant that matters: exactly one advance per sale, no duplicates.
    assert len(advances) == 5
    assert len({a.sale_id for a in advances}) == 5
    assert sum(a.amount for a in advances) == Decimal("20.00")
