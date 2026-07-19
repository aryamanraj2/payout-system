"""Engine and session setup.

SQLite needs two things configured per connection or the guarantees in this
app don't hold: foreign keys (off by default), and BEGIN IMMEDIATE so writers
are serialised (otherwise two withdrawals can both read the same balance).
See docs/DESIGN.md.
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
        # allow the engine to be shared across threads (tests race threads);
        # BEGIN IMMEDIATE is what keeps that safe
        kwargs["connect_args"] = {"check_same_thread": False}
    return create_engine(url, **kwargs)


engine = _make_engine()


def register_sqlite_listeners(target_engine) -> None:
    @event.listens_for(target_engine, "connect")
    def _on_connect(dbapi_conn, _record):
        # disable pysqlite's implicit BEGIN so we can issue our own
        dbapi_conn.isolation_level = None
        cursor = dbapi_conn.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        # queued writers should wait for the lock, not fail instantly
        cursor.execute("PRAGMA busy_timeout=5000")
        cursor.close()

    @event.listens_for(target_engine, "begin")
    def _on_begin(conn):
        conn.exec_driver_sql("BEGIN IMMEDIATE")


if IS_SQLITE:
    register_sqlite_listeners(engine)


SessionLocal = sessionmaker(bind=engine, expire_on_commit=False, class_=Session)


def init_db(target_engine=None) -> None:
    from app import models  # noqa: F401  (registers the mappers)

    Base.metadata.create_all(target_engine or engine)


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
