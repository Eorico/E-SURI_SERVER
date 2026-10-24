"""SQLAlchemy engine, session factory, declarative Base, and the get_db dependency.

Every query in the app goes through the ORM or a bound `text()` parameter, so
user input is never concatenated into SQL.
"""

from __future__ import annotations

from collections.abc import Generator

from sqlalchemy import MetaData, create_engine, event
from sqlalchemy.engine import Engine
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from api.core.config import settings

#: Predictable constraint names, so migrations can reference them by name.
NAMING_CONVENTION = {
    "ix": "ix_%(column_0_label)s",
    "uq": "uq_%(table_name)s_%(column_0_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}

_is_sqlite = settings.DATABASE_URL.startswith("sqlite")

engine: Engine = create_engine(
    settings.DATABASE_URL,
    pool_pre_ping=True,
    future=True,
    echo=False,
    **(
        {}
        if _is_sqlite
        else {
            "pool_size": settings.DB_POOL_SIZE,
            "max_overflow": settings.DB_MAX_OVERFLOW,
            # A runaway query is a denial-of-service risk; cap it at the server.
            "connect_args": {
                "options": f"-c statement_timeout={settings.DB_STATEMENT_TIMEOUT_MS}",
                "application_name": "imecar-api",
            },
        }
    ),
)

if _is_sqlite:

    @event.listens_for(engine, "connect")
    def _sqlite_pragmas(dbapi_connection, _record) -> None:
        """Foreign keys are off by default in SQLite; the tests rely on them."""
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()


SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False, future=True)


class Base(DeclarativeBase):
    """Declarative base all ORM models inherit from."""

    metadata = MetaData(naming_convention=NAMING_CONVENTION)


def get_db() -> Generator[Session, None, None]:
    """Yield a session and guarantee it is rolled back and closed.

    Controllers never commit; the repository that owns the write does, so a
    failure part-way through a request cannot leave a half-written row.
    """
    db = SessionLocal()
    try:
        yield db
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()
