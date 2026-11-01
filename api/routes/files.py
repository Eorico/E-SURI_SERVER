"""Serving stored files behind a signed URL.

This route exists because GridFS has no URLs. A storage provider could hand the
browser a signed link and serve the bytes itself; GridFS serves them over the
database connection, so the API signs a token and streams the file.

Authentication is the token in the path, not a bearer header, and deliberately
so: these URLs go into `<img src>` and download links, which the browser
fetches without any header we could set. The token therefore carries the whole
capability, which is why it names exactly one file, expires within the hour
(`SIGNED_URL_TTL_SECONDS`), and is signed with a `typ` that
`decode_access_token` refuses - so a leaked file link cannot be turned into a
session.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Response

from api.core.tokens import read_file_token
from api.deps import get_storage
from api.services.storage_service import StorageService

router = APIRouter(prefix="/files", tags=["files"])


@router.get(
    "/{token}",
    summary="Download a stored file",
    response_class=Response,
    responses={
        200: {"content": {"application/octet-stream": {}}},
        401: {"description": "The link has expired or was not signed by this server."},
        404: {"description": "The file is no longer stored."},
    },
)
def download(token: str, storage: StorageService = Depends(get_storage)) -> Response:
    """Stream the file a signed token names."""
    bucket, path = read_file_token(token)
    data, content_type = storage.store.open(bucket, path)
    return Response(
        content=data,
        media_type=content_type,
        headers={
            # Private: the URL is a capability, so a shared cache must never
            # hold the bytes where another viewer could be handed them.
            "Cache-Control": "private, max-age=300",
            "Content-Length": str(len(data)),
            # Stops a browser from deciding an uploaded file is HTML and
            # running it against this origin.
            "X-Content-Type-Options": "nosniff",
        },
    )
