"""Repository layer abstractions.

- `Repository[T]`: the narrow Protocol services depend on (DIP + ISP).
- `MongoRepository[T]`: the shared CRUD implementation, written once (DRY);
  concrete repositories subclass it and add only their own queries (OCP).

The surface is deliberately small and ordinary - `get`, `list`, `first`,
`paginate`, `count`, `exists`, `add`, `add_all`, `update`, `delete`,
`delete_where` - so a service reads as what it is doing rather than as how this
database does it.

Two things are worth knowing when writing a subclass.

**Nothing is a query language.** Filters are plain field equality, assembled
into a BSON document, so a value is always data and never fragments of a
command - the same property parameter binding gave the SQL version.

**Order is never implicit.** A collection has none, so a query without a sort
may return documents in any order, and that order can change as documents are
updated. Every repository therefore declares `default_sort`, and `list()`
applies it. A report whose rows reshuffle between two runs of the same export
is the bug this prevents.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any, Generic, Iterable, Protocol, Sequence, TypeVar, runtime_checkable

from pymongo import ASCENDING
from pymongo.collection import Collection
from pymongo.database import Database

from api.core.cache import invalidate
from api.models.documents import COLLECTIONS, Doc

T = TypeVar("T", bound=Doc)

#: Hard ceiling on any list endpoint, so a crafted `limit` cannot pull a whole
#: collection.
MAX_PAGE_SIZE = 200


@runtime_checkable
class Repository(Protocol[T]):
    def get(self, id: Any) -> T | None: ...
    def list(self, **filters: Any) -> list[T]: ...
    def add(self, obj: T) -> T: ...
    def delete(self, obj: T) -> None: ...


def _query(filters: dict[str, Any]) -> dict[str, Any]:
    """Turn keyword filters into a BSON query.

    Two translations, and only these two:

    * `id` becomes `_id`. Callers say `first(id=...)` as they always have,
      while the document stores its identifier where MongoDB expects it.
    * A `uuid.UUID` becomes its string form, matching how it was stored - see
      `_bson_safe` in `api.models.documents`. Recursively, so a `uuid` nested
      inside `{"$in": [...]}` is converted too; missing that is the bug this
      function exists to make impossible, because the query would simply match
      nothing and the screen would come back empty with no error anywhere.
    """
    query: dict[str, Any] = {}
    for key, value in filters.items():
        query["_id" if key == "id" else key] = _query_value(value)
    return query


def as_uuid(value: Any) -> Any:
    """A raw document id, back as a `uuid.UUID`.

    The counterpart to `_query_value`. Ids are stored as strings, so anything
    read out of a raw document - `document["_id"]`, `document["teacher_id"]` -
    is a `str`. Keying a lookup table on one and then probing it with the
    `uuid.UUID` a model carries silently misses every time, which is how the
    hydration slots all came back empty.

    Prefer validating the document into its model and keying on `model.id`.
    This is for the cases that read an id without the document around it.
    """
    if isinstance(value, str):
        try:
            return uuid.UUID(value)
        except ValueError:
            return value
    return value


def _query_value(value: Any) -> Any:
    if isinstance(value, uuid.UUID):
        return str(value)
    if isinstance(value, dict):
        return {key: _query_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_query_value(item) for item in value]
    return value


class Coercing:
    """A `Collection` that stringifies every `uuid.UUID` passed through it.

    Ids are stored as strings (see `_bson_safe` in `api.models.documents`), so
    a query holding a `uuid.UUID` matches nothing. That failure is silent - no
    error, no warning, just an empty screen - which makes it exactly the kind
    of bug that survives review.

    Fixing each query would have left the next repository free to reintroduce
    it. This wraps the collection instead, so every call is coerced whatever it
    is and whoever writes it. Coercion is recursive and idempotent, so it is
    applied to all arguments without needing to know which one is a filter,
    which is an update, and which is a pipeline.
    """

    __slots__ = ("_collection",)

    def __init__(self, collection: Collection):
        self._collection = collection

    @property
    def raw(self) -> Collection:
        """The unwrapped collection, for the rare call that must not be touched."""
        return self._collection

    def __getattr__(self, name: str) -> Any:
        attribute = getattr(self._collection, name)
        if not callable(attribute):
            return attribute

        def coerced(*args: Any, **kwargs: Any) -> Any:
            return attribute(
                *(_query_value(arg) for arg in args),
                **{key: _query_value(value) for key, value in kwargs.items()},
            )

        return coerced

    def __getitem__(self, name: str) -> Any:
        return Coercing(self._collection[name])


class MongoRepository(Generic[T]):
    """Generic MongoDB CRUD. Subclasses set `model`."""

    model: type[T]
    #: Applied by `list` and `paginate` so results are reproducible. Overridden
    #: by any repository with a more meaningful order.
    default_sort: Sequence[tuple[str, int]] = (("created_at", ASCENDING),)
    #: Other cache namespaces a write here must also clear, because their
    #: cached reads embed data from this collection.
    depends_on: tuple[str, ...] = ()

    def __init__(self, db: Database):
        self.db = db

    @property
    def collection_name(self) -> str:
        """Also the cache namespace, which is what ties a write to the entries
        it has to clear."""
        return COLLECTIONS[self.model]

    @property
    def collection(self) -> Coercing:
        """This repository's own collection, id-coercing."""
        return Coercing(self.db[self.collection_name])

    def other(self, name: str) -> Coercing:
        """Another collection, id-coercing.

        The two-step that replaced a join reads from a second collection -
        narrow there on an indexed field, then fetch by primary key here. Going
        through this rather than `self.db[name]` is what keeps that read
        coerced.
        """
        return Coercing(self.db[name])

    # --- reads ------------------------------------------------------------
    def get(self, id: Any) -> T | None:
        return self._one(self.collection.find_one({"_id": id}))

    def list(self, **filters: Any) -> list[T]:
        cursor = self.collection.find(_query(filters)).sort(list(self.default_sort))
        return self._many(cursor)

    def paginate(self, *, limit: int = 50, offset: int = 0, **filters: Any) -> list[T]:
        cursor = (
            self.collection.find(_query(filters))
            .sort(list(self.default_sort))
            .skip(max(offset, 0))
            .limit(min(limit, MAX_PAGE_SIZE))
        )
        return self._many(cursor)

    def count(self, **filters: Any) -> int:
        return self.collection.count_documents(_query(filters))

    def first(self, **filters: Any) -> T | None:
        return self._one(self.collection.find_one(_query(filters), sort=list(self.default_sort)))

    def exists(self, **filters: Any) -> bool:
        """Cheaper than `first`: the server stops at the first match and sends
        back only the id, not the document."""
        found = self.collection.find_one(_query(filters), projection={"_id": 1})
        return found is not None

    # --- writes -----------------------------------------------------------
    #
    # Every one of these invalidates the collection's cache namespace. It is
    # done here rather than at the call sites so that caching is opt-in while
    # invalidation is automatic - the safe way round. A repository author who
    # adds a `@cached` read gets correct invalidation for free; one who forgets
    # to invalidate cannot, because there is nowhere to forget it.
    def add(self, obj: T) -> T:
        self.collection.insert_one(obj.to_bson())
        self._invalidate()
        return obj

    def add_all(self, objs: Sequence[T]) -> list[T]:
        """Insert many documents in one round trip.

        One command, so it is one network exchange rather than N - which is
        what the SQL version's single transaction bought. It is not atomic
        across documents: `ordered=False` lets the rest land even if one fails,
        which is right for a batch of independent records.
        """
        items = list(objs)
        if items:
            self.collection.insert_many([obj.to_bson() for obj in items], ordered=False)
            self._invalidate()
        return items

    def find(self, query: dict[str, Any], *, sort=None, limit: int = 0) -> list[T]:
        """A hand-written query - an `$in`, a range - in this collection."""
        cursor = self.collection.find(query).sort(list(sort or self.default_sort))
        return self._many(cursor.limit(limit) if limit else cursor)

    def ids_matching(self, collection: str, query: dict[str, Any]) -> list[Any]:
        """The `_id`s of another collection's matching documents.

        The two-step that replaced a join: narrow on an indexed field there,
        then fetch by primary key here.
        """
        found = self.other(collection).find(query, projection={"_id": 1})
        return [as_uuid(document["_id"]) for document in found]

    def update(self, obj: T, **changes: Any) -> T:
        """Apply changes to the document in memory and in the database.

        `updated_at` is maintained here because the SQL column did it with
        `onupdate=func.now()`, and losing it would quietly break every "last
        changed" column in the UI.
        """
        for field, value in changes.items():
            setattr(obj, field, value)

        payload = {field: _bson(value) for field, value in changes.items()}
        if hasattr(obj, "updated_at"):
            stamped = datetime.now(timezone.utc)
            obj.updated_at = stamped
            payload["updated_at"] = stamped

        if payload:
            self.collection.update_one({"_id": obj.id}, {"$set": payload})
            self._invalidate()
        return obj

    def replace(self, obj: T) -> T:
        """Write the whole document back.

        For a change that touches an embedded array - a submission's responses,
        an assessment's answer key - where naming each altered field would mean
        reimplementing the document's own shape at the call site.
        """
        self.collection.replace_one({"_id": obj.id}, obj.to_changes(), upsert=True)
        self._invalidate()
        return obj

    def delete(self, obj: T) -> None:
        self.collection.delete_one({"_id": obj.id})
        self._invalidate()

    def delete_where(self, **filters: Any) -> int:
        removed = self.collection.delete_many(_query(filters)).deleted_count
        if removed:
            self._invalidate()
        return removed

    def _invalidate(self) -> None:
        """Drop this collection's cached reads, plus anything that depends on it.

        `depends_on` is for a read that caches data from elsewhere - a
        hydrated class holds a copy of its section, so changing a section has
        to clear the classes too. Over-invalidating is the right failure: a
        needless miss costs one query, a missed invalidation serves a wrong
        answer until the TTL runs out.
        """
        invalidate(self.collection_name, *self.depends_on)

    # --- helpers for subclasses -------------------------------------------
    def _one(self, document: dict[str, Any] | None) -> T | None:
        return self.model.model_validate(document) if document else None

    def _many(self, documents: Iterable[dict[str, Any]]) -> list[T]:
        return [self.model.model_validate(document) for document in documents]

    def _aggregate(self, pipeline: list[dict[str, Any]]) -> list[T]:
        return self._many(self.collection.aggregate(pipeline))

    @staticmethod
    def _lookup(
        *, collection: str, local: str, as_field: str, unwind: bool = True
    ) -> list[dict[str, Any]]:
        """A server-side join that fills one hydration slot.

        `preserveNullAndEmptyArrays` keeps the parent when there is nothing to
        join. That is not a detail: there are no foreign keys, so an id can
        point at a document that has been deleted, and such a record must still
        be listable with the slot empty rather than vanish from the result.
        """
        stages: list[dict[str, Any]] = [
            {
                "$lookup": {
                    "from": collection,
                    "localField": local,
                    "foreignField": "_id",
                    "as": as_field,
                }
            }
        ]
        if unwind:
            stages.append(
                {"$unwind": {"path": f"${as_field}", "preserveNullAndEmptyArrays": True}}
            )
        return stages


def _bson(value: Any) -> Any:
    """Make one value storable. See `_bson_safe` in `api.models.documents`."""
    from api.models.documents import _bson_safe

    if isinstance(value, Doc):
        return value.to_bson()
    if hasattr(value, "model_dump"):
        return _bson_safe(value.model_dump())
    if isinstance(value, (list, tuple)):
        return [_bson(item) for item in value]
    return _bson_safe(value)


def new_id() -> uuid.UUID:
    """A fresh document id, for the few callers that need one before inserting."""
    return uuid.uuid4()


# --------------------------------------------------------------------------- #
# Embedded arrays                                                              #
# --------------------------------------------------------------------------- #
E = TypeVar("E")


class EmbeddedRepository(Generic[E]):
    """CRUD over an array inside another document.

    Three tables became embedded arrays - an assessment's answer key, a
    submission's responses, a report's five least-mastered competencies - and
    each had a repository the services call. Those repositories still exist and
    still answer the same methods; only where the data lives has changed.

    The write path is read, modify in Python, write the array back. That is two
    round trips rather than one, and it is chosen over `$push` and the
    positional operators for two reasons: the document's own validators run, so
    the uniqueness that used to be a UNIQUE constraint is still enforced, and
    the arrays are bounded at sixty, so there is nothing to stream.

    Subclasses declare where the array lives:

        parent_model = Assessment      # the document that holds it
        array_field = "answer_keys"    # the field inside it
        parent_key = "assessment_id"   # the routing attribute on each item
        order_by = "item_no"           # stable order, as the old ORDER BY gave
    """

    model: type[E]
    parent_model: type[Doc]
    array_field: str
    parent_key: str
    order_by: str

    def __init__(self, db: Database):
        self.db = db

    @property
    def parents(self) -> Coercing:
        return Coercing(self.db[COLLECTIONS[self.parent_model]])

    # --- reads ------------------------------------------------------------
    def for_parent(self, parent_id: Any) -> list[E]:
        """Every item in one parent, in `order_by` order."""
        document = self.parents.find_one(
            {"_id": parent_id}, projection={self.array_field: 1}
        )
        if not document:
            return []
        items = [self.model.model_validate(raw) for raw in document.get(self.array_field) or []]
        for item in items:
            setattr(item, self.parent_key, parent_id)
        return sorted(items, key=lambda item: getattr(item, self.order_by))

    def first(self, **filters: Any) -> E | None:
        """One item, located by its parent plus any field on the item itself."""
        parent_id = filters.pop(self.parent_key, None)
        if parent_id is None:
            raise ValueError(f"{type(self).__name__}.first needs {self.parent_key}")
        for item in self.for_parent(parent_id):
            if all(getattr(item, key, None) == value for key, value in filters.items()):
                return item
        return None

    def list(self, **filters: Any) -> list[E]:
        parent_id = filters.pop(self.parent_key, None)
        if parent_id is None:
            raise ValueError(f"{type(self).__name__}.list needs {self.parent_key}")
        items = self.for_parent(parent_id)
        if not filters:
            return items
        return [
            item
            for item in items
            if all(getattr(item, key, None) == value for key, value in filters.items())
        ]

    def count(self, **filters: Any) -> int:
        return len(self.list(**filters))

    def exists(self, **filters: Any) -> bool:
        return self.first(**filters) is not None

    # --- writes -----------------------------------------------------------
    def add(self, item: E) -> E:
        return self.add_all([item])[0]

    def add_all(self, items: Sequence[E]) -> list[E]:
        """Append items, grouped so each parent is written once."""
        collected = list(items)
        for parent_id, group in _group_by(collected, self.parent_key).items():
            existing = self.for_parent(parent_id)
            self._write(parent_id, existing + list(group))
        return collected

    def update(self, item: E, **changes: Any) -> E:
        for field, value in changes.items():
            setattr(item, field, value)

        parent_id = getattr(item, self.parent_key)
        key = getattr(item, self.order_by)
        kept = [
            other for other in self.for_parent(parent_id)
            if getattr(other, self.order_by) != key
        ]
        self._write(parent_id, kept + [item])
        return item

    def delete(self, item: E) -> None:
        parent_id = getattr(item, self.parent_key)
        key = getattr(item, self.order_by)
        self._write(
            parent_id,
            [
                other for other in self.for_parent(parent_id)
                if getattr(other, self.order_by) != key
            ],
        )

    def delete_where(self, **filters: Any) -> int:
        """Clear the array, or the part of it that matches."""
        parent_id = filters.pop(self.parent_key, None)
        if parent_id is None:
            raise ValueError(f"{type(self).__name__}.delete_where needs {self.parent_key}")

        before = self.for_parent(parent_id)
        kept = [
            item
            for item in before
            if filters
            and not all(getattr(item, key, None) == value for key, value in filters.items())
        ]
        self._write(parent_id, kept)
        return len(before) - len(kept)

    def replace_for_parent(self, parent_id: Any, items: Sequence[E]) -> list[E]:
        """Supersede the array entirely - a rescan, or a recomputed key."""
        stored = list(items)
        self._write(parent_id, stored)
        return stored

    def _write(self, parent_id: Any, items: Sequence[E]) -> None:
        """Validate the whole array through the parent, then store it.

        Routing it through the parent model is what keeps the uniqueness rule:
        `Assessment.answer_keys` and `Submission.responses` both refuse a
        duplicate item number, and that check was a UNIQUE constraint before
        these became arrays.
        """
        ordered = sorted(items, key=lambda item: getattr(item, self.order_by))
        self.parent_model.__pydantic_validator__.validate_assignment(
            self.parent_model.model_construct(), self.array_field, ordered
        )
        payload = [_bson(item) for item in ordered]
        self.parents.update_one(
            {"_id": parent_id},
            {"$set": {self.array_field: payload, "updated_at": datetime.now(timezone.utc)}},
        )
        # The array lives inside the parent document, so a cached read of that
        # parent now holds the old array.
        invalidate(COLLECTIONS[self.parent_model])


def _group_by(items: Sequence[Any], attribute: str) -> dict[Any, list[Any]]:
    grouped: dict[Any, list[Any]] = {}
    for item in items:
        key = getattr(item, attribute)
        if key is None:
            raise ValueError(f"Cannot store an embedded item with no {attribute}.")
        grouped.setdefault(key, []).append(item)
    return grouped
