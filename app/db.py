"""Engine, session factory, and the SQLite pragmas that make the concurrency
guarantees in this system real rather than aspirational.

Two things SQLite does not do by default, both of which we depend on:

1. Foreign keys are NOT enforced unless `PRAGMA foreign_keys=ON` is issued on
   every connection. Without it the FK declarations in models.py are decorative.

2. pysqlite opens transactions lazily and in DEFERRED mode, so two writers can
   both read a balance before either writes -- exactly the double-spend the
   withdrawal path must prevent. `BEGIN IMMEDIATE` takes the write lock up
   front, serializing writers. It cannot be issued with `session.execute()`
   because SQLAlchemy has already opened a transaction by then; it has to be
   emitted here, on the engine's `begin` event.

On Postgres neither listener applies: FKs are always enforced, and the
withdrawal path uses `SELECT ... FOR UPDATE` on the user row instead.
"""

import os

from sqlalchemy import create_engine, event
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

DATABASE_URL = os.getenv("DATABASE_URL", "sqlite:///./payouts.db")
IS_SQLITE = DATABASE_URL.startswith("sqlite")


class Base(DeclarativeBase):
    pass


def _make_engine(url: str = DATABASE_URL):
    kwargs: dict = {"echo": False}
    if url.startswith("sqlite"):
        # check_same_thread=False so the threaded concurrency tests can share
        # one engine across threads; the BEGIN IMMEDIATE lock is what actually
        # keeps them correct.
        kwargs["connect_args"] = {"check_same_thread": False}
    return create_engine(url, **kwargs)


engine = _make_engine()


def register_sqlite_listeners(target_engine) -> None:
    """Attach the two SQLite-only listeners described in the module docstring."""

    @event.listens_for(target_engine, "connect")
    def _on_connect(dbapi_conn, _record):
        # Disable pysqlite's implicit BEGIN so we can issue our own.
        dbapi_conn.isolation_level = None
        cursor = dbapi_conn.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()

    @event.listens_for(target_engine, "begin")
    def _on_begin(conn):
        conn.exec_driver_sql("BEGIN IMMEDIATE")


if IS_SQLITE:
    register_sqlite_listeners(engine)


SessionLocal = sessionmaker(bind=engine, expire_on_commit=False, class_=Session)


def init_db(target_engine=None) -> None:
    """Create all tables. Real deployments would use Alembic migrations."""
    from app import models  # noqa: F401  -- registers the mappers

    Base.metadata.create_all(target_engine or engine)


def get_db():
    """FastAPI dependency: one session per request, always closed."""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
