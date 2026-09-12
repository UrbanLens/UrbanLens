"""Turns a transient Media-gallery item into a persisted ``Image`` row."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any
from urllib.parse import urlparse

from django.core.files.base import ContentFile
import requests

from urbanlens.dashboard.models.images.model import Image, ImageSource, QuotaExemption
from urbanlens.dashboard.models.images.relevance import media_item_key
from urbanlens.dashboard.services.core.text_limits import column_max_length
from urbanlens.dashboard.services.media.images import compute_checksum
from urbanlens.dashboard.services.security.url_safety import UnsafeUrlError, fetch_public_url

if TYPE_CHECKING:
    from urbanlens.dashboard.models.location.model import Location
    from urbanlens.dashboard.models.pin.model import Pin
    from urbanlens.dashboard.models.profile.model import Profile
    from urbanlens.dashboard.models.wiki.model import Wiki

logger = logging.getLogger(__name__)

_DOWNLOAD_TIMEOUT = 15
# A Media gallery photo is a thumbnail/preview, not a multi-megapixel original -
# bound the download defensively regardless of what a provider's Content-Length claims.
_MAX_DOWNLOAD_BYTES = 20 * 1024 * 1024
_DEFAULT_FILENAME = "photo.jpg"
# Redirects are followed manually (see materialize_media_item) so each hop can
# be SSRF-validated; this bounds how many hops a hostile server can chain.
_MAX_REDIRECTS = 5
# Wikimedia (and several other public CDNs) 403 the default python-requests UA;
# match the descriptive string the Wikimedia/Wikipedia gateways already send.
_USER_AGENT = "UrbanLens/1.0 (https://github.com/urbanlens/urbanlens; jess.a.mann@gmail.com) python-requests/2.x"
_DOWNLOAD_HEADERS = {"User-Agent": _USER_AGENT}

# The Media gallery's per-provider panel key (GalleryMediaSource.key, what's actually sent as
# `source` here) doesn't always match the ImageSource value with the same real-world meaning -
# translate the ones that differ so a materialized row keeps correct attribution instead of silently
# falling back to plain ImageSource.UPLOAD (see ImageSource.valid() below).
_PANEL_KEY_TO_IMAGE_SOURCE = {
    "loc": ImageSource.LIBRARY_OF_CONGRESS,
    "cris_building": ImageSource.CRIS,
}

_CAPTION_MAX_LENGTH = column_max_length(Image, "caption")


def _truncated_caption(caption: str) -> str | None:
    """Fit a provider-supplied caption into ``Image.caption``'s column width."""
    caption = caption.strip()
    return caption[:_CAPTION_MAX_LENGTH] or None


class MaterializeError(RuntimeError):
    """Raised when a Media gallery item can't be downloaded or persisted."""


def _translated_source(source: str) -> str:
    """Translate a Media gallery panel key to its ``ImageSource`` value, falling back to ``UPLOAD``."""
    translated = _PANEL_KEY_TO_IMAGE_SOURCE.get(source, source)
    return translated if ImageSource.valid(translated) else ImageSource.UPLOAD


def find_materialized_image(location: Location, source: str, url: str, *, page_url: str = "", pin: Pin | None = None, profile: Profile | None = None) -> Image | None:
    """Look up an already-materialized ``Image`` row for a Media gallery item, without creating one.

    Args:
        location: The shared Location the item belongs to.
        source: A Media gallery panel key or ``ImageSource`` value.
        url: The item's full-resolution image URL.
        page_url: The item's page url, if the original materialize call was given one - must match to find the same row (see ``source_url`` below).
        pin: Narrows the lookup to a specific pin's own materialized copy, mirroring :func:`materialize_media_item`'s pin-scoped dedupe.
        profile: Required alongside ``pin`` for the same narrowed lookup.

    Returns:
        The matching ``Image`` row, or None if this item was never materialized."""
    source_url = page_url or url
    django_source = _translated_source(source)
    dedupe_filter: dict[str, Any] = {"location": location, "source": django_source, "source_url": source_url}
    if pin is not None:
        dedupe_filter["pin"] = pin
        dedupe_filter["profile"] = profile
    return Image.objects.filter(**dedupe_filter).first()


def _filename_from_url(url: str) -> str:
    """Best-effort filename for the downloaded content, defaulting when unclear.
    Left untruncated - ``Image.image``'s ``upload_to`` trims an overlong name to fit the field itself, so every writer doesn't need its own copy of that logic."""
    name = urlparse(url).path.rsplit("/", 1)[-1]
    return name if name and "." in name else _DEFAULT_FILENAME


def fetch_with_revalidated_redirects(
    url: str,
    *,
    max_redirects: int = _MAX_REDIRECTS,
    timeout: float = _DOWNLOAD_TIMEOUT,
    headers: dict[str, str] | None = None,
) -> requests.Response:
    """Fetch ``url`` via GET, with rebind-proof SSRF protection on every hop.

    Args:
        url: The url to fetch.
        max_redirects: Maximum redirect hops to follow before giving up.
        timeout: Per-request timeout in seconds.
        headers: Extra headers to send (e.g. a descriptive User-Agent some providers require).

    Returns:
        The final, non-redirect ``requests.Response`` - streamed (``stream=True``), not yet read.

    Raises:
        UnsafeUrlError: A hop's target failed the public-reachability check, the connection landed on an unvalidated address, a redirect response had no ``Location`` header, or the chain exceeded ``max_redirects`` hops.
        requests.RequestException: The underlying request failed."""
    return fetch_public_url(url, headers=headers, timeout=timeout, max_redirects=max_redirects)


def materialize_media_item(
    *,
    location: Location,
    profile: Profile,
    source: str,
    url: str,
    page_url: str = "",
    caption: str = "",
    wiki: Wiki | None = None,
    pin: Pin | None = None,
) -> Image:
    """Download one Media gallery item and persist it as an ``Image`` row.
    Idempotent per ``(location, source, source_url)`` - re-sending the same item (e.g. clicking "send to wiki" twice) reuses the existing row rather than downloading and storing a duplicate.

    Args:
        location: The shared Location the item belongs to.
        profile: The acting user - becomes the row's uploader and pays the storage-quota cost of the download.
        source: A Media gallery panel key or ``ImageSource`` value identifying the provider - translated via ``_PANEL_KEY_TO_IMAGE_SOURCE`` first for the handful of panels whose key doesn't already match its ``ImageSource`` value.
        url: The item's full-resolution image URL to download.
        page_url: The item's page on the provider's site, if any - stored as ``source_url`` (preferred over ``url`` so the attribution link points at a real page rather than a bare image file).
        caption: Human-readable caption, if any.
        wiki: Wiki to attach the row to, when materializing for "send to wiki".
        pin: Pin to attach the row to, when materializing for "mark relevant" - also narrows the idempotency check (see above).

    Returns:
        The persisted (or reused) ``Image`` row.

    Raises:
        MaterializeError: The download failed, or the profile's storage quota doesn't have room for it."""
    source_url = page_url or url
    item_key = media_item_key(url)
    django_source = _translated_source(source)

    dedupe_filter: dict[str, Any] = {"location": location, "source": django_source, "source_url": source_url}
    if pin is not None:
        dedupe_filter["pin"] = pin
        dedupe_filter["profile"] = profile
    existing = Image.objects.filter(**dedupe_filter).first()
    if existing:
        update_fields = []
        if wiki is not None and existing.wiki_id != wiki.pk:
            existing.wiki = wiki
            update_fields.append("wiki")
        # Backfills the (source, item_key) identity onto rows materialized before these fields
        # existed, or onto any row a dedupe hit reused without them having been set - see
        # Image.media_source_key's docstring for why this identity can't be reconstructed from
        # `source_url` alone.
        if existing.media_source_key != source or existing.media_item_key != item_key:
            existing.media_source_key = source
            existing.media_item_key = item_key
            update_fields += ["media_source_key", "media_item_key"]
        if update_fields:
            existing.save(update_fields=[*update_fields, "updated"])
        return existing

    try:
        response = fetch_with_revalidated_redirects(url, max_redirects=_MAX_REDIRECTS, timeout=_DOWNLOAD_TIMEOUT, headers=_DOWNLOAD_HEADERS)
        response.raise_for_status()
        content = response.raw.read(_MAX_DOWNLOAD_BYTES + 1, decode_content=True)
    except (requests.RequestException, OSError, UnsafeUrlError) as exc:
        logger.info("Could not download %s: %s", url, exc)
        raise MaterializeError(f"Could not download {url}.") from exc
    if len(content) > _MAX_DOWNLOAD_BYTES:
        raise MaterializeError(f"{url} is larger than the {_MAX_DOWNLOAD_BYTES // (1024 * 1024)}MB limit for Media gallery photos.")
    if not content:
        raise MaterializeError(f"{url} returned no image data.")

    file_obj = ContentFile(content, name=_filename_from_url(url))
    checksum = compute_checksum(file_obj)
    file_obj.seek(0)

    # Deliberately not quota-checked.
    # This is a cached copy of someone else's photo, kept so the gallery survives the provider's URL
    # rotting - the user who upvoted it into the cache didn't author it and isn't charged for it.
    image = Image.objects.create(
        image=file_obj,
        location=location,
        wiki=wiki,
        pin=pin,
        profile=profile,
        source=django_source,
        source_url=source_url,
        # The address actually fetched. Both are kept: the page and the file rot
        # independently, and this function was already handed both.
        source_media_url=url,
        media_source_key=source,
        media_item_key=item_key,
        caption=_truncated_caption(caption),
        checksum=checksum,
        file_size=len(content),
        quota_exempt_reason=QuotaExemption.EXTERNAL_MEDIA,
        # Provider bytes are no more trusted than a user's: quarantined on
        # create, cleared by process_image_upload once scanned and normalised.
        pending_scan=True,
    )

    # Same post-storage pipeline an ordinary upload gets.
    # Without it a materialized item kept its provider EXIF (location included), was never
    # downscaled or thumbnailed, and was served exactly as fetched, forever.
    from urbanlens.dashboard.services.core.celery import safely_enqueue_task
    from urbanlens.dashboard.tasks import process_image_upload

    safely_enqueue_task(process_image_upload, image.pk)
    return image
