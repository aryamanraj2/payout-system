"""Shared test fixtures.

The database is file-backed, not `:memory:`, on purpose. In-memory SQLite is
private to a single connection, so the concurrency tests in later stages would
be testing nothing -- two "racing" threads would each get their own empty
database and both trivially succeed. A temp file gives them one real database
to contend over.
"""

import tempfile
from decimal import Decimal
from pathlib import Path

import pytest

from app.db import Base, _make_engine, register_sqlite_listeners
from app.models import Sale, User
from sqlalchemy.orm import sessionmaker


@pytest.fixture
def db_path():
    """A fresh SQLite file per test, deleted afterwards."""
    with tempfile.TemporaryDirectory() as tmp:
        yield Path(tmp) / "test.db"


@pytest.fixture
def engine(db_path):
    eng = _make_engine(f"sqlite:///{db_path}")
    register_sqlite_listeners(eng)
    Base.metadata.create_all(eng)
    yield eng
    eng.dispose()


@pytest.fixture
def session_factory(engine):
    return sessionmaker(bind=engine, expire_on_commit=False)


@pytest.fixture
def db(session_factory):
    s = session_factory()
    yield s
    s.close()


@pytest.fixture
def john(db):
    """The user from the assignment's worked example."""
    user = User(id="john_doe")
    db.add(user)
    db.commit()
    return user


@pytest.fixture
def three_sales(db, john):
    """The exact scenario from the PDF: 3 pending sales of Rs 40 each."""
    sales = [
        Sale(id=f"sale_{i}", user_id="john_doe", brand="brand_1", earning=Decimal("40.00"))
        for i in range(3)
    ]
    db.add_all(sales)
    db.commit()
    return sales
