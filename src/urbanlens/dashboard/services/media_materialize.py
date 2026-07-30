"""Turns a transient Media-gallery item into a persisted ``Image`` row.

The pin detail page's Media gallery (Wikimedia, Smithsonian, Yelp, Google
Images, ...) renders straight from each provider's live results (see
``services.external_data``) without persisting anything per item - that's
what keeps browsing it cheap. Two actions need a real, durable photo though:
sending an item to the community wiki, and setting it as a cover photo. Both
funnel through :func:`materialize_media_item`, which downloads the item once
and creates (or reuses) an ``Image`` row for it, counted against the acting
user's storage quota like any other upload.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING
from urllib.parse import urljoin, urlparse

from django.core.files.base import ContentFile
import requests

from urbanlens.dashboard.models.images.model import Image, ImageSource
from urbanlens.dashboard.models.images.relevance import media_item_key
from urbanlens.dashboard.services.images import compute_checksum
from urbanlens.dashboard.services.storage import quota_error_for_upload
from urbanlens.dashboard.services.url_safety import UnsafeUrlError, ensure_public_http_url

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

# The Media gallery's per-provider panel key (GalleryMediaSource.key, what's
# actually sent as `source` here) doesn't always match the ImageSource value
# with the same real-world meaning - translate the ones that differ so a
# materialized row keeps correct attribution instead of silently falling
# back to plain ImageSource.UPLOAD (see ImageSource.valid() below). Every
# panel key not listed here already equals its ImageSource value directly
# (e.g. "smithsonian", "wikimedia", "yelp", "google_images", "google_maps").
_PANEL_KEY_TO_IMAGE_SOURCE = {
    "loc": ImageSource.LIBRARY_OF_CONGRESS,
    "cris_building": ImageSource.CRIS,
}


class MaterializeError(RuntimeError):
    """Raised when a Media gallery item can't be downloaded or persisted."""


def _filename_from_url(url: str) -> str:
    """Best-effort filename for the downloaded content, defaulting when unclear."""
    name = urlparse(url).path.rsplit("/", 1)[-1]
    return name[:100] if name and "." in name else _DEFAULT_FILENAME


def fetch_with_revalidated_redirects(
    url: str,
    *,
    max_redirects: int = _MAX_REDIRECTS,
    timeout: float = _DOWNLOAD_TIMEOUT,
    headers: dict[str, str] | None = None,
) -> requests.Response:
    """Fetch ``url`` via GET, manually following redirects with per-hop SSRF re-validation.

    Shared by every service that downloads a user- or provider-supplied url
    from the server (this module, ``services.pin_suggestions``'s
    ``_download_photo_bytes``, and ``services.ai.link_extraction``'s
    ``fetch_page_text`` - previously each had its own copy of this loop).

    ``ensure_public_http_url`` closes the DNS-at-check-time gap but not a
    rebind that happens *between* a check and the actual connection - each
    hop here is fetched with ``allow_redirects=False`` and re-validated
    immediately before connecting (including every redirect hop) to keep
    that window as small as possible; see that function's docstring.

    Args:
        url: The url to fetch. Already validated once by the caller, if
            applicable (e.g. at submission time) - this re-validates it
            regardless, since time may have passed since then.
        max_redirects: Maximum redirect hops to follow before giving up.
        timeout: Per-request timeout in seconds.
        headers: Extra headers to send (e.g. a descriptive User-Agent some
            providers require).

    Returns:
        The final, non-redirect ``requests.Response`` - streamed
        (``stream=True``), not yet read.

    Raises:
        UnsafeUrlError: A hop's target failed the public-reachability check,
            a redirect response had no ``Location`` header, or the chain
            exceeded ``max_redirects`` hops.
        requests.RequestException: The underlying request failed.
    """
    fetch_url = url
    for _hop in range(max_redirects + 1):
        fetch_url = ensure_public_http_url(fetch_url)
        # ensure_public_http_url (above) re-validates this exact hop - literal
        # IP and resolved hostname - immediately before the connection below;
        # CodeQL doesn't model it as a sanitizer.
        response = requests.get(  # lgtm[py/full-ssrf]
            fetch_url,
            timeout=timeout,
            stream=True,
            allow_redirects=False,
            headers=headers,
        )
        if response.is_redirect:
            redirect_target = response.headers.get("Location")
            response.close()
            if not redirect_target:
                raise UnsafeUrlError(f"{url} redirected with no target.")
            fetch_url = urljoin(fetch_url, redirect_target)
            continue
        return response
    raise UnsafeUrlError(f"{url} redirects too many times.")


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

    Idempotent per ``(location, source, source_url)`` - re-sending the same
    item (e.g. clicking "send to wiki" twice) reuses the existing row rather
    than downloading and storing a duplicate. When ``pin`` is given (marking
    a Media item "relevant" on a specific pin, a personal "save this for me"
    action - see ``PinController.media_relevance``), the dedup check is
    additionally scoped to ``(pin, profile)`` so it never reuses a row
    another profile already materialized for the same external item via a
    *different* action (e.g. sending it to the shared wiki) - unlike the
    wiki-send path, this one must always end up owned by the marking profile.

    Args:
        location: The shared Location the item belongs to.
        profile: The acting user - becomes the row's uploader and pays the
            storage-quota cost of the download.
        source: A Media gallery panel key or ``ImageSource`` value
            identifying the provider - translated via
            ``_PANEL_KEY_TO_IMAGE_SOURCE`` first for the handful of panels
            whose key doesn't already match its ``ImageSource`` value.
        url: The item's full-resolution image URL to download.
        page_url: The item's page on the provider's site, if any - stored as
            ``source_url`` (preferred over ``url`` so the attribution link
            points at a real page rather than a bare image file).
        caption: Human-readable caption, if any.
        wiki: Wiki to attach the row to, when materializing for "send to wiki".
        pin: Pin to attach the row to, when materializing for "mark relevant"
            - also narrows the idempotency check (see above).

    Returns:
        The persisted (or reused) ``Image`` row.

    Raises:
        MaterializeError: The download failed, or the profile's storage quota
            doesn't have room for it.
    """
    source_url = page_url or url
    item_key = media_item_key(url)
    # Translated *before* the dedupe check - existing rows are persisted with
    # `source=django_source` (see the `Image.objects.create` call below), so
    # filtering on the untranslated panel key here would never match a
    # previously materialized "loc"/"cris_building" item and re-download +
    # duplicate it on every subsequent vote.
    translated_source = _PANEL_KEY_TO_IMAGE_SOURCE.get(source, source)
    django_source = translated_source if ImageSource.valid(translated_source) else ImageSource.UPLOAD

    dedupe_filter = {"location": location, "source": django_source, "source_url": source_url}
    if pin is not None:
        dedupe_filter["pin"] = pin
        dedupe_filter["profile"] = profile
    existing = Image.objects.filter(**dedupe_filter).first()
    if existing:
        update_fields = []
        if wiki is not None and existing.wiki_id != wiki.pk:
            existing.wiki = wiki
            update_fields.append("wiki")
        # Backfills the (source, item_key) identity onto rows materialized
        # before these fields existed, or onto any row a dedupe hit reused
        # without them having been set - see Image.media_source_key's
        # docstring for why this identity can't be reconstructed from
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
        raise MaterializeError(f"Could not download {url}: {exc}") from exc
    if len(content) > _MAX_DOWNLOAD_BYTES:
        raise MaterializeError(f"{url} is larger than the {_MAX_DOWNLOAD_BYTES // (1024 * 1024)}MB limit for Media gallery photos.")
    if not content:
        raise MaterializeError(f"{url} returned no image data.")

    quota_error = quota_error_for_upload(profile, len(content))
    if quota_error:
        raise MaterializeError(quota_error)

    file_obj = ContentFile(content, name=_filename_from_url(url))
    checksum = compute_checksum(file_obj)
    file_obj.seek(0)

    return Image.objects.create(
        image=file_obj,
        location=location,
        wiki=wiki,
        pin=pin,
        profile=profile,
        source=django_source,
        source_url=source_url,
        media_source_key=source,
        media_item_key=item_key,
        caption=caption.strip() or None,
        checksum=checksum,
        file_size=len(content),
    )
