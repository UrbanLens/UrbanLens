"""Background-enrichment sources that cache small, wiki-attached location photos.

Three sources, contributed to the hourly scheduled-enrichment cycle
(``services.locations.enrichment.run_enrichment_cycle``) by ``plugins.builtin.google_places``
(:class:`PlacePhotoEnrichmentSource`) and ``plugins.builtin.google_maps``
(:class:`StreetViewEnrichmentSource`, :class:`SatelliteEnrichmentSource`):

- Google Places business photos.
- A static Street View image.
- A static satellite image.

Each is persisted as an ordinary, small (resized + WebP) ``Image`` row, attached to the
location's Wiki (lazily created if absent, exactly like the "share to wiki" action any user can
already take) rather than a pin or profile - these are site-owned, publicly-sourced reference
imagery, not anyone's private upload, so there is no profile to charge storage quota against and
no privacy concern in making them wiki-visible. Attaching to the wiki is deliberate: it means
these photos become eligible for SpotGuessr's Photos mode (``services.spotguessr.photos``)
through that mode's existing, unmodified ``Image.wiki_id`` gate - no changes needed there at all.

Each source tracks its own "attempted, even if nothing was found" completion via a dedicated
``LocationCache`` marker row, mirroring ``services.locations.enrichment.AddressEnrichmentSource`` - so a
location with no coverage/photos is tried exactly once, never retried forever.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, ClassVar

from django.db.models import Exists, OuterRef, Q
import requests

from urbanlens.dashboard.models.images.model import ImageSource
from urbanlens.dashboard.services.core.gateway import GatewayRequestError
from urbanlens.dashboard.services.locations.enrichment import EnrichmentSource

if TYPE_CHECKING:
    from urbanlens.dashboard.models.images.model import Image
    from urbanlens.dashboard.models.location.model import Location

logger = logging.getLogger(__name__)

#: Up to this many Places business photos are downloaded per location - bounds both API
#: spend and how much a single location can grow the enrichment run's storage footprint.
MAX_PLACE_PHOTOS = 3

#: Longest edge, in pixels, Places photos are downscaled to - a game/gallery thumbnail, not a
#: full-resolution original nobody asked to keep.
_PLACE_PHOTO_MAX_DIMENSION = 1024

#: Street View Static / Static Maps responses already come back ~640x400 - this mostly just
#: forces the WebP re-encode rather than actually shrinking anything.
_STATIC_IMAGE_MAX_DIMENSION = 800

#: Shared with plugins.builtin.google_places.GoogleMapsPhotosPanelSource.cache_source - reusing
#: the same LocationCache key lets this source skip the API call entirely when a user's own
#: pin-detail Media gallery view already warmed it.
_PLACE_PHOTO_LIST_CACHE_SOURCE = "google_maps_photos"


def _save_enriched_image(location: Location, content: bytes, *, source: str, source_url: str = "", caption: str = "", max_dimension: int) -> Image:
    """Persist downloaded bytes as a wiki-attached, profile-less Image row, resized to WebP.

    Args:
        location: The shared Location this photo belongs to.
        content: Raw downloaded image bytes.
        source: The ``ImageSource`` value to record.
        source_url: Attribution link, if any (left blank when there's no real page to link to).
        caption: Human-readable caption, if any.
        max_dimension: Longest-edge cap passed to ``downscale_stored_image``.

    Returns:
        The persisted Image row (already resized/WebP-converted where possible).
    """
    from django.core.files.base import ContentFile

    from urbanlens.dashboard.models.images.model import Image
    from urbanlens.dashboard.models.wiki.model import Wiki
    from urbanlens.dashboard.services.media.images import compute_checksum, downscale_stored_image

    # Deliberate, not a "lazy side effect of viewing/editing content" (the case
    # Wiki.objects.get_or_create_for_location's docstring warns against) - making this photo
    # wiki-visible (and thereby SpotGuessr-eligible) is this function's entire purpose.
    wiki, _wiki_created = Wiki.objects.get_or_create_for_location(location)

    file_obj = ContentFile(content, name="photo.jpg")
    checksum = compute_checksum(file_obj)
    file_obj.seek(0)

    image = Image.objects.create(
        image=file_obj,
        location=location,
        wiki=wiki,
        source=source,
        source_url=source_url,
        caption=caption.strip() or None,
        checksum=checksum,
        file_size=len(content),
    )
    try:
        new_size = downscale_stored_image(image, max_dimension=max_dimension, convert_webp=True)
    except (OSError, ValueError) as exc:
        logger.warning("Downscaling failed for enriched image %s: %s", image.pk, exc, exc_info=True)
        return image
    if new_size is not None:
        image.file_size = new_size
        image.save(update_fields=["image", "file_size", "updated"])
    return image


class _BackfillMarkerSource(EnrichmentSource):
    """Shared "attempted once, ever" completion tracking via a LocationCache marker row.

    Same contract as ``services.locations.enrichment.AddressEnrichmentSource``: the marker is written
    regardless of whether anything was actually found, so a location that turns out to have no
    coverage/photos isn't retried every cycle forever.
    """

    #: LocationCache source recording that a backfill attempt happened.
    marker_source: ClassVar[str] = ""

    def missing_filter(self) -> Q:
        """Locations with no prior backfill attempt for this source."""
        from urbanlens.dashboard.models.cache.location_cache import LocationCache

        attempted = LocationCache.objects.filter(location=OuterRef("pk"), source=self.marker_source)
        return ~Q(Exists(attempted))


class PlacePhotoEnrichmentSource(_BackfillMarkerSource):
    """Backfills Google Places business photos, wiki-attached and resized to WebP."""

    key: ClassVar[str] = "place_photos"
    verbose_name: ClassVar[str] = "Google Places photos"
    calls_per_item: ClassVar[int] = 5
    marker_source: ClassVar[str] = "place_photo_backfill"

    @property
    def service_keys(self) -> tuple[str, ...]:
        """Whichever of REData/Google Places ``places_resolution`` would actually dispatch to.

        Not a fixed ``ClassVar`` like most sources: which service this source's calls are
        actually billed/rate-limited against depends on runtime REData configuration, exactly
        like ``services.apis.locations.places_resolution``'s own internal dispatch. Budgeting
        against the *other*, unused path (e.g. a REData-only install's disabled/never-touched
        ``google_places`` service row) would wrongly throttle or skip this source.
        """
        if _redata_configured():
            return ("redata_places",)
        return ("google_places",)

    def gate(self) -> bool:
        """Requires either REData or the unrestricted Google API key."""
        from urbanlens.UrbanLens.settings.app import settings as app_settings

        return bool(app_settings.google_unrestricted_api_key) or _redata_configured()

    def enrich(self, location: Location) -> bool:
        """Find and download up to :data:`MAX_PLACE_PHOTOS` business photos for ``location``."""
        from urbanlens.dashboard.models.cache.location_cache import LocationCache
        from urbanlens.dashboard.services.apis.locations import places_resolution
        from urbanlens.dashboard.services.apis.locations.places_resolution import PhotoNotFoundError
        from urbanlens.UrbanLens.settings.app import settings as app_settings

        api_key = app_settings.google_unrestricted_api_key or ""
        lat, lng = float(location.latitude), float(location.longitude)

        cached = LocationCache.get_fresh(location, _PLACE_PHOTO_LIST_CACHE_SOURCE)
        if cached is not None:
            place_id = (cached.data or {}).get("place_id")
            photo_names: list[str] = (cached.data or {}).get("photo_names") or []
        else:
            place_id, photo_names = places_resolution.find_nearest_place_photos(lat, lng, api_key=api_key)
            LocationCache.set(location, _PLACE_PHOTO_LIST_CACHE_SOURCE, {"place_id": place_id, "photo_names": photo_names}, query_key=f"{lat},{lng}")

        page_url = f"https://www.google.com/maps/place/?q=place_id:{place_id}" if place_id else ""

        created = 0
        for photo_name in photo_names[:MAX_PLACE_PHOTOS]:
            try:
                content, _content_type = places_resolution.download_photo(photo_name, api_key=api_key)
            except (PhotoNotFoundError, GatewayRequestError, requests.exceptions.RequestException) as exc:
                logger.info("Place photo %s unavailable for location=%s: %s", photo_name, location.pk, exc)
                continue
            _save_enriched_image(location, content, source=ImageSource.GOOGLE_MAPS, source_url=page_url, max_dimension=_PLACE_PHOTO_MAX_DIMENSION)
            created += 1

        LocationCache.set(location, self.marker_source, {"created": created, "found": len(photo_names)})
        return True


class StreetViewEnrichmentSource(_BackfillMarkerSource):
    """Backfills a single static Street View image, wiki-attached and resized to WebP."""

    key: ClassVar[str] = "street_view_photo"
    verbose_name: ClassVar[str] = "Street View photo"
    service_keys: ClassVar[tuple[str, ...]] = ("google_maps",)
    marker_source: ClassVar[str] = "street_view_backfill"

    def gate(self) -> bool:
        """Requires the unrestricted Google API key."""
        from urbanlens.UrbanLens.settings.app import settings as app_settings

        return bool(app_settings.google_unrestricted_api_key)

    def enrich(self, location: Location) -> bool:
        """Fetch and store one Street View Static image for ``location``, if coverage exists."""
        from urbanlens.dashboard.models.cache.location_cache import LocationCache
        from urbanlens.dashboard.services.apis.locations.google.maps import GoogleMapsGateway
        from urbanlens.UrbanLens.settings.app import settings as app_settings

        gateway = GoogleMapsGateway(api_key=app_settings.google_unrestricted_api_key or "")
        found = False
        try:
            content, _capture_date = gateway.get_street_view_single(float(location.latitude), float(location.longitude))
        except (ValueError, requests.exceptions.RequestException) as exc:
            logger.info("Street View unavailable for location=%s: %s", location.pk, exc)
        else:
            found = True
            _save_enriched_image(location, content, source=ImageSource.GOOGLE_STREET_VIEW, caption="Street View", max_dimension=_STATIC_IMAGE_MAX_DIMENSION)

        LocationCache.set(location, self.marker_source, {"found": found})
        return True


class SatelliteEnrichmentSource(_BackfillMarkerSource):
    """Backfills a single static satellite image, wiki-attached and resized to WebP."""

    key: ClassVar[str] = "satellite_photo"
    verbose_name: ClassVar[str] = "Satellite photo"
    service_keys: ClassVar[tuple[str, ...]] = ("google_maps",)
    marker_source: ClassVar[str] = "satellite_backfill"

    def gate(self) -> bool:
        """Requires the unrestricted Google API key."""
        from urbanlens.UrbanLens.settings.app import settings as app_settings

        return bool(app_settings.google_unrestricted_api_key)

    def enrich(self, location: Location) -> bool:
        """Fetch and store one satellite image for ``location``, if the request succeeds."""
        from urbanlens.dashboard.models.cache.location_cache import LocationCache
        from urbanlens.dashboard.services.apis.locations.google.maps import GoogleMapsGateway
        from urbanlens.UrbanLens.settings.app import settings as app_settings

        gateway = GoogleMapsGateway(api_key=app_settings.google_unrestricted_api_key or "")
        content = gateway.get_satellite_image_bytes(float(location.latitude), float(location.longitude))
        if content:
            _save_enriched_image(location, content, source=ImageSource.GOOGLE_SATELLITE, caption="Satellite view", max_dimension=_STATIC_IMAGE_MAX_DIMENSION)

        LocationCache.set(location, self.marker_source, {"found": bool(content)})
        return True


def _redata_configured() -> bool:
    """Whether REData is configured, mirroring ``places_resolution._redata_configured``."""
    from urbanlens.UrbanLens.settings.app import settings as app_settings

    return bool(app_settings.redata_api_url and app_settings.redata_api_key)
