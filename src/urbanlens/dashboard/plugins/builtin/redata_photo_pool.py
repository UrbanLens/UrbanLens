"""Nearby photos plugin: members' photos of the places around a pin, found through REData's photo relevance index.

This site submits every located photo to REData for relevance scoring (``services.photos.redata_relevance``), so
REData can say which of them belong to the places near a point - matched on each photo's place, not where the camera
stood - and to the parcel the pin is on, with a fresh relevance score for each. The photos themselves never leave this
site: REData returns their ids, and only those the viewer may already see elsewhere are shown.

Photos of this pin's own place are left out: the owner's are on the pin already, and the shared ones on its wiki.
"""

from __future__ import annotations

from dataclasses import asdict
import logging
from typing import TYPE_CHECKING, Any, ClassVar
import uuid

from urbanlens.dashboard.plugins.base import UrbanLensPlugin
from urbanlens.dashboard.services.apis.assets.base import MediaItem
from urbanlens.dashboard.services.apis.locations.redata_context_gateway import redata_configured
from urbanlens.dashboard.services.pins.external_data import GalleryMediaSource

if TYPE_CHECKING:
    from urbanlens.dashboard.models.location.model import Location
    from urbanlens.dashboard.models.pin.model import Pin
    from urbanlens.dashboard.models.profile.model import Profile
    from urbanlens.dashboard.services.pins.external_data import PanelSource

logger = logging.getLogger(__name__)

#: Most photos one tab shows; REData answers at most 100 per call.
_MAX_PHOTOS = 60


def _parcel_uuid(location: Location) -> str:
    """The parcel REData resolved for ``location``, from the Property Records card's cached record; ``""`` when none."""
    from urbanlens.dashboard.models.cache.location_cache import LocationCache
    from urbanlens.dashboard.plugins.builtin.property_records import PropertyRecordsPanelSource

    row = LocationCache.get_fresh(location, PropertyRecordsPanelSource.cache_source)
    data = row.data if row is not None and isinstance(row.data, dict) else {}
    return str(data.get("uuid") or "") if data.get("available") else ""


def _merged(nearby: list[dict[str, Any]], on_parcel: list[dict[str, Any]], parcel_uuid: str) -> list[dict[str, Any]]:
    """One entry per photo, those on the pin's parcel first, then by REData's confidence."""
    merged: dict[str, dict[str, Any]] = {}
    for row in [*on_parcel, *nearby]:
        photo_id = str(row.get("photo_id") or "")
        confidence = row.get("confidence")
        entry = merged.setdefault(photo_id, {"photo_id": photo_id, "confidence": None, "on_parcel": False})
        if isinstance(confidence, int | float) and not isinstance(confidence, bool) and (entry["confidence"] is None or confidence > entry["confidence"]):
            entry["confidence"] = float(confidence)
        if parcel_uuid and str(row.get("parcel_uuid") or "") == parcel_uuid:
            entry["on_parcel"] = True
    return sorted(merged.values(), key=lambda entry: (not entry["on_parcel"], -(entry["confidence"] if entry["confidence"] is not None else -1.0)))


class NearbyPhotosSource(GalleryMediaSource):
    """Members' photos of the places near the pin that the viewer may see, as a Media-gallery tab."""

    key = "redata_photo_pool"
    cache_source = "redata_photo_pool"
    icon = "photo_camera"
    title = "Nearby Photos"
    members_media: ClassVar[bool] = True

    def gate(self, pin: Pin) -> bool:
        """Requires coordinates and REData."""
        return bool(pin.effective_latitude and pin.effective_longitude) and redata_configured()

    def fetch(self, pin: Pin) -> None:
        """Cache which of this site's photos REData places near the pin, and on its parcel, with their scores.

        The parcel's photos are asked for only when the Property Records card has already resolved the parcel, and
        are a bonus: a parcel REData does not hold, or a failure asking, leaves the nearby photos to stand alone.
        """
        from urbanlens.dashboard.models.cache.location_cache import UNANSWERED_SOURCES_KEY, LocationCache
        from urbanlens.dashboard.services.apis.photos.redata_photos_gateway import RedataPhotosGateway
        from urbanlens.dashboard.services.core.gateway import GatewayRequestError, is_source_outage
        from urbanlens.dashboard.services.locations.redata_point_data import point_key

        latitude = float(pin.effective_latitude or 0)
        longitude = float(pin.effective_longitude or 0)
        gateway = RedataPhotosGateway()
        nearby = gateway.lookup_near(latitude, longitude)
        parcel_uuid = _parcel_uuid(pin.location) if pin.location is not None else ""
        on_parcel: list[dict[str, Any]] = []
        unanswered: list[str] = []
        if parcel_uuid:
            try:
                on_parcel = gateway.photos_for_parcel(parcel_uuid)
            except GatewayRequestError as exc:  # outage-cache-ok: the nearby photos are kept, marked partial, for the hour
                logger.info("REData parcel photos unavailable for location %s: %s", pin.location_id, exc)
                if is_source_outage(exc):
                    unanswered.append("parcel_photos")
        data: dict[str, Any] = {"photos": _merged(nearby, on_parcel, parcel_uuid)}
        if unanswered:
            data[UNANSWERED_SOURCES_KEY] = unanswered
        LocationCache.set(pin.location, self.cache_source, data, query_key=point_key(latitude, longitude))

    def for_viewer(self, data: dict, viewer: Profile, location: Location) -> dict:
        """The cached photos ``viewer`` may see, as tiles: their own, or ones shared into a wiki they can reach.

        Photos of ``location`` itself are left out - filed there, or under its pin or wiki - and so is a photo shown to
        the viewer only because a direct message or safety check-in named them: that consent does not extend to a
        gallery, so another member's photo must sit in a wiki the viewer reaches, whatever else it is filed under.
        """
        from django.db.models import Q

        from urbanlens.dashboard.models.images.model import Image
        from urbanlens.dashboard.services.wiki.wiki_access import visible_wiki_locations

        ranked: dict[uuid.UUID, int] = {}
        for index, entry in enumerate((data or {}).get("photos") or []):
            try:
                ranked.setdefault(uuid.UUID(str(entry.get("photo_id") or "")), index)
            except (AttributeError, ValueError):
                continue
        if not ranked:
            return {"items": []}
        images = (
            Image.objects.filter(uuid__in=list(ranked))
            .exclude(location_id=location.pk)
            .exclude(pin__location_id=location.pk)
            .exclude(wiki__location_id=location.pk)
            .filter(Q(profile=viewer) | Q(wiki__location_id__in=visible_wiki_locations(viewer)))
            .photos()
            .servable()
            .exclude(image="")
            .visible_to(viewer)
        )
        ordered = sorted(images, key=lambda image: ranked[image.uuid])[:_MAX_PHOTOS]
        items = [
            MediaItem(
                url=image.display_url,
                thumb_url=image.thumb_url,
                caption=image.display_caption,
                source="Your photo" if image.profile_id == viewer.pk else "Member photo",
                author=image.author or "",
                # Only the viewer's own: another member's map_hidden is not this tab's to override, and no tile is placed.
                latitude=float(image.latitude) if image.latitude is not None and image.profile_id == viewer.pk else None,
                longitude=float(image.longitude) if image.longitude is not None and image.profile_id == viewer.pk else None,
            )
            for image in ordered
            if image.display_url
        ]
        return {"items": [asdict(item) for item in items]}

    def media_items(self, data: dict) -> list[MediaItem]:
        """The tiles :meth:`for_viewer` resolved; a cached row names only photo ids, so it yields none by itself."""
        return [MediaItem(**item) for item in (data or {}).get("items") or [] if isinstance(item, dict)]


class NearbyPhotosPlugin(UrbanLensPlugin):
    """Members' photos of the places around a pin, found through REData's photo relevance index."""

    name: ClassVar[str] = "redata_photo_pool"
    verbose_name: ClassVar[str] = "Nearby Photos"
    description: ClassVar[str] = (
        "Adds a Nearby Photos tab to the Media gallery: photos of the places around the pin, and of its parcel, that the viewer may already see - their own, or ones shared to a wiki they can reach - ranked by REData's relevance score."
    )
    author: ClassVar[str] = "UrbanLens"

    def get_panel_sources(self) -> list[PanelSource]:
        """Contribute the Nearby Photos gallery source."""
        return [NearbyPhotosSource()]
