"""Object storage for scans, sheets and certificates.

Wraps Supabase Storage behind a narrow interface so services never import the
SDK and tests can substitute an in-memory double (DIP). Files are private; the
API hands out short-lived signed URLs rather than public links.
"""

from __future__ import annotations

import re
import uuid
from typing import Protocol

from api.core.config import settings
from api.core.exceptions import UpstreamError, ValidationError
from api.core.logging import get_logger

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


class SupabaseObjectStore:
    """The real store. Uses the service-role key, so it never leaves the server."""

    def __init__(self, client_factory=None):
        self._client_factory = client_factory

    def _client(self):
        if self._client_factory is not None:
            return self._client_factory()
        from api.core.supabase import get_supabase  # deferred: optional at import time

        return get_supabase()

    def upload(self, bucket: str, path: str, data: bytes, content_type: str) -> str:
        try:
            self._client().storage.from_(bucket).upload(
                path, data, {"content-type": content_type, "upsert": "true"}
            )
        except Exception as exc:  # pragma: no cover - network dependent
            logger.exception("storage_upload_failed", extra={"bucket": bucket})
            raise UpstreamError("Could not store the file. Try again.") from exc
        return path

    def signed_url(self, bucket: str, path: str, expires: int) -> str:
        try:
            result = self._client().storage.from_(bucket).create_signed_url(path, expires)
        except Exception as exc:  # pragma: no cover - network dependent
            logger.exception("storage_sign_failed", extra={"bucket": bucket})
            raise UpstreamError("Could not produce a download link.") from exc

        # Storage reports a missing object by returning a null URL rather than
        # raising, so an empty string here would travel all the way to a broken
        # download button. Fail where the cause is still visible.
        url = result.get("signedURL") or result.get("signedUrl")
        if not url:
            logger.error("storage_sign_empty", extra={"bucket": bucket, "path": path})
            raise UpstreamError("That file is no longer available.")
        return url


class InMemoryObjectStore:
    """Used by tests and by a local run with no Supabase project attached."""

    def __init__(self) -> None:
        self.files: dict[tuple[str, str], bytes] = {}

    def upload(self, bucket: str, path: str, data: bytes, content_type: str) -> str:
        self.files[(bucket, path)] = data
        return path

    def signed_url(self, bucket: str, path: str, expires: int) -> str:
        return f"memory://{bucket}/{path}?expires={expires}"


class StorageService:
    """Where each kind of artefact lives, and what its path looks like."""

    def __init__(self, store: ObjectStore):
        self.store = store

    def upload_scan(
        self, *, assessment_id: uuid.UUID, submission_id: uuid.UUID, data: bytes, content_type: str
    ) -> str:
        detected = validate_upload(data, content_type)
        path = f"{assessment_id}/{submission_id}.{_extension(detected)}"
        return self.store.upload(settings.ANSWER_SHEET_BUCKET, path, data, detected)

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
