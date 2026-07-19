"""Stage 1: the schema-level guarantees everything else is built on.

These tests assert that the database itself enforces the rules, so that no
amount of buggy application code above it can violate them.
"""

from decimal import Decimal

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from app.models import AdvancePayout, LedgerEntry, Sale
from app.enums import LedgerEntryType


def test_foreign_keys_are_enforced(db, john):
    """SQLite ignores FKs unless PRAGMA foreign_keys=ON is set per connection.

    Without this the FK declarations in models.py would be decorative and
    orphaned rows could accumulate silently.
    """
    assert db.execute(text("PRAGMA foreign_keys")).scalar() == 1

    db.add(Sale(id="orphan", user_id="ghost_user", brand="brand_1", earning=Decimal("10")))
    with pytest.raises(IntegrityError):
        db.flush()
    db.rollback()


def test_money_round_trips_as_exact_decimal(db, john):
    """Money must never become a float. 10% of 40.00 has to be exactly 4.00."""
    db.add(Sale(id="s1", user_id="john_doe", brand="brand_1", earning=Decimal("40.00")))
    db.commit()
    db.expire_all()

    earning = db.get(Sale, "s1").earning
    assert isinstance(earning, Decimal), "Money type leaked a float"
    assert earning == Decimal("40.00")
    assert (earning * Decimal("0.10")).quantize(Decimal("0.01")) == Decimal("4.00")


def test_money_quantizes_to_two_places(db, john):
    """Sub-paisa precision is rounded on write, so stored values are canonical."""
    db.add(Sale(id="s2", user_id="john_doe", brand="brand_1", earning=Decimal("33.333")))
    db.commit()
    db.expire_all()
    assert db.get(Sale, "s2").earning == Decimal("33.33")


def test_advance_never_paid_twice_for_same_sale(db, john):
    """BUSINESS RULE #1, enforced by the database.

    "Once an advance payout has been successfully transferred, the same sale
    must never receive another advance payout, even if the job runs multiple
    times." This holds no matter how many job instances race, because it is a
    UNIQUE constraint rather than an application-level check.
    """
    db.add(Sale(id="s1", user_id="john_doe", brand="brand_1", earning=Decimal("40.00")))
    db.commit()

    db.add(AdvancePayout(id="a1", sale_id="s1", user_id="john_doe", amount=Decimal("4.00")))
    db.commit()

    db.add(AdvancePayout(id="a2", sale_id="s1", user_id="john_doe", amount=Decimal("4.00")))
    with pytest.raises(IntegrityError):
        db.commit()
    db.rollback()

    assert db.query(AdvancePayout).filter_by(sale_id="s1").count() == 1


def test_ledger_idempotency_key_is_unique(db, john):
    """The key that makes duplicate webhooks and re-run jobs safe.

    Two entries claiming the same logical event collide here, so the retry
    becomes a no-op instead of double-crediting the user.
    """
    for entry_id in ("l1", "l2"):
        db.add(
            LedgerEntry(
                id=entry_id,
                user_id="john_doe",
                entry_type=LedgerEntryType.FINAL_CREDIT,
                amount=Decimal("36.00"),
                idempotency_key="final:sale_1",
            )
        )
        if entry_id == "l1":
            db.commit()

    with pytest.raises(IntegrityError):
        db.commit()
    db.rollback()

    assert db.query(LedgerEntry).count() == 1
