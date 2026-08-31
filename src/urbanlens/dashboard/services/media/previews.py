"""Server-side previews for Media-gallery items a browser can't render itself.

The Media gallery (pin detail and wiki) renders every item as a plain
``<img src=...>``. That silently fails for a large share of what external
providers actually return: Wikimedia and the Library of Congress serve TIFFs,
CRIS and NRHP serve scanned PDFs, and Apple-sourced archives serve HEIC. A
TIFF renders as a broken image in every browser except Safari, and a PDF
never renders in an ``<img>`` at all - so those items were reaching the page
and then disappearing into the broken-image fallback or a grey document icon.

This module answers two questions and does one conversion:

* :func:`needs_server_side_preview` - "would a browser choke on this?", from a
  provider-declared content type when there is one and the URL's extension
  otherwise.
* :func:`preview_thumb_url` - the URL the gallery should actually put in
  ``src``: the item's own thumbnail when it's already web-safe, and a
  server-rendered preview of it (or of the full-size file, for a provider that
  publishes no thumbnail at all) when it isn't.
* :func:`render_preview` - the conversion itself: first page for a PDF (via
  poppler, already installed for the OCR pipeline - see
  ``services.media.documents``), Pillow for everything else, out as JPEG/PNG.

Remote URLs are fetched through a signed generic endpoint
(``controllers.media_preview``) rather than by the template, so this never
becomes an open image-fetching relay - same reasoning as the Google Maps photo
proxy's signature (see ``controllers.media_proxy``). In-app proxy routes
(CRIS attachments, LoopNet photos) already have the bytes server-side and take
a ``?preview=1`` flag instead of a second round trip.
"""

from __future__ import annotations

from io import BytesIO
import logging
from pathlib import Path
import posixpath
import time
from urllib.parse import urlencode, urlsplit
from uuid import uuid4

from django.core.cache import cache
from django.core.signing import Signer

from urbanlens.dashboard.services.sandbox import untrusted_parse

logger = logging.getLogger(__name__)

#: Salt for the generic preview endpoint's URL signature. Binds a preview
#: request to a URL this server itself emitted into a gallery, so the endpoint
#: can never be pointed at an arbitrary third-party URL by a client.
PREVIEW_SIGNER_SALT = "urbanlens.media_previews.source_url"

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

#: Longest edge of a generated preview. Media tiles are ~200 px and the
#: lightbox falls back to this same image when the full-size file won't load,
#: so this is sized for the latter.
PREVIEW_MAX_DIMENSION = 1200

#: JPEG quality for generated previews - these are disposable thumbnails
#: regenerated on demand, not archival copies.
PREVIEW_JPEG_QUALITY = 82

#: Bound on how much of a remote file the preview endpoint will pull down. A
#: scanned inventory-form PDF or an archival TIFF is genuinely large, so this
#: is well above the gallery's own materialize cap.
MAX_PREVIEW_SOURCE_BYTES = 60 * 1024 * 1024


def _extension(url: str) -> str:
    """The lowercased file extension of a URL's path, or ``""``."""
    return posixpath.splitext(urlsplit(url).path)[1].lower()


def is_web_safe(url: str, content_type: str = "") -> bool:
    """Whether a browser can render this item directly in an ``<img>``.

    Args:
        url: The item's URL. Only its path extension is consulted.
        content_type: The provider-declared content type, when known - it wins
            over the extension, which is frequently absent or wrong on an
            API-generated URL.

    Returns:
        True when the item needs no server-side conversion.
    """
    declared = (content_type or "").split(";")[0].strip().lower()
    if declared:
        return declared in WEB_SAFE_CONTENT_TYPES
    return _extension(url) in WEB_SAFE_EXTENSIONS


def needs_server_side_preview(url: str, content_type: str = "") -> bool:
    """Whether this item must be converted server-side to be displayable.

    Deliberately narrower than ``not is_web_safe``: an item whose format this
    module can't convert either (an archive, a plain-text record) is better
    served by the gallery's existing document-icon tile than by a preview
    request guaranteed to 404.

    Args:
        url: The item's URL.
        content_type: The provider-declared content type, when known.

    Returns:
        True when :func:`render_preview` is expected to be able to produce an
        image for this item.
    """
    if not url:
        return False
    declared = (content_type or "").split(";")[0].strip().lower()
    if declared:
        return declared in RENDERABLE_CONTENT_TYPES
    return _extension(url) in RENDERABLE_EXTENSIONS


def sign_source_url(url: str) -> str:
    """Signature token binding a preview request to a server-issued URL.

    Args:
        url: The absolute source URL to be previewed.

    Returns:
        The URL-safe signature to pass as the preview URL's ``sig`` param.
    """
    return Signer(salt=PREVIEW_SIGNER_SALT).signature(url)


def signature_is_valid(url: str, signature: str) -> bool:
    """Whether ``signature`` is this server's own signature for ``url``.

    Args:
        url: The source URL as received from the client.
        signature: The ``sig`` query parameter as received.

    Returns:
        True when the pair verifies.
    """
    import hmac

    return bool(signature) and hmac.compare_digest(sign_source_url(url), signature)


def remote_preview_url(url: str) -> str:
    """The signed generic-endpoint URL that renders ``url`` as a web-safe image.

    Args:
        url: An absolute http(s) source URL.

    Returns:
        A relative in-app URL for :class:`~urbanlens.dashboard.controllers.media_preview.MediaPreviewView`.
    """
    from django.urls import reverse

    return f"{reverse('media.preview')}?{urlencode({'u': url, 'sig': sign_source_url(url)})}"


def _with_preview_flag(url: str) -> str:
    """Append ``preview=1`` to an in-app proxy URL, preserving any existing query."""
    return f"{url}{'&' if '?' in url else '?'}preview=1"


def preview_thumb_url(url: str, content_type: str = "") -> str:
    """The URL a gallery tile should render for an item that isn't web-safe.

    In-app proxy routes render their own preview inline (they already hold the
    bytes); everything else goes through the signed generic endpoint.

    Args:
        url: The item's URL - relative for an in-app proxy, absolute otherwise.
        content_type: The provider-declared content type, when known.

    Returns:
        A URL that serves a browser-renderable image, or ``""`` when this item
        can't be previewed and should keep its fallback icon tile.
    """
    if not needs_server_side_preview(url, content_type):
        return ""
    if url.startswith("/"):
        return _with_preview_flag(url)
    if urlsplit(url).scheme in ("http", "https"):
        return remote_preview_url(url)
    return ""


def gallery_thumb_url(item_url: str, thumb_url: str, content_type: str = "") -> str:
    """The best ``<img src>`` for one gallery item, converting when needed.

    Resolves the three cases the gallery actually sees, in order:

    1. A web-safe thumbnail the provider already published - used as-is.
    2. A thumbnail in a format the browser can't render (a Wikimedia TIFF
       "thumbnail" that is just the original) - converted.
    3. No thumbnail at all, but a convertible full-size file (a scanned PDF
       inventory form) - the full file's first page becomes the thumbnail,
       which is the whole point: those tiles previously showed only a grey
       document icon even though the document is a photograph of the building.

    Args:
        item_url: The item's full-resolution URL.
        thumb_url: The provider's own thumbnail URL, possibly ``""``.
        content_type: The declared content type of ``item_url``, when known.
            Not applied to ``thumb_url``, which is a different file whenever
            the provider published one separately.

    Returns:
        A displayable URL, or ``""`` when nothing here is renderable and the
        caller should fall back to an icon tile.
    """
    if thumb_url:
        if is_web_safe(thumb_url):
            return thumb_url
        if preview := preview_thumb_url(thumb_url):
            return preview
        # An unrecognized thumbnail is still likelier to render than nothing -
        # providers do serve extension-less thumbnail URLs that are plain JPEG.
        return thumb_url
    return preview_thumb_url(item_url, content_type)


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

    Format detection prefers the file's own magic bytes over the declared
    content type: REData, CRIS and several archives label scanned documents
    with generic or simply wrong types, and getting this wrong means falling
    back to a broken tile rather than raising.

    Args:
        raw: The source file's bytes.
        content_type: The declared content type, used only as a hint.
        max_dimension: Longest edge of the result.

    Returns:
        ``(image_bytes, content_type)`` for the converted image, or None when
        the source couldn't be decoded as either a PDF or an image.
    """
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
        image = image.convert("RGBA" if has_alpha else "RGB")
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

#: Where a source file waits between the web process staging it and the sandbox
#: worker decoding it. Under MEDIA_ROOT because that is the one writable volume
#: both containers mount; nothing serves it, because every media URL resolves
#: through an ``Image`` row and these files have none.
PREVIEW_SOURCE_DIR = "preview_sources"

#: How long a staged source survives an un-run render before the sweep removes
#: it. Must outlive a queue backlog; anything older is an orphan whose task
#: never ran (a broker outage at enqueue time).
PREVIEW_SOURCE_MAX_AGE = 3600


def _preview_source_root() -> Path:
    """The directory staged sources live in, created if it does not exist."""
    from django.conf import settings

    root = Path(settings.MEDIA_ROOT) / PREVIEW_SOURCE_DIR
    root.mkdir(parents=True, exist_ok=True)
    return root


def stage_preview_source(digest: str, raw: bytes, content_type: str) -> dict[str, str]:
    """Write a source file where the sandbox worker can read it.

    Not through the cache, and not through the broker. The source cap is 60MB
    (:data:`MAX_PREVIEW_SOURCE_BYTES`) and both of those are the same 512MB
    Valkey that holds the Celery broker, sessions and Channels groups - one
    gallery page of large scanned PDFs would evict all of it under
    ``volatile-lru``, including (self-defeatingly) the staged sources
    themselves. The media volume is mounted by both containers and is where
    large files belong.

    Args:
        digest: A stable hash of the source URL, used as the filename.
        raw: The file's bytes.
        content_type: The provider-declared content type, passed through to the
            renderer as a hint.

    Returns:
        A small descriptor to put in the cache - filename plus content type,
        not bytes - for :func:`load_preview_source` to resolve.
    """
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
        ``(bytes, content_type)``, or None when the file is gone - swept as an
        orphan, or already consumed by an earlier render.
    """
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

    ``render_media_preview`` removes its own source, so anything left is from
    an enqueue that failed (the broker was down) - which leaves a file on the
    media volume that nothing will ever read.

    Args:
        max_age: Age in seconds past which a staged file is an orphan.

    Returns:
        How many files were removed.
    """
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
    """Queue a preview render in the sandbox worker, at most once per key.

    :func:`render_preview` reaches Pillow and poppler, so it must not run in a
    web process - see :mod:`urbanlens.dashboard.services.sandbox.guard`.

    Deliberately fire-and-forget. Waiting on the result would keep the endpoint's
    old contract ("one GET returns the preview"), but it also means every tile
    request pins a web worker for as long as the sandbox is behind - twenty tiles
    on one gallery page, times the wait, whenever ``media-worker`` is down. A
    caller that finds nothing cached serves its icon tile, exactly as it already
    does for a file that cannot be converted at all, and the tile fills in on the
    next load. Self-healing beats synchronously correct here, because the thing
    being waited for is a decorative thumbnail.

    Args:
        source_cache_key: Cache key holding what the worker needs to find the
            source - a :func:`stage_preview_source` descriptor, or (for a caller
            that already had the bytes cached) a ``(bytes, content_type)`` pair.
            Must already be populated by the caller.
        preview_cache_key: Cache key the rendered preview is written to.
        ttl: Seconds to cache a successful render.
        failure_ttl: Seconds to cache the :data:`UNPREVIEWABLE` sentinel.
    """
    from urbanlens.dashboard.services.core.celery import safely_enqueue_task
    from urbanlens.dashboard.tasks import render_media_preview

    # add() is atomic in both the Redis and locmem backends, so concurrent tile
    # requests race here rather than at the queue.
    if not cache.add(preview_cache_key, RENDER_QUEUED, RENDER_QUEUED_TTL):
        return
    if safely_enqueue_task(render_media_preview, source_cache_key, preview_cache_key, ttl, failure_ttl) is None:
        # Broker unreachable. Drop the marker so the next request retries rather
        # than waiting out RENDER_QUEUED_TTL against a queue nothing was put on.
        cache.delete(preview_cache_key)


def cached_preview(preview_cache_key: str) -> tuple[bytes, str] | None:
    """Read a previously rendered preview, treating both sentinels as "no preview".

    Args:
        preview_cache_key: The key :func:`request_sandbox_render` was given.

    Returns:
        ``(image_bytes, content_type)``, or None when the render has not
        finished, was never queued, or produced nothing. All three mean the same
        thing to a caller: serve the icon tile.
    """
    cached = cache.get(preview_cache_key)
    if cached is None or cached in (UNPREVIEWABLE, RENDER_QUEUED):
        return None
    return cached
