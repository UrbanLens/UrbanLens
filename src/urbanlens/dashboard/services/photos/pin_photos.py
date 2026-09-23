"""Everything a Private Pin page's Photos tab lists: the owner's own photos and the public-source photos its Media panel shows."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import timedelta
import logging
from typing import TYPE_CHECKING, Any

from django.utils import timezone

from urbanlens.dashboard.models.site_settings import SiteSettings
from urbanlens.dashboard.services.pins.external_data import GalleryMediaSource, gate_allows, get_panel_source, panel_visible_to, schedule_panel_fetch

if TYPE_CHECKING:
    from collections.abc import Sequence

    from django.contrib.auth.models import AbstractBaseUser, AnonymousUser

    from urbanlens.dashboard.models.pin.model import Pin
    from urbanlens.dashboard.models.profile.model import Profile

logger = logging.getLogger(__name__)

#: The Media panel's external providers, in its loader order (``pages/location/index.html``).
PIN_MEDIA_GALLERY_SOURCES: tuple[str, ...] = (
    "smithsonian",
    "wikimedia",
    "wikipedia_media",
    "loc",
    "internet_archive",
    "digital_commonwealth",
    "yelp",
    "google_images",
    "searxng_images",
    "google_maps",
    "loopnet",
    "cris_building",
)

#: Upper bound on one page of any Photos-tab grid.
MAX_PAGE_SIZE = 100


@dataclass(frozen=True, slots=True)
class ExternalPhoto:
    """One public-source photo, attributed.

    Attributes:
        source: The Media panel's provider key, e.g. ``"wikimedia"``.
        source_name: The provider's display name.
        key: ``media_item_key(url)``, the item's identity across the Media panel, relevance marks and materialized copies.
        url: The full-size image: a local copy when one exists, else the provider's own.
        thumb_url: A URL a browser can render, converted through the preview route when needed.
        caption: The provider's caption or title.
        author: Who to credit for the photo itself.
        page_url: The item's page on the provider's site.
        relevant: The viewer's relevance mark, or None.
    """

    source: str
    source_name: str
    key: str
    url: str
    thumb_url: str
    caption: str
    author: str
    page_url: str
    relevant: bool | None

    def to_json(self) -> dict[str, Any]:
        """The Photos tab's client payload for this photo."""
        return {
            "origin": "external",
            "is_mine": False,
            "source": self.source,
            "source_name": self.source_name,
            "key": self.key,
            "url": self.url,
            "thumb_url": self.thumb_url,
            "caption": self.caption,
            "author": self.author,
            "page_url": self.page_url,
            "relevant": self.relevant,
        }


@dataclass(slots=True)
class ExternalPhotoListing:
    """Every public-source photo for one pin, and the providers yet to answer.

    Attributes:
        photos: Displayable photos, deduplicated, in provider order.
        pending: Provider keys whose fetch is in flight, so the list may still grow.
    """

    photos: list[ExternalPhoto] = field(default_factory=list)
    pending: list[str] = field(default_factory=list)


def _visible_sources(pin: Pin, user: AbstractBaseUser | AnonymousUser) -> list[GalleryMediaSource]:
    sources = []
    for key in PIN_MEDIA_GALLERY_SOURCES:
        panel = get_panel_source(key)
        if isinstance(panel, GalleryMediaSource) and panel_visible_to(user, panel):
            sources.append(panel)
    return sources


def _own_copy_keys(pins: Sequence[Pin], profile: Profile, keys: set[str]) -> set[str]:
    """Media keys already saved as the viewer's own photos on any of *pins*, which list them there instead."""
    from urbanlens.dashboard.models.images.model import Image

    if not keys:
        return set()
    return set(
        Image.objects.filter(pin__in=pins, profile=profile, media_item_key__in=keys, media_source_key__in=PIN_MEDIA_GALLERY_SOURCES).values_list("media_item_key", flat=True),
    )


def external_photos_for_pin(pin: Pin, profile: Profile, user: AbstractBaseUser | AnonymousUser, *, own_pins: Sequence[Pin] = ()) -> ExternalPhotoListing:
    """The public-source photos the Media panel shows for *pin*, as one attributed list.

    Mirrors ``PinController.media_provider`` per provider: the same visibility and gate checks, the same
    cached rows, local copies and thumbnail conversion. Differs where a single list needs it: items the
    viewer marked not relevant and items with nothing to show are left out, a photo two providers both
    return appears once, and one the viewer already saved to their own pins is left to their own photos.
    A provider with no answer yet is scheduled and reported as pending.

    Args:
        pin: The pin whose place to list.
        profile: The viewing profile, the pin's owner.
        user: The viewing user, for feature-gated providers.
        own_pins: Pins whose own photos the Photos tab also lists; defaults to *pin*.

    Returns:
        The listing.
    """
    from urbanlens.dashboard.models.cache.location_cache import LocationCache
    from urbanlens.dashboard.models.images.relevance import MediaRelevance, media_item_key
    from urbanlens.dashboard.services.media.media_relevance import local_images_for_gallery_items
    from urbanlens.dashboard.services.media.previews import gallery_thumb_url

    listing = ExternalPhotoListing()
    location = pin.location
    if location is None:
        return listing

    sources = _visible_sources(pin, user)
    cutoff = timezone.now() - timedelta(days=SiteSettings.get_current().external_data_cache_days)
    fresh = LocationCache.objects.filter(location=location, source__in=[source.cache_source for source in sources], updated__gte=cutoff)
    rows = {row.source: row.data or {} for row in fresh}
    relevance: dict[tuple[str, str], bool | None] = {
        (source, key): is_relevant for source, key, is_relevant in MediaRelevance.objects.filter(profile=profile, location=location, source__in=[source.key for source in sources]).values_list("source", "item_key", "is_relevant")
    }

    candidates: list[ExternalPhoto] = []
    for source in sources:
        data = rows.get(source.cache_source)
        if data is not None and not source.media_is_ready(data):
            data = None
        if data is None:
            if gate_allows(source, pin) and schedule_panel_fetch(source.key, pin):
                listing.pending.append(source.key)
            continue
        try:
            items = source.media_items(data)
        except Exception:
            logger.exception("Media source %s could not read its cached row for location %s", source.key, location.pk)
            continue
        if not items or not gate_allows(source, pin):
            continue
        local = local_images_for_gallery_items(location, source.key, [item.url for item in items])
        for item in items:
            key = media_item_key(item.url)
            mark = relevance.get((source.key, key))
            if mark is False:
                continue
            local_url = local[item.url].file_url if item.url in local else ""
            thumb = local_url or gallery_thumb_url(item.url, item.thumb_url, item.content_type)
            if not thumb:
                continue
            candidates.append(
                ExternalPhoto(
                    source=source.key,
                    source_name=item.source,
                    key=key,
                    url=local_url or item.url,
                    thumb_url=thumb,
                    caption=item.caption or "",
                    author=item.author or "",
                    page_url=item.page_url or item.url,
                    relevant=mark,
                ),
            )

    saved = _own_copy_keys(own_pins or [pin], profile, {photo.key for photo in candidates})
    seen: set[str] = set()
    for photo in candidates:
        if photo.key in seen or photo.key in saved:
            continue
        seen.add(photo.key)
        listing.photos.append(photo)
    return listing
