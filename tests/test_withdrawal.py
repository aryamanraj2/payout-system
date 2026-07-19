"""Withdrawals: the 24h rule and double-spend prevention."""

import threading
from datetime import timedelta
from decimal import Decimal

import pytest

from app.enums import LedgerEntryType, PayoutStatus, SaleStatus
from app.errors import DomainError, InsufficientBalance, NotFound, WithdrawalRateLimited
from app.models import LedgerEntry, Payout, Sale, User, utcnow
from app.services import withdrawal_service
from app.services.advance_service import run_advance_payout_job
from app.services.balance_service import get_withdrawable_balance
from app.services.reconciliation_service import reconcile_sale
from app.services.withdrawal_service import request_withdrawal


@pytest.fixture
def funded(db, three_sales):
    """john_doe with the Rs 68 balance from the assignment example."""
    run_advance_payout_job(db)
    reconcile_sale(db, "sale_0", SaleStatus.REJECTED)
    reconcile_sale(db, "sale_1", SaleStatus.APPROVED)
    reconcile_sale(db, "sale_2", SaleStatus.APPROVED)
    assert get_withdrawable_balance(db, "john_doe") == Decimal("68.00")
    return db


def test_withdrawal_debits_balance_immediately(funded):
    payout = request_withdrawal(funded, "john_doe", Decimal("68.00"))

    assert payout.status == PayoutStatus.INITIATED
    assert payout.amount == Decimal("68.00")
    assert get_withdrawable_balance(funded, "john_doe") == Decimal("0.00")

    debit = funded.query(LedgerEntry).filter_by(payout_id=payout.id).one()
    assert debit.entry_type == LedgerEntryType.WITHDRAWAL_DEBIT
    assert debit.amount == Decimal("-68.00")
    assert debit.idempotency_key == f"withdraw:{payout.id}"


def test_partial_withdrawal_leaves_remainder(funded):
    request_withdrawal(funded, "john_doe", Decimal("20.00"))
    assert get_withdrawable_balance(funded, "john_doe") == Decimal("48.00")


def test_insufficient_balance_rejected(funded):
    with pytest.raises(InsufficientBalance) as exc:
        request_withdrawal(funded, "john_doe", Decimal("68.01"))

    assert exc.value.available == Decimal("68.00")
    assert exc.value.requested == Decimal("68.01")
    # nothing was written
    assert funded.query(Payout).count() == 0
    assert get_withdrawable_balance(funded, "john_doe") == Decimal("68.00")


def test_withdrawal_blocked_while_balance_is_negative(db, three_sales):
    run_advance_payout_job(db)
    for i in range(3):
        reconcile_sale(db, f"sale_{i}", SaleStatus.REJECTED)
    assert get_withdrawable_balance(db, "john_doe") == Decimal("-12.00")

    with pytest.raises(InsufficientBalance):
        request_withdrawal(db, "john_doe", Decimal("1.00"))


def test_non_positive_amounts_rejected(funded):
    for bad in (Decimal("0.00"), Decimal("-5.00")):
        with pytest.raises(DomainError, match="must be positive"):
            request_withdrawal(funded, "john_doe", bad)
    assert funded.query(Payout).count() == 0


def test_withdrawal_for_unknown_user_is_not_found(db, john):
    with pytest.raises(NotFound):
        request_withdrawal(db, "ghost", Decimal("10.00"))


def test_second_withdrawal_within_24h_blocked(funded):
    first = request_withdrawal(funded, "john_doe", Decimal("10.00"))

    with pytest.raises(WithdrawalRateLimited) as exc:
        request_withdrawal(funded, "john_doe", Decimal("10.00"))

    expected = first.created_at + withdrawal_service.WITHDRAWAL_COOLDOWN
    assert exc.value.retry_after == expected
    assert funded.query(Payout).count() == 1
    assert get_withdrawable_balance(funded, "john_doe") == Decimal("58.00")


def test_withdrawal_allowed_after_24h_elapses(funded):
    first = request_withdrawal(funded, "john_doe", Decimal("10.00"))

    # age the payout past the cooldown instead of sleeping
    first.created_at = first.created_at - timedelta(hours=25)
    funded.commit()

    second = request_withdrawal(funded, "john_doe", Decimal("10.00"))
    assert second.id != first.id
    assert funded.query(Payout).count() == 2


def test_retry_after_uses_the_most_recent_blocking_payout(funded):
    older = request_withdrawal(funded, "john_doe", Decimal("5.00"))
    older.created_at = older.created_at - timedelta(hours=20)  # still in window
    funded.commit()

    # inserted directly: going through request_withdrawal would be blocked by
    # the payout above, which is the rule under test
    newer = Payout(
        id="newer",
        user_id="john_doe",
        amount=Decimal("5.00"),
        status=PayoutStatus.INITIATED,
        created_at=utcnow() - timedelta(hours=2),
        updated_at=utcnow(),
    )
    funded.add(newer)
    funded.commit()

    with pytest.raises(WithdrawalRateLimited) as exc:
        request_withdrawal(funded, "john_doe", Decimal("5.00"))

    assert exc.value.retry_after == newer.created_at + withdrawal_service.WITHDRAWAL_COOLDOWN
    assert exc.value.retry_after != older.created_at + withdrawal_service.WITHDRAWAL_COOLDOWN


@pytest.mark.parametrize(
    "terminal_status",
    [PayoutStatus.FAILED, PayoutStatus.CANCELLED, PayoutStatus.REJECTED],
)
def test_failed_payouts_do_not_consume_the_24h_slot(funded, terminal_status):
    # Q2: a user whose payout failed must be able to withdraw again
    first = request_withdrawal(funded, "john_doe", Decimal("10.00"))
    first.status = terminal_status
    funded.commit()

    second = request_withdrawal(funded, "john_doe", Decimal("10.00"))
    assert second.id != first.id


def test_concurrent_withdrawals_one_wins(session_factory, monkeypatch):
    """Two threads withdraw the full balance at the same moment; exactly one
    may succeed. Cooldown disabled so the loser is rejected by the balance
    check, not masked by the rate limit."""
    monkeypatch.setattr(withdrawal_service, "WITHDRAWAL_COOLDOWN", timedelta(0))

    setup = session_factory()
    setup.add(User(id="john_doe"))
    setup.add(Sale(id="s", user_id="john_doe", brand="brand_1", earning=Decimal("68.00")))
    setup.commit()
    setup.add(
        LedgerEntry(
            id="seed",
            user_id="john_doe",
            entry_type=LedgerEntryType.FINAL_CREDIT,
            amount=Decimal("68.00"),
            sale_id="s",
            idempotency_key="final:s",
        )
    )
    setup.commit()
    setup.close()

    barrier = threading.Barrier(2)
    outcomes: list[str] = []
    lock = threading.Lock()

    def worker():
        session = session_factory()
        try:
            barrier.wait()
            request_withdrawal(session, "john_doe", Decimal("68.00"))
            with lock:
                outcomes.append("ok")
        except InsufficientBalance:
            with lock:
                outcomes.append("insufficient")
        except Exception as exc:
            with lock:
                outcomes.append(f"unexpected:{type(exc).__name__}:{exc}")
        finally:
            session.close()

    threads = [threading.Thread(target=worker) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)

    verify = session_factory()
    balance = get_withdrawable_balance(verify, "john_doe")
    payout_count = verify.query(Payout).count()
    verify.close()

    assert sorted(outcomes) == ["insufficient", "ok"], f"outcomes were {outcomes}"
    assert payout_count == 1
    assert balance == Decimal("0.00")
