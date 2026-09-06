"""S3-compatible storage for user uploads, with URLs that stay behind the media gate.

Selected by ``UL_MEDIA_STORAGE_BACKEND=s3``; the default is still the local
filesystem, which is what a single-machine self-host needs and what every
deployment ran on before this existed.

**The ``url()`` override is the whole point of the subclass.** ``FileField.url``
is what templates, serializers and the external API all render, and
``S3Storage.url`` returns a presigned URL into the bucket. Swapping the backend
without this would have silently replaced every gated ``/media/...`` link with a
bearer token for one object - readable by anyone the URL reaches, for as long as
it lasts, with no session, no authorization walk, and no way to revoke it. That
is the access model :mod:`urbanlens.dashboard.services.media.access` exists to
enforce, so the URL a caller gets back is identical to the filesystem one and
every read keeps going through
:class:`~urbanlens.dashboard.controllers.media.MediaGateView`.

How the gate then gets the bytes is
:mod:`urbanlens.dashboard.controllers.media`'s decision, not this module's; see
``MediaByteSource`` there.
"""

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
    """Object storage whose public URL is the media gate rather than the bucket.

    Every other behaviour - opening, saving, deleting, size, existence - is
    django-storages'. Only URL generation changes, and it changes to exactly
    what ``FileSystemStorage`` produced, so nothing downstream can tell which
    backend is configured.
    """

    def url(self, name: str, parameters: dict[str, Any] | None = None, expire: int | timedelta | None = None, http_method: str | None = None) -> str:
        """Return the gated ``/media/`` URL for *name*.

        Deliberately identical to ``FileSystemStorage.url`` against
        ``MEDIA_URL``, including the leading-slash strip that makes ``urljoin``
        treat the name as relative - so an absolute ``UL_MEDIA_BASE_URL`` keeps
        moving uploads onto the media origin exactly as it does today.

        Args:
            name: The stored object's key.
            parameters: Ignored. Presigning parameters have no meaning for a
                URL that is not presigned.
            expire: Ignored, for the same reason.
            http_method: Ignored, for the same reason.

        Returns:
            ``MEDIA_URL`` joined with the URL-quoted name.
        """
        quoted = filepath_to_uri(name)
        if quoted is not None:
            quoted = quoted.lstrip("/")
        return urljoin(settings.MEDIA_URL, quoted)

    def signed_object_url(self, name: str, expire: int) -> str:
        """Return a presigned URL for *name*, for server-side use only.

        The one caller is the gate's ``X-Accel-Redirect`` hand-off, where the
        URL is put in a response header that nginx consumes and strips: it
        reaches the object store from inside the cluster and never reaches the
        client. Nothing may hand the return value to a browser - that is the
        access-model change this class exists to prevent.

        Args:
            name: The stored object's key, already authorized.
            expire: Lifetime in seconds. Seconds, not minutes: this is consumed
                by the same request that mints it.

        Returns:
            A presigned GET URL.
        """
        return super().url(name, expire=expire)
