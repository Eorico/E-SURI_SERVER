"""Repository layer abstractions.

- `Repository[T]`: the narrow Protocol services depend on (DIP + ISP).
- `SqlRepository[T]`: the shared CRUD implementation, written once (DRY);
  concrete repositories subclass it and add only their own queries (OCP).

Every query is built with SQLAlchemy constructs, so values are always bound
parameters and never interpolated into SQL.
"""

from __future__ import annotations

from typing import Any, Generic, Protocol, Sequence, TypeVar, runtime_checkable

from sqlalchemy import Select, func, select
from sqlalchemy.orm import Session

T = TypeVar("T")

#: Hard ceiling on any list endpoint, so a crafted `limit` cannot pull a table.
MAX_PAGE_SIZE = 200


@runtime_checkable
class Repository(Protocol[T]):
    def get(self, id: Any) -> T | None: ...
    def list(self, **filters: Any) -> list[T]: ...
    def add(self, obj: T) -> T: ...
    def delete(self, obj: T) -> None: ...


class SqlRepository(Generic[T]):
    """Generic SQLAlchemy CRUD. Subclasses set `model`."""

    model: type[T]

    def __init__(self, db: Session):
        self.db = db

    # --- reads ------------------------------------------------------------
    def get(self, id: Any) -> T | None:
        return self.db.get(self.model, id)

    def list(self, **filters: Any) -> list[T]:
        return list(self.db.scalars(self._select(**filters)))

    def paginate(self, *, limit: int = 50, offset: int = 0, **filters: Any) -> list[T]:
        stmt = self._select(**filters).limit(min(limit, MAX_PAGE_SIZE)).offset(max(offset, 0))
        return list(self.db.scalars(stmt))

    def count(self, **filters: Any) -> int:
        stmt = select(func.count()).select_from(self.model).filter_by(**filters)
        return self.db.scalar(stmt) or 0

    def first(self, **filters: Any) -> T | None:
        return self.db.scalars(self._select(**filters).limit(1)).first()

    def exists(self, **filters: Any) -> bool:
        return self.first(**filters) is not None

    def _select(self, **filters: Any) -> Select:
        return select(self.model).filter_by(**filters)

    # --- writes -----------------------------------------------------------
    def add(self, obj: T) -> T:
        self.db.add(obj)
        self.db.commit()
        self.db.refresh(obj)
        return obj

    def add_all(self, objs: Sequence[T]) -> list[T]:
        """Insert many rows in one transaction — used for keys and responses."""
        self.db.add_all(list(objs))
        self.db.commit()
        return list(objs)

    def update(self, obj: T, **changes: Any) -> T:
        for field, value in changes.items():
            setattr(obj, field, value)
        self.db.commit()
        self.db.refresh(obj)
        return obj

    def delete(self, obj: T) -> None:
        self.db.delete(obj)
        self.db.commit()

    def delete_where(self, **filters: Any) -> int:
        rows = self.list(**filters)
        for row in rows:
            self.db.delete(row)
        self.db.commit()
        return len(rows)
