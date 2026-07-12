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
from urllib.parse import urlparse

from django.core.files.base import ContentFile
import requests

from urbanlens.dashboard.models.images.model import Image, ImageSource
from urbanlens.dashboard.services.images import compute_checksum
from urbanlens.dashboard.services.storage import quota_error_for_upload

if TYPE_CHECKING:
    from urbanlens.dashboard.models.location.model import Location
    from urbanlens.dashboard.models.profile.model import Profile
    from urbanlens.dashboard.models.wiki.model import Wiki

logger = logging.getLogger(__name__)

_DOWNLOAD_TIMEOUT = 15
# A Media gallery photo is a thumbnail/preview, not a multi-megapixel original -
# bound the download defensively regardless of what a provider's Content-Length claims.
_MAX_DOWNLOAD_BYTES = 20 * 1024 * 1024
_DEFAULT_FILENAME = "photo.jpg"


class MaterializeError(RuntimeError):
    """Raised when a Media gallery item can't be downloaded or persisted."""


def _filename_from_url(url: str) -> str:
    """Best-effort filename for the downloaded content, defaulting when unclear."""
    name = urlparse(url).path.rsplit("/", 1)[-1]
    return name[:100] if name and "." in name else _DEFAULT_FILENAME


def materialize_media_item(*, location: Location, profile: Profile, source: str, url: str, page_url: str = "", caption: str = "", wiki: Wiki | None = None) -> Image:
    """Download one Media gallery item and persist it as an ``Image`` row.

    Idempotent per ``(location, source, source_url)``: re-sending the same
    item (e.g. clicking "send to wiki" twice) reuses the existing row rather
    than downloading and storing a duplicate.

    Args:
        location: The shared Location the item belongs to.
        profile: The acting user - becomes the row's uploader and pays the
            storage-quota cost of the download.
        source: An ``ImageSource`` value identifying the provider.
        url: The item's full-resolution image URL to download.
        page_url: The item's page on the provider's site, if any - stored as
            ``source_url`` (preferred over ``url`` so the attribution link
            points at a real page rather than a bare image file).
        caption: Human-readable caption, if any.
        wiki: Wiki to attach the row to, when materializing for "send to wiki".

    Returns:
        The persisted (or reused) ``Image`` row.

    Raises:
        MaterializeError: The download failed, or the profile's storage quota
            doesn't have room for it.
    """
    source_url = page_url or url
    existing = Image.objects.filter(location=location, source=source, source_url=source_url).first()
    if existing:
        if wiki is not None and existing.wiki_id != wiki.pk:
            existing.wiki = wiki
            existing.save(update_fields=["wiki", "updated"])
        return existing

    try:
        response = requests.get(url, timeout=_DOWNLOAD_TIMEOUT, stream=True)
        response.raise_for_status()
        content = response.raw.read(_MAX_DOWNLOAD_BYTES + 1, decode_content=True)
    except (requests.RequestException, OSError) as exc:
        raise MaterializeError(f"Could not download {url}: {exc}") from exc
    if len(content) > _MAX_DOWNLOAD_BYTES:
        raise MaterializeError(f"{url} is larger than the {_MAX_DOWNLOAD_BYTES // (1024 * 1024)}MB limit for Media gallery photos.")
    if not content:
        raise MaterializeError(f"{url} returned no image data.")

    quota_error = quota_error_for_upload(profile, len(content))
    if quota_error:
        raise MaterializeError(quota_error)

    django_source = source if ImageSource.valid(source) else ImageSource.UPLOAD
    file_obj = ContentFile(content, name=_filename_from_url(url))
    checksum = compute_checksum(file_obj)
    file_obj.seek(0)

    return Image.objects.create(
        image=file_obj,
        location=location,
        wiki=wiki,
        profile=profile,
        source=django_source,
        source_url=source_url,
        caption=caption.strip() or None,
        checksum=checksum,
        file_size=len(content),
    )
