"""MongoDB client, database handle, and the `get_db` dependency.

`get_db` is the single seam the whole data layer hangs off: every repository
is constructed as `SomeRepository(db)` and every provider in `api/deps.py`
reads `db = Depends(get_db)`, so this one function decides what all of them
talk to.

Two choices here are load-bearing:

* **Identifiers stay `uuid.UUID` in Python.** Switching to `ObjectId` would
  have changed the type of every id in every DTO and route signature; instead a
  document's `_id` is the same value the API has always exposed, written as its
  string form (see `_bson_safe` in `api.models.documents` for why a string
  rather than BSON binary), and `dto/` needed no edit.

* **One client for the process.** `MongoClient` is itself a connection pool and
  is thread-safe, so it is built once and shared. Building one per request would
  mean a new pool, and a new TLS handshake to Atlas, every time.
"""

from __future__ import annotations

from collections.abc import Generator
from functools import lru_cache

from pymongo import MongoClient
from pymongo.database import Database
from pymongo.errors import PyMongoError

from api.core.config import settings
from api.core.exceptions import UpstreamError
from api.core.logging import get_logger

logger = get_logger(__name__)


@lru_cache
def get_client() -> MongoClient:
    """The process-wide client.

    Nothing connects here. PyMongo dials lazily on first use and reconnects on
    its own, so a cluster that is briefly unreachable does not need a restart.
    """
    return MongoClient(
        settings.MONGODB_URI,
        # BSON records a datetime in UTC but stores no offset, so without this
        # the driver returns naive datetimes while everything in the
        # application builds aware ones - and the two cannot be compared.
        tz_aware=True,
        maxPoolSize=settings.MONGO_MAX_POOL_SIZE,
        minPoolSize=settings.MONGO_MIN_POOL_SIZE,
        serverSelectionTimeoutMS=settings.MONGO_SERVER_SELECTION_TIMEOUT_MS,
        connectTimeoutMS=settings.MONGO_CONNECT_TIMEOUT_MS,
        socketTimeoutMS=settings.MONGO_SOCKET_TIMEOUT_MS,
        maxIdleTimeMS=settings.MONGO_MAX_IDLE_TIME_MS,
        appname="imecar-api",
        #: Read and write against the primary, acknowledged by a majority of
        #: the replica set. A teacher who saves a score and immediately reloads
        #: must see it, which a secondary read cannot promise.
        w="majority",
        retryWrites=True,
    )


@lru_cache
def get_database() -> Database:
    return get_client()[settings.MONGODB_DB]


def get_db() -> Generator[Database, None, None]:
    """Yield the database for one request.

    There is no transaction and nothing to roll back by default: a single
    document update is atomic in MongoDB, which covers all but a handful of
    writes. The few that span collections open a session explicitly through
    `transaction()` rather than paying for one on every request.
    """
    yield get_database()


def transaction():
    """A client session with a transaction, for a write that spans collections.

    Atlas runs a replica set, so transactions are available. A standalone
    `mongod` is not a replica set and cannot start one - which is why this is
    reached for only where atomicity across collections is genuinely needed,
    and why most writes are shaped to need a single document instead.

    Use as a context manager:

        with transaction() as session:
            collection_a.insert_one(doc, session=session)
            collection_b.update_one(filt, change, session=session)
    """
    return get_client().start_session()


def ping() -> float:
    """Round-trip time to the cluster in milliseconds, for the health check."""
    import time

    started = time.perf_counter()
    try:
        get_client().admin.command("ping")
    except PyMongoError as exc:
        logger.warning("mongo_ping_failed", extra={"error": str(exc)})
        raise UpstreamError("The database is not reachable.") from exc
    return round((time.perf_counter() - started) * 1000, 1)


def reset_client() -> None:
    """Drop the cached client, so a test or a reload can rebuild it."""
    try:
        get_client().close()
    except Exception:  # noqa: BLE001 - closing a never-connected client is fine
        pass
    get_client.cache_clear()
    get_database.cache_clear()
