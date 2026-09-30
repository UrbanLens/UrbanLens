"""Server-side previews for Media-gallery items a browser can't render itself.
A TIFF renders as a broken image in every browser except Safari, and a PDF never renders in an ``<img>`` at all - so those items were reaching the page and then disappearing into the broken-image fallback or a grey document icon."""

from __future__ import annotations

from dataclasses import dataclass
from io import BytesIO
import logging
from pathlib import Path
import posixpath
import time
from typing import TYPE_CHECKING
from urllib.parse import urlsplit
from uuid import uuid4

from django.core.cache import cache
from django.http import HttpResponse

from urbanlens.dashboard.services.media.images import pixels_only
from urbanlens.dashboard.services.sandbox import untrusted_parse

if TYPE_CHECKING:
    from collections.abc import Sequence

    from urbanlens.dashboard.services.apis.assets.base import MediaItem

logger = logging.getLogger(__name__)

#: Content types every current browser renders in an ``<img>`` directly.
WEB_SAFE_CONTENT_TYPES = frozenset(
    {
        "image/jpeg",
        "image/jpg",
        "image/png",
        "image/gif",
        "image/webp",
        "image/avif",
        "image/svg+xml",
    },
)

#: Extensions matching :data:`WEB_SAFE_CONTENT_TYPES`, for the common case of a
#: provider that returns a bare URL and no content type.
WEB_SAFE_EXTENSIONS = frozenset({".jpg", ".jpeg", ".png", ".gif", ".webp", ".avif", ".svg"})

#: Formats a browser won't render but this module can convert. Anything not in
#: here *and* not web-safe (a .zip, a .doc) is left alone - a preview attempt
#: would only turn a correctly-iconed tile into a broken one.
RENDERABLE_CONTENT_TYPES = frozenset(
    {
        "application/pdf",
        "image/tiff",
        "image/x-tiff",
        "image/heic",
        "image/heif",
        "image/bmp",
        "image/x-ms-bmp",
        "image/jp2",
        "image/jpx",
        "image/x-icon",
        "image/vnd.microsoft.icon",
        "image/x-portable-pixmap",
        "image/x-targa",
    },
)

#: Extensions matching :data:`RENDERABLE_CONTENT_TYPES`.
RENDERABLE_EXTENSIONS = frozenset({".pdf", ".tif", ".tiff", ".heic", ".heif", ".bmp", ".jp2", ".jpf", ".jpx", ".ico", ".ppm", ".tga", ".dng"})

#: Longest edge of a rendered image. The lightbox falls back to this same image
#: when the full-size file won't load, so this is sized for the latter.
PREVIEW_MAX_DIMENSION = 1200

#: JPEG quality for rendered images: display copies, not archival ones.
PREVIEW_JPEG_QUALITY = 82


def _extension(url: str) -> str:
    """The lowercased file extension of a URL's path, or ``""``."""
    return posixpath.splitext(urlsplit(url).path)[1].lower()


def is_web_safe(url: str, content_type: str = "") -> bool:
    """Whether a browser can render this item directly in an ``<img>``.

    Args:
        url: The item's URL.
        content_type: The provider-declared content type, when known - it wins over the extension, which is frequently absent or wrong on an API-generated URL.

    Returns:
        True when the item needs no server-side conversion."""
    declared = (content_type or "").split(";")[0].strip().lower()
    if declared:
        return declared in WEB_SAFE_CONTENT_TYPES
    return _extension(url) in WEB_SAFE_EXTENSIONS


def needs_server_side_preview(url: str, content_type: str = "") -> bool:
    """Whether this item must be converted server-side to be displayable.

    Args:
        url: The item's URL.
        content_type: The provider-declared content type, when known.

    Returns:
        True when :func:`render_preview` is expected to be able to produce an image for this item."""
    if not url:
        return False
    declared = (content_type or "").split(";")[0].strip().lower()
    if declared:
        return declared in RENDERABLE_CONTENT_TYPES
    return _extension(url) in RENDERABLE_EXTENSIONS


_FETCH_TIMEOUT = 20
_MAX_REDIRECTS = 5
_USER_AGENT = "UrbanLens/1.0 (https://github.com/urbanlens/urbanlens; jess.a.mann@gmail.com) python-requests/2.x"


def fetch_remote_source(url: str, *, max_bytes: int) -> tuple[bytes, str] | None:
    """Download a remote file for a server-side render, pinning each hop to the address it validated to.

    Args:
        url: The absolute http(s) URL to fetch.
        max_bytes: Largest body accepted.

    Returns:
        ``(body, content_type)``, or None when the URL was unsafe, the fetch failed, or the body was over *max_bytes*.
    """
    import requests

    from urbanlens.dashboard.services.security.redact import redact_text
    from urbanlens.dashboard.services.security.url_safety import UnsafeUrlError, fetch_public_url

    try:
        response = fetch_public_url(url, headers={"User-Agent": _USER_AGENT}, timeout=_FETCH_TIMEOUT, max_redirects=_MAX_REDIRECTS)
    except UnsafeUrlError:
        logger.info("Remote source rejected as unsafe: %s", redact_text(url))
        return None
    except requests.RequestException:
        logger.info("Remote source fetch failed: %s", redact_text(url))
        return None

    with response:
        if response.status_code != 200:
            return None
        body = bytearray()
        for chunk in response.iter_content(64 * 1024):
            body.extend(chunk)
            if len(body) > max_bytes:
                logger.info("Remote source exceeded the size cap: %s", redact_text(url))
                return None
        return bytes(body), response.headers.get("Content-Type", "")


def _with_preview_flag(url: str) -> str:
    """Append ``preview=1`` to an in-app proxy URL, preserving any existing query."""
    return f"{url}{'&' if '?' in url else '?'}preview=1"


def _thumb_source(item_url: str, thumb_url: str, content_type: str) -> str:
    """Where a gallery tile's picture comes from: the provider's thumbnail, else the item when it can be shown."""
    if thumb_url:
        return thumb_url
    if is_web_safe(item_url, content_type) or needs_server_side_preview(item_url, content_type):
        return item_url
    return ""


@dataclass(frozen=True, slots=True)
class GalleryUrls:
    """Where a gallery tile gets its pictures.

    Attributes:
        thumb: The tile's ``<img src>``, ``""`` for an icon tile.
        view: What the lightbox shows in place of the provider's own file, ``""`` to show the item's own URL.
    """

    thumb: str
    view: str


def _gallery_url(source: str, copies: dict[str, str], declared: str) -> str:
    if source in copies:
        return copies[source]
    if source.startswith("/"):
        return _with_preview_flag(source) if needs_server_side_preview(source, declared) else source
    return ""


def gallery_urls(items: Sequence[MediaItem], *, provider: str, with_views: bool = True) -> list[GalleryUrls]:
    """The pictures for each gallery item, in order.

    A provider's image is never linked directly: it becomes this site's copy (``remote_copies``), made and kept on first
    view. An in-app proxy URL is used as it is, flagged for a server-side preview when a browser cannot render it.

    Args:
        items: The gallery's items.
        provider: The gallery source, kept as each copy's provenance.
        with_views: Whether to prepare the lightbox's copies too.

    Returns:
        One :class:`GalleryUrls` per item.
    """
    from urbanlens.dashboard.services.media.remote_copies import RemoteImage, copy_urls

    thumbs = [_thumb_source(item.url, item.thumb_url, item.content_type) for item in items]
    views = [item.url if with_views and (is_web_safe(item.url, item.content_type) or needs_server_side_preview(item.url, item.content_type)) else "" for item in items]
    wanted = [RemoteImage(source, provider, item.page_url) for sources in (thumbs, views) for source, item in zip(sources, items, strict=True) if source]
    copies = copy_urls(wanted)
    return [
        GalleryUrls(
            thumb=_gallery_url(thumb, copies, item.content_type if thumb == item.url else ""),
            view=copies.get(view, ""),
        )
        for thumb, view, item in zip(thumbs, views, items, strict=True)
    ]


def gallery_thumb_urls(items: Sequence[MediaItem], *, provider: str) -> list[str]:
    """Each gallery item's ``<img src>``, without preparing lightbox copies (see :func:`gallery_urls`).

    Args:
        items: The gallery's items.
        provider: The gallery source.

    Returns:
        One URL per item, ``""`` where the tile should fall back to an icon.
    """
    return [urls.thumb for urls in gallery_urls(items, provider=provider, with_views=False)]


def gallery_thumb_url(item: MediaItem, *, provider: str) -> str:
    """:func:`gallery_thumb_urls` for one item.

    Args:
        item: The gallery item.
        provider: The gallery source.

    Returns:
        The tile's ``<img src>``, or ``""``.
    """
    return gallery_thumb_urls([item], provider=provider)[0]


@untrusted_parse("document.render")
def _pdf_first_page(raw: bytes):
    """Render a PDF's first page to a PIL image, or None when poppler can't.

    Args:
        raw: The PDF file's bytes.

    Returns:
        A ``PIL.Image.Image``, or None.
    """
    try:
        from pdf2image import convert_from_bytes

        pages = convert_from_bytes(raw, first_page=1, last_page=1, fmt="ppm", size=(None, PREVIEW_MAX_DIMENSION))
    except Exception:
        logger.warning("PDF preview rendering failed", exc_info=True)
        return None
    return pages[0] if pages else None


@untrusted_parse("image.decode")
def render_preview(raw: bytes, content_type: str = "", *, max_dimension: int = PREVIEW_MAX_DIMENSION) -> tuple[bytes, str] | None:
    """Convert one file's bytes into a browser-renderable image.

    Args:
        raw: The source file's bytes.
        content_type: The declared content type, used only as a hint.
        max_dimension: Longest edge of the result.

    Returns:
        ``(image_bytes, content_type)`` for the converted image, or None when the source couldn't be decoded as either a PDF or an image."""
    if not raw:
        return None

    declared = (content_type or "").split(";")[0].strip().lower()
    if raw[:5] == b"%PDF-" or declared == "application/pdf":
        image = _pdf_first_page(raw)
    else:
        try:
            from PIL import Image as PILImage

            image = PILImage.open(BytesIO(raw))
            image.load()
        except Exception:
            logger.info("Preview source could not be decoded as an image (declared %r)", declared, exc_info=True)
            return None

    if image is None:
        return None

    try:
        from PIL import Image as PILImage

        # Multi-frame sources (a multi-page TIFF, an animated format) preview
        # as their first frame, matching the PDF branch above.
        if getattr(image, "n_frames", 1) > 1:
            image.seek(0)
        has_alpha = image.mode in ("RGBA", "LA") or (image.mode == "P" and "transparency" in image.info)
        image = pixels_only(image.convert("RGBA" if has_alpha else "RGB"))
        image.thumbnail((max_dimension, max_dimension), PILImage.Resampling.LANCZOS)
        buffer = BytesIO()
        if has_alpha:
            image.save(buffer, format="PNG", optimize=True)
            return buffer.getvalue(), "image/png"
        image.save(buffer, format="JPEG", quality=PREVIEW_JPEG_QUALITY, optimize=True)
    except Exception:
        logger.warning("Preview encoding failed (declared %r)", declared, exc_info=True)
        return None
    return buffer.getvalue(), "image/jpeg"


#: Cache sentinel for "this source was decoded and could not be previewed".
#: Shared by both preview endpoints and by the task that writes it, so a
#: provider serving something unconvertible is not re-decoded per tile.
UNPREVIEWABLE = "unpreviewable"

#: Cache sentinel for "a render is already queued for this key". A gallery page
#: fires one request per tile at once; without it, twenty tiles for the same
#: uncached item would queue twenty identical renders.
RENDER_QUEUED = "queued"

#: How long :data:`RENDER_QUEUED` stands before another request will re-queue.
#: Long enough to cover a render plus the queue behind it, short enough that a
#: worker that died mid-render does not wedge the tile for the whole day.
RENDER_QUEUED_TTL = 120

#: Where a source file waits between the web process staging it and the sandbox worker decoding it.
#: Under MEDIA_ROOT because that is the one writable volume both containers mount; nothing serves
#: it, because every media URL resolves through an ``Image`` row and these files have none.
PREVIEW_SOURCE_DIR = "preview_sources"

#: How long a staged source survives an un-run render before the sweep removes it.
#: Must outlive a queue backlog; anything older is an orphan whose task never ran (a broker outage
#: at enqueue time).
PREVIEW_SOURCE_MAX_AGE = 3600


def _preview_source_root() -> Path:
    """The directory staged sources live in, created if it does not exist."""
    from django.conf import settings

    root = Path(settings.MEDIA_ROOT) / PREVIEW_SOURCE_DIR
    root.mkdir(parents=True, exist_ok=True)
    return root


def stage_preview_source(digest: str, raw: bytes, content_type: str) -> dict[str, str]:
    """Write a source file where the sandbox worker can read it.

    Args:
        digest: A stable hash of the source URL, used as the filename.
        raw: The file's bytes.
        content_type: The provider-declared content type, passed through to the renderer as a hint.

    Returns:
        A small descriptor - filename plus content type, not bytes - for the render task's arguments and :func:`load_preview_source`."""
    root = _preview_source_root()
    target = root / f"{digest}.bin"
    # Written beside and renamed, so a worker never reads a half-written file.
    scratch = root / f"{digest}.{uuid4().hex}.part"
    scratch.write_bytes(raw)
    scratch.replace(target)
    return {"name": target.name, "content_type": content_type}


def load_preview_source(descriptor: dict[str, str]) -> tuple[bytes, str] | None:
    """Read back what :func:`stage_preview_source` wrote.

    Args:
        descriptor: The value :func:`stage_preview_source` returned.

    Returns:
        ``(bytes, content_type)``, or None when the file is gone - swept as an orphan, or already consumed by an earlier render."""
    target = _preview_source_root() / Path(descriptor.get("name", "")).name
    try:
        return target.read_bytes(), descriptor.get("content_type", "")
    except OSError:
        return None


def discard_preview_source(descriptor: dict[str, str]) -> None:
    """Remove a staged source once its render is done with it.

    Args:
        descriptor: The value :func:`stage_preview_source` returned.
    """
    target = _preview_source_root() / Path(descriptor.get("name", "")).name
    target.unlink(missing_ok=True)


def sweep_preview_sources(max_age: int = PREVIEW_SOURCE_MAX_AGE) -> int:
    """Delete staged sources whose render never ran.

    Args:
        max_age: Age in seconds past which a staged file is an orphan.

    Returns:
        How many files were removed."""
    cutoff = time.time() - max_age
    removed = 0
    try:
        entries = list(_preview_source_root().iterdir())
    except OSError:
        logger.warning("Could not list the staged preview-source directory", exc_info=True)
        return 0
    for entry in entries:
        try:
            if entry.is_file() and entry.stat().st_mtime < cutoff:
                entry.unlink(missing_ok=True)
                removed += 1
        except OSError:
            logger.warning("Could not remove staged preview source %s", entry, exc_info=True)
    return removed


def request_sandbox_render(source_cache_key: str, preview_cache_key: str, *, ttl: int, failure_ttl: int) -> None:
    """Queue a preview render in the sandbox worker, at most once per key. :func:`render_preview` reaches Pillow and poppler, so it must not run in a web process - see :mod:`urbanlens.dashboard.services.sandbox.guard`.

    Args:
        source_cache_key: Cache key holding the source's ``(bytes, content_type)`` pair.
        preview_cache_key: Cache key the rendered preview is written to.
        ttl: Seconds to cache a successful render.
        failure_ttl: Seconds to cache the :data:`UNPREVIEWABLE` sentinel."""
    from urbanlens.dashboard.services.core.celery import safely_enqueue_task
    from urbanlens.dashboard.tasks import render_media_preview

    # add() is atomic in both the Redis and locmem backends, so concurrent tile
    # requests race here rather than at the queue.
    if not cache.add(preview_cache_key, RENDER_QUEUED, RENDER_QUEUED_TTL):
        return
    if safely_enqueue_task(render_media_preview, source_cache_key, preview_cache_key, ttl, failure_ttl, durable=False) is None:
        # Broker unreachable. Drop the marker so the next request retries rather
        # than waiting out RENDER_QUEUED_TTL against a queue nothing was put on.
        cache.delete(preview_cache_key)


#: Seconds a client is told to wait before asking again for a preview still being rendered.
PREVIEW_RETRY_AFTER_SECONDS = 3


def unfinished_preview_response(preview_cache_key: str) -> HttpResponse:
    """The answer for a preview with no finished render: 503 with Retry-After while one is queued, else 404.

    Args:
        preview_cache_key: The key :func:`request_sandbox_render` was given.

    Returns:
        The response. A 404 means no preview is coming (unconvertible, or the render could not be queued),
        which the gallery's onerror handler turns into the icon tile.
    """
    if cache.get(preview_cache_key) != RENDER_QUEUED:
        return HttpResponse(status=404)
    response = HttpResponse(status=503)
    response["Retry-After"] = str(PREVIEW_RETRY_AFTER_SECONDS)
    response["Cache-Control"] = "no-store"
    return response


def cached_preview(preview_cache_key: str) -> tuple[bytes, str] | None:
    """Read a previously rendered preview, treating both sentinels as "no preview".

    Args:
        preview_cache_key: The key :func:`request_sandbox_render` was given.

    Returns:
        ``(image_bytes, content_type)``, or None when the render has not finished, was never queued, or produced nothing."""
    cached = cache.get(preview_cache_key)
    if cached is None or cached in (UNPREVIEWABLE, RENDER_QUEUED):
        return None
    return cached
