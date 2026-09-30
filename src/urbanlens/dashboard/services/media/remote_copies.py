"""Third-party images served from this site instead of their provider's, and kept after the provider drops them.

A page that would show a provider's image links to :func:`copy_url` instead. The first request for it downloads the
source, the sandbox worker re-encodes it (``render_remote_image_copy``), and every later request is served from the
stored file, whether or not the provider still has it (Jess, 2026-09-30).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta
import hashlib
from typing import TYPE_CHECKING
from urllib.parse import urlsplit

from django.core.files.base import ContentFile
from django.urls import reverse
from django.utils import timezone

from urbanlens.dashboard.models.remote_image_copy.model import RemoteImageCopy

if TYPE_CHECKING:
    from collections.abc import Iterable

#: Longest edge of a stored copy: thumbnails, and the lightbox's fallback when the full image cannot load.
REMOTE_COPY_MAX_DIMENSION = 1200

#: Largest source accepted. Providers hand over full-resolution images where they have no thumbnail.
MAX_REMOTE_COPY_SOURCE_BYTES = 25 * 1024 * 1024

#: The first retry after a failed download; each further failure doubles it, up to :data:`MAX_RETRY_DELAY`.
FIRST_RETRY_DELAY = timedelta(hours=1)
MAX_RETRY_DELAY = timedelta(days=7)


@dataclass(frozen=True, slots=True)
class RemoteImage:
    """One remote image a page is about to show.

    Attributes:
        url: Its address at the provider.
        provider: Which feature or provider it came from, kept as provenance.
        page_url: The provider's page for it, when known.
        edition: Set for an address whose picture changes over time (a "current imagery" export): each edition is its
            own copy, and earlier ones are kept.
    """

    url: str
    provider: str
    page_url: str = ""
    edition: str = ""


def url_digest(url: str, edition: str = "") -> str:
    """The key a remote image is stored under."""
    return hashlib.sha256(f"{url}\n{edition}".encode() if edition else url.encode()).hexdigest()


def pending_marker(digest: str) -> str:
    """Cache key standing while a copy's render is queued, so concurrent requests fetch its source once."""
    return f"ul_remote_copy_{digest}"


def _is_remote(url: str) -> bool:
    return urlsplit(url).scheme in ("http", "https")


def copy_urls(images: Iterable[RemoteImage]) -> dict[str, str]:
    """This site's address for each remote image, recording any not seen before.

    Args:
        images: The images a page is about to show.

    Returns:
        Each http(s) source URL mapped to its in-app address. Anything else (an in-app path, a ``data:`` URI) is left
        out, for the caller to use as it is.
    """
    remote = {image.url: image for image in images if image.url and _is_remote(image.url)}
    if not remote:
        return {}
    RemoteImageCopy.objects.bulk_create(
        [
            RemoteImageCopy(
                url_digest=url_digest(url, image.edition),
                source_url=url,
                edition=image.edition,
                provider=image.provider[:64],
                page_url=image.page_url,
            )
            for url, image in remote.items()
        ],
        ignore_conflicts=True,
    )
    return {url: reverse("media.remote_copy", args=[url_digest(url, image.edition)]) for url, image in remote.items()}


def copy_url(url: str, *, provider: str, page_url: str = "", edition: str = "") -> str:
    """:func:`copy_urls` for one image, returning *url* unchanged when it is not a remote address.

    Args:
        url: The image's address.
        provider: Which feature or provider it came from.
        page_url: The provider's page for it, when known.
        edition: See :class:`RemoteImage`.

    Returns:
        The address to put in the page.
    """
    return copy_urls([RemoteImage(url, provider, page_url, edition)]).get(url, url)


def retry_is_due(copy: RemoteImageCopy) -> bool:
    """Whether a copy whose downloads failed may be tried again yet.

    Args:
        copy: The copy.

    Returns:
        True when it has never failed, or its backoff has run out.
    """
    if not copy.failed_attempts or copy.last_failed_at is None:
        return True
    delay = min(FIRST_RETRY_DELAY * 2 ** (copy.failed_attempts - 1), MAX_RETRY_DELAY)
    return timezone.now() - copy.last_failed_at >= delay


def record_failure(copy: RemoteImageCopy) -> None:
    """Count one failed download or render of *copy*.

    Args:
        copy: The copy.
    """
    from django.db.models import F

    RemoteImageCopy.objects.filter(pk=copy.pk).update(failed_attempts=F("failed_attempts") + 1, last_failed_at=timezone.now())


def store(copy: RemoteImageCopy, content: bytes, content_type: str) -> None:
    """Keep a rendered copy.

    Args:
        copy: The copy.
        content: The re-encoded image.
        content_type: Its type.
    """
    extension = "png" if content_type == "image/png" else "jpg"
    copy.file.save(f"{copy.url_digest}.{extension}", ContentFile(content), save=False)
    copy.content_type = content_type
    copy.file_size = len(content)
    copy.checksum = hashlib.sha256(content).hexdigest()
    copy.fetched_at = timezone.now()
    copy.save(update_fields=["file", "content_type", "file_size", "checksum", "fetched_at", "updated"])
