"""Shared fixtures.

File-backed SQLite instead of :memory: — an in-memory db is private to one
connection, so the concurrency tests would be racing against nothing.
"""

import tempfile
from decimal import Decimal
from pathlib import Path

import pytest
from sqlalchemy.orm import sessionmaker

from app.db import Base, _make_engine, register_sqlite_listeners
from app.models import Sale, User


@pytest.fixture
def db_path():
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
def client(session_factory):
    """TestClient wired to the per-test db. Not used as a context manager on
    purpose — that would run the lifespan handler, whose init_db() targets
    the real engine and would create a stray payouts.db."""
    from fastapi.testclient import TestClient

    from app.db import get_db
    from app.main import app

    def override():
        s = session_factory()
        try:
            yield s
        finally:
            s.close()

    app.dependency_overrides[get_db] = override
    yield TestClient(app)
    app.dependency_overrides.clear()


@pytest.fixture
def john(db):
    user = User(id="john_doe")
    db.add(user)
    db.commit()
    return user


@pytest.fixture
def three_sales(db, john):
    """The assignment example: 3 pending sales of Rs 40."""
    sales = [
        Sale(id=f"sale_{i}", user_id="john_doe", brand="brand_1", earning=Decimal("40.00"))
        for i in range(3)
    ]
    db.add_all(sales)
    db.commit()
    return sales
