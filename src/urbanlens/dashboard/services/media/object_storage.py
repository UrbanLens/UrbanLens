"""S3-compatible storage for user uploads, with URLs that stay behind the media gate."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any
from urllib.parse import urljoin

from django.conf import settings
from django.utils.encoding import filepath_to_uri
from storages.backends.s3 import S3Storage

if TYPE_CHECKING:
    from datetime import timedelta

__all__ = ["GatedS3Storage"]


class GatedS3Storage(S3Storage):
    """Object storage whose public URL is the media gate rather than the bucket."""

    def url(self, name: str, parameters: dict[str, Any] | None = None, expire: int | timedelta | None = None, http_method: str | None = None) -> str:
        """Return the gated ``/media/`` URL for *name*.

        Returns:
            ``MEDIA_URL`` joined with the URL-quoted name."""
        quoted = filepath_to_uri(name)
        if quoted is not None:
            quoted = quoted.lstrip("/")
        return urljoin(settings.MEDIA_URL, quoted)

    def signed_object_url(self, name: str, expire: int) -> str:
        """Return a presigned URL for *name*, for server-side use only.
        The one caller is the gate's ``X-Accel-Redirect`` hand-off, where the URL is put in a response header that nginx consumes and strips: it reaches the object store from inside the cluster and never reaches the client.

        Returns:
            A presigned GET URL."""
        return super().url(name, expire=expire)
