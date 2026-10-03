"""Third-party images served from this site instead of their provider's, and kept after the provider drops them.

A page that would show a provider's image links to :func:`copy_url` instead. The first request for it downloads the
source, the sandbox worker re-encodes it (``render_remote_image_copy``), and every later request is served from the
stored file, whether or not the provider still has it (Jess, 2026-09-30).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta
import hashlib
from io import BytesIO
from typing import TYPE_CHECKING
from urllib.parse import urlsplit

from django.core.files.base import ContentFile
from django.urls import reverse
from django.utils import timezone

from urbanlens.dashboard.models.remote_image_copy.model import RemoteImageCopy
from urbanlens.dashboard.services.security.throttle import Rate

if TYPE_CHECKING:
    from collections.abc import Iterable

#: Longest edge of a stored copy: thumbnails, and the lightbox's fallback when the full image cannot load.
REMOTE_COPY_MAX_DIMENSION = 1200

#: Longest edge of the copy a gallery tile asks for with ``?size=thumb``.
REMOTE_COPY_TILE_DIMENSION = 400
TILE_SIZE = "thumb"

#: Largest source accepted. Providers hand over full-resolution images where they have no thumbnail.
MAX_REMOTE_COPY_SOURCE_BYTES = 25 * 1024 * 1024

#: The first retry after a failed download; each further failure doubles it, up to :data:`MAX_RETRY_DELAY`.
FIRST_RETRY_DELAY = timedelta(hours=1)
MAX_RETRY_DELAY = timedelta(days=7)

#: How long the download may take. It runs on a worker, so a slow provider costs no web request: the USGS
#: National Map's export took 18 s and 31 s to answer (P180).
DOWNLOAD_TIMEOUT_SECONDS = 90

#: How long a copy stays pending before another request may start it again: the download, the render, and the
#: queues ahead of each. A copy waiting for a download slot renews it with each wait.
COPY_PENDING_TTL = 300

#: First downloads one caller may start per window. A page asks for every tile at once.
COPY_RATE = Rate(limit=600, window_seconds=60)
COPY_THROTTLE_SCOPE = "media.remote_copy"

#: First downloads in flight at once, site-wide. They run on the interactive worker beside safety deadlines (and,
#: on k3s, on the one worker that drains every queue), so slow providers must leave it room.
DOWNLOAD_SLOTS = 1
#: A slot outlives the download; past this a worker that died holding one gives it back.
DOWNLOAD_SLOT_TTL = DOWNLOAD_TIMEOUT_SECONDS + 60
#: How long a copy that found every slot busy sits on the broker before it asks again. It holds no worker meanwhile.
DOWNLOAD_SLOT_WAIT_SECONDS = 3
#: How long a copy waits for a slot before it gives up uncounted, for a later request to queue it again.
DOWNLOAD_SLOT_WAIT_LIMIT_SECONDS = 600


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


def take_download_slot(holder: str) -> str | None:
    """Claim one of the site-wide :data:`DOWNLOAD_SLOTS` for a copy's first download.

    Args:
        holder: What the slot is held for (the copy's id), so only that download frees it.

    Returns:
        The slot's key, or None when every slot is busy.
    """
    from django.core.cache import cache

    for index in range(DOWNLOAD_SLOTS):
        key = f"ul_remote_copy_download_slot_{index}"
        if cache.add(key, holder, DOWNLOAD_SLOT_TTL):
            return key
    return None


def release_download_slot(key: str, holder: str) -> None:
    """Give back a slot :func:`take_download_slot` returned, unless it already expired and went to another copy.

    Args:
        key: The slot's key.
        holder: What took it.
    """
    from urbanlens.dashboard.services.core import counters

    if key:
        counters.delete_if_value(key, holder)


def wait_for_download_slot(copy: RemoteImageCopy, waited: int) -> bool:
    """Queue *copy*'s download again for when a slot may have freed, keeping it pending meanwhile.

    Args:
        copy: The copy that found every slot busy.
        waited: Seconds it has already waited.

    Returns:
        True when it was queued again. False when it has waited :data:`DOWNLOAD_SLOT_WAIT_LIMIT_SECONDS` or could not
        be queued; it is then no longer pending, so the next request for it starts it afresh.
    """
    from django.core.cache import cache

    from urbanlens.dashboard.services.core.celery import safely_enqueue_task
    from urbanlens.dashboard.services.media.previews import RENDER_QUEUED
    from urbanlens.dashboard.tasks import fetch_remote_image_copy

    marker = pending_marker(copy.url_digest)
    if waited < DOWNLOAD_SLOT_WAIT_LIMIT_SECONDS:
        cache.set(marker, RENDER_QUEUED, COPY_PENDING_TTL)
        if safely_enqueue_task(fetch_remote_image_copy, copy.pk, countdown=DOWNLOAD_SLOT_WAIT_SECONDS, waited=waited + DOWNLOAD_SLOT_WAIT_SECONDS, durable=False) is not None:
            return True
    cache.delete(marker)
    return False


def forgive_timeout(copy: RemoteImageCopy) -> bool:
    """Whether a timed-out download may go uncounted: the first in :data:`FIRST_RETRY_DELAY` does.

    A provider that usually answers in seconds sometimes takes longer than the timeout (P184), and counting that
    like a refusal would show an icon for the next hour.

    Args:
        copy: The copy whose download timed out.

    Returns:
        True when this timeout is forgiven, so the next request may try again at once.
    """
    from django.core.cache import cache

    return bool(cache.add(f"ul_remote_copy_timeout_{copy.url_digest}", 1, int(FIRST_RETRY_DELAY.total_seconds())))


def pending_marker(digest: str) -> str:
    """Cache key standing while a copy's render is queued, so concurrent requests fetch its source once."""
    return f"ul_remote_copy_{digest}"


def absolute_remote_url(url: str) -> str | None:
    """The http(s) address *url* names on another host, or None when it names none.

    A protocol-relative ``//host/path`` (SearXNG returns them) is another host's image, fetched over https.

    Args:
        url: An image address as a provider or page gave it.

    Returns:
        The absolute URL, or None for an in-app path, a ``data:`` URI, or anything ``urlsplit`` refuses.
    """
    absolute = f"https:{url}" if url.startswith("//") else url
    try:
        return absolute if urlsplit(absolute).scheme in ("http", "https") else None
    except ValueError:
        return None


def copy_urls(images: Iterable[RemoteImage]) -> dict[str, str]:
    """This site's address for each remote image, recording any not seen before.

    Args:
        images: The images a page is about to show.

    Returns:
        Each remote source URL, as given, mapped to its in-app address. Anything else (an in-app path, a ``data:`` URI)
        is left out, for the caller to use as it is.
    """
    remote = {image.url: (absolute, image) for image in images if image.url and (absolute := absolute_remote_url(image.url))}
    if not remote:
        return {}
    RemoteImageCopy.objects.bulk_create(
        [
            RemoteImageCopy(
                url_digest=url_digest(absolute, image.edition),
                source_url=absolute,
                edition=image.edition,
                provider=image.provider[:64],
                page_url=image.page_url,
            )
            for absolute, image in remote.values()
        ],
        ignore_conflicts=True,
    )
    return {url: reverse("media.remote_copy", args=[url_digest(absolute, image.edition)]) for url, (absolute, image) in remote.items()}


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


def tile_copy_url(copy_address: str) -> str:
    """A copy's address for a gallery tile, which is served the tile-sized file where one was made."""
    return f"{copy_address}?size={TILE_SIZE}"


def wants_tile_copy(content: bytes) -> bool:
    """Whether a rendered copy is larger than a tile needs. Reads only the header of an image this site encoded."""
    from PIL import Image as PILImage

    with PILImage.open(BytesIO(content)) as image:
        return max(image.size) > REMOTE_COPY_TILE_DIMENSION


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


def _extension(content_type: str) -> str:
    return "png" if content_type == "image/png" else "jpg"


def store(copy: RemoteImageCopy, content: bytes, content_type: str, tile: tuple[bytes, str] | None = None) -> None:
    """Keep a rendered copy.

    Args:
        copy: The copy.
        content: The re-encoded image.
        content_type: Its type.
        tile: The same image at a tile's size, when the copy is larger than that.
    """
    copy.file.save(f"{copy.url_digest}.{_extension(content_type)}", ContentFile(content), save=False)
    if tile is not None:
        copy.thumb_file.save(f"{copy.url_digest}.{TILE_SIZE}.{_extension(tile[1])}", ContentFile(tile[0]), save=False)
    copy.content_type = content_type
    copy.file_size = len(content)
    copy.checksum = hashlib.sha256(content).hexdigest()
    copy.fetched_at = timezone.now()
    copy.save(update_fields=["file", "thumb_file", "content_type", "file_size", "checksum", "fetched_at", "updated"])
