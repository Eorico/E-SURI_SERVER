"""Object storage for scans, sheets and certificates.

Files live in GridFS, inside the same MongoDB cluster as the records, behind
the same narrow interface as before - so no service knows where bytes go and a
test can substitute an in-memory double (DIP).

Two consequences of GridFS are worth stating plainly.

**Storage is shared with the data.** Every stored sheet competes with every
record for the cluster's quota, which a separately-billed bucket would not.
That is why `upload_scan` recompresses: a 3MB phone photo carries far more
detail than the OMR grid needs, and at 1600px in grayscale a sheet lands near
200KB. On a 512MB cluster that is the difference between a few hundred sheets
and a few thousand. Set `SHEET_RECOMPRESS=false` to keep originals byte for
byte.

**There are no signed URLs.** GridFS serves bytes over the database
connection, so the API signs a short-lived token and serves the file itself
from `GET /files/{token}` - see `api/routes/files.py`. The capability has to
sit in the URL because a browser fetching an `<img src>` cannot send an
Authorization header, which is the same reason a storage provider's signed
URLs worked the way they did.
"""

from __future__ import annotations

import re
import uuid
from typing import Protocol

from gridfs import GridFSBucket
from gridfs.errors import NoFile
from pymongo.errors import PyMongoError

from api.core.config import settings
from api.core.exceptions import NotFoundError, UpstreamError, ValidationError
from api.core.logging import get_logger
from api.core.tokens import issue_file_token

logger = get_logger(__name__)

#: Magic bytes for the formats we accept, so a renamed file is still rejected.
_SIGNATURES: tuple[tuple[bytes, str], ...] = (
    (b"\xff\xd8\xff", "image/jpeg"),
    (b"\x89PNG\r\n\x1a\n", "image/png"),
    (b"RIFF", "image/webp"),
)

_SAFE_SEGMENT = re.compile(r"[^A-Za-z0-9._-]")


def sniff_image_type(data: bytes) -> str | None:
    for signature, mime in _SIGNATURES:
        if data.startswith(signature):
            if mime == "image/webp" and data[8:12] != b"WEBP":
                continue
            return mime
    return None


def validate_upload(data: bytes, declared_type: str | None) -> str:
    """Check size and real format. Returns the detected content type."""
    if not data:
        raise ValidationError("The uploaded file is empty.")
    if len(data) > settings.MAX_UPLOAD_BYTES:
        raise ValidationError(
            f"That image is larger than {settings.MAX_UPLOAD_BYTES // (1024 * 1024)}MB."
        )

    detected = sniff_image_type(data)
    if detected is None or detected not in settings.ALLOWED_UPLOAD_TYPES:
        raise ValidationError("Upload a JPEG, PNG or WebP photo of the answer sheet.")
    if declared_type and declared_type.split(";")[0].strip() != detected:
        logger.warning("upload_type_mismatch", extra={"declared": declared_type, "detected": detected})
    return detected


def safe_segment(value: str) -> str:
    """Make a path segment that cannot escape its folder."""
    cleaned = _SAFE_SEGMENT.sub("-", value).strip("-.") or "item"
    return cleaned[:64]


class ObjectStore(Protocol):
    def upload(self, bucket: str, path: str, data: bytes, content_type: str) -> str: ...
    def signed_url(self, bucket: str, path: str, expires: int) -> str: ...
    #: Reading is part of the interface now. A storage provider served its own
    #: signed URLs; GridFS cannot, so whatever holds the bytes is also what
    #: serves them.
    def open(self, bucket: str, path: str) -> tuple[bytes, str]: ...


class GridFsObjectStore:
    """The real store: GridFS buckets in the application's own database."""

    def __init__(self, db_factory=None):
        self._db_factory = db_factory

    def _bucket(self, bucket: str) -> GridFSBucket:
        from api.core.mongo import get_database  # deferred: no connection at import

        database = self._db_factory() if self._db_factory is not None else get_database()
        return GridFSBucket(database, bucket_name=bucket)

    def upload(self, bucket: str, path: str, data: bytes, content_type: str) -> str:
        """Store the bytes under `path`, replacing anything already there.

        GridFS keeps each upload of a name as a new version rather than
        overwriting, so a rescanned sheet would quietly accumulate copies -
        every one of them charged against the cluster's storage. Deleting the
        old versions first is the `upsert: true` the bucket API used to give.
        """
        try:
            handle = self._bucket(bucket)
            self._remove_existing(handle, path)
            handle.upload_from_stream(
                path, data, metadata={"contentType": content_type, "size": len(data)}
            )
        except PyMongoError as exc:
            logger.exception("storage_upload_failed", extra={"bucket": bucket})
            raise UpstreamError("Could not store the file. Try again.") from exc
        return path

    def signed_url(self, bucket: str, path: str, expires: int) -> str:
        """A short-lived URL this API will serve the file from."""
        token = issue_file_token(bucket=bucket, path=path, expires_in=expires)
        base = settings.PUBLIC_API_URL.rstrip("/")
        return f"{base}{settings.API_PREFIX}/files/{token}"

    def open(self, bucket: str, path: str) -> tuple[bytes, str]:
        """The stored bytes and their content type.

        Used by the route behind a signed URL. A missing file raises rather
        than returning empty, so a broken link fails where the cause is still
        visible instead of at a blank image.
        """
        try:
            with self._bucket(bucket).open_download_stream_by_name(path) as stream:
                data = stream.read()
                metadata = stream.metadata or {}
        except NoFile as exc:
            logger.info("storage_missing", extra={"bucket": bucket, "path": path})
            raise NotFoundError("That file is no longer available.") from exc
        except PyMongoError as exc:
            logger.exception("storage_read_failed", extra={"bucket": bucket})
            raise UpstreamError("Could not read the file.") from exc
        return data, str(metadata.get("contentType") or "application/octet-stream")

    @staticmethod
    def _remove_existing(handle: GridFSBucket, path: str) -> None:
        for existing in handle.find({"filename": path}):
            handle.delete(existing._id)


class InMemoryObjectStore:
    """Used by tests and by a local run with no database attached."""

    def __init__(self) -> None:
        self.files: dict[tuple[str, str], bytes] = {}

    def upload(self, bucket: str, path: str, data: bytes, content_type: str) -> str:
        self.files[(bucket, path)] = data
        return path

    def signed_url(self, bucket: str, path: str, expires: int) -> str:
        return f"memory://{bucket}/{path}?expires={expires}"

    def open(self, bucket: str, path: str) -> tuple[bytes, str]:
        data = self.files.get((bucket, path))
        if data is None:
            raise NotFoundError("That file is no longer available.")
        return data, sniff_image_type(data) or "application/octet-stream"


class StorageService:
    """Where each kind of artefact lives, and what its path looks like."""

    def __init__(self, store: ObjectStore):
        self.store = store

    def upload_scan(
        self, *, assessment_id: uuid.UUID, submission_id: uuid.UUID, data: bytes, content_type: str
    ) -> str:
        """Validate the upload, shrink it, and store it under the assessment.

        Validation runs on what arrived, not on what is stored: the magic-byte
        check is there to reject a file that is not really an image, and
        rewriting it first would make that check inspect our own output.
        """
        detected = validate_upload(data, content_type)
        stored, rewritten = recompress_sheet(data)
        detected = rewritten or detected
        path = f"{assessment_id}/{submission_id}.{_extension(detected)}"
        return self.store.upload(settings.ANSWER_SHEET_BUCKET, path, stored, detected)

    def upload_sheet_pdf(self, *, assessment_id: uuid.UUID, filename: str, data: bytes) -> str:
        path = f"{assessment_id}/sheets/{safe_segment(filename)}.pdf"
        return self.store.upload(settings.ANSWER_SHEET_BUCKET, path, data, "application/pdf")

    def upload_certificate(self, *, class_id: uuid.UUID, certificate_id: uuid.UUID, data: bytes) -> str:
        path = f"{class_id}/{certificate_id}.pdf"
        return self.store.upload(settings.CERTIFICATE_BUCKET, path, data, "application/pdf")

    def scan_url(self, path: str) -> str:
        return self.store.signed_url(
            settings.ANSWER_SHEET_BUCKET, path, settings.SIGNED_URL_TTL_SECONDS
        )

    def certificate_url(self, path: str) -> str:
        return self.store.signed_url(
            settings.CERTIFICATE_BUCKET, path, settings.SIGNED_URL_TTL_SECONDS
        )


def _extension(mime: str) -> str:
    return {"image/jpeg": "jpg", "image/png": "png", "image/webp": "webp"}.get(mime, "bin")


def recompress_sheet(data: bytes) -> tuple[bytes, str]:
    """Shrink a sheet photo to what the grid actually needs.

    GridFS shares the cluster's storage with every record in it, so what a
    sheet costs to keep matters in a way it did not when the bucket was billed
    separately. A phone camera produces 2-4MB of detail; the marks on an answer
    sheet are large, high-contrast and monochrome, and survive 1600px and
    grayscale untouched. The result is around 200KB.

    The retained image is for a teacher to look at when reviewing a flagged
    paper - the scan itself has already happened against the full-resolution
    upload - so this trades nothing the system relies on.

    Returns the original bytes unchanged if recompression is switched off, if
    the image is already smaller than the target, or if OpenCV cannot read it.
    The last case matters: a sheet that fails to recompress must still be
    stored, because losing it would lose the evidence behind a score.
    """
    if not settings.SHEET_RECOMPRESS:
        return data, "image/jpeg" if data[:3] == b"\xff\xd8\xff" else ""

    try:
        import cv2
        import numpy as np

        image = cv2.imdecode(np.frombuffer(data, dtype=np.uint8), cv2.IMREAD_COLOR)
        if image is None:
            logger.info("sheet_recompress_unreadable")
            return data, ""

        height, width = image.shape[:2]
        longest = max(height, width)
        if longest > settings.SHEET_MAX_EDGE_PX:
            scale = settings.SHEET_MAX_EDGE_PX / longest
            # INTER_AREA is the right filter for shrinking: it averages the
            # pixels being merged, where INTER_LINEAR samples them and can
            # drop a thin pencil line entirely.
            image = cv2.resize(
                image,
                (round(width * scale), round(height * scale)),
                interpolation=cv2.INTER_AREA,
            )

        if settings.SHEET_GRAYSCALE:
            image = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)

        ok, encoded = cv2.imencode(
            ".jpg", image, [int(cv2.IMWRITE_JPEG_QUALITY), settings.SHEET_JPEG_QUALITY]
        )
        if not ok:
            return data, ""

        shrunk = encoded.tobytes()
    except Exception:  # noqa: BLE001 - storing the sheet matters more than shrinking it
        logger.exception("sheet_recompress_failed")
        return data, ""

    # Never grow a file. A small or already-compressed upload can encode larger
    # than it arrived, and keeping the original is then both smaller and truer.
    if len(shrunk) >= len(data):
        return data, ""

    logger.info(
        "sheet_recompressed",
        extra={"from_bytes": len(data), "to_bytes": len(shrunk)},
    )
    return shrunk, "image/jpeg"
