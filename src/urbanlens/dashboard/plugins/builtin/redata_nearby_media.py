"""Nearby media plugin: the photographs, videos and webcams REData finds near a pin, as a Media-gallery tab.

One ``media/lookup`` answer per point feeds this tab and the Aerial & Drone tab (``redata_aerial_media``), which takes
the aerial rows this one leaves out; the street-level networks' rows belong to the Street-level tab
(``redata_street_level``), which shows every dated capture rather than the newest.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, ClassVar
import uuid

from urbanlens.dashboard.plugins.base import UrbanLensPlugin
from urbanlens.dashboard.services.apis.assets.base import MediaItem
from urbanlens.dashboard.services.apis.locations.redata_context_gateway import redata_configured
from urbanlens.dashboard.services.pins.external_data import GalleryMediaSource

if TYPE_CHECKING:
    from urbanlens.dashboard.models.pin.model import Pin
    from urbanlens.dashboard.services.pins.external_data import PanelSource

#: Street-level networks, whose captures the Street-level tab shows by date.
STREET_LEVEL_PROVIDERS = frozenset({"mapillary", "kartaview", "panoramax"})

#: Display names for REData's media provider tags; an unlisted tag is titled from itself.
PROVIDER_LABELS: dict[str, str] = {
    "nps_media": "National Park Service",
    "wikimedia_commons": "Wikimedia Commons",
    "flickr": "Flickr",
    "instagram": "Instagram",
    "youtube": "YouTube",
    "tiktok": "TikTok",
    "vimeo": "Vimeo",
    "dailymotion": "Dailymotion",
    "internet_archive_video": "Internet Archive",
    "mapillary": "Mapillary",
    "kartaview": "KartaView",
    "panoramax": "Panoramax",
}

#: The ``MediaItemSerializer`` fields a tile is built from; the rest (``attributes``, embed bookkeeping) stays in REData.
_STORED_FIELDS = ("uuid", "provider", "kind", "title", "description", "url", "thumbnail_url", "cached_url", "credit", "latitude", "longitude", "is_aerial")


def provider_label(provider: str) -> str:
    """A REData media provider tag as a reader would name it."""
    return PROVIDER_LABELS.get(provider) or provider.replace("_", " ").title() or "REData"


def stored_media_row(row: dict[str, Any]) -> dict[str, Any]:
    """The part of a REData media row a gallery tile needs, for the cache row."""
    return {name: row.get(name) for name in _STORED_FIELDS if row.get(name) not in (None, "")}


def mirrored_media_url(row: dict[str, Any]) -> str:
    """This site's proxy for REData's mirrored copy of a media row's image, or ``""`` when REData holds none.

    REData's ``cached_url`` needs its API key, which never reaches a browser, so the tile reads it through
    ``PinRedataMediaView`` instead.
    """
    from django.urls import reverse

    if not row.get("cached_url"):
        return ""
    try:
        media_uuid = uuid.UUID(str(row.get("uuid") or ""))
    except ValueError:
        return ""
    return reverse("pin.redata.media", args=[media_uuid])


def _coordinate(value: object) -> float | None:
    return float(value) if isinstance(value, int | float) and not isinstance(value, bool) else None


def media_item_from_row(row: dict[str, Any], *, fallback_caption: str = "") -> MediaItem | None:
    """A gallery tile for one REData media row.

    Args:
        row: A ``MediaItemSerializer`` row, or :func:`stored_media_row` of one.
        fallback_caption: The caption when the row has no title.

    Returns:
        The tile, or None when the row has no picture to show (an audio clip, a page REData found no image for).
    """
    mirrored = mirrored_media_url(row)
    picture = mirrored or str(row.get("thumbnail_url") or "")
    if not picture:
        return None
    title = str(row.get("title") or "")
    return MediaItem(
        url=picture,
        thumb_url=picture,
        caption=title or fallback_caption,
        source=provider_label(str(row.get("provider") or "")),
        page_url=str(row.get("url") or ""),
        # REData re-encodes every mirror to WebP; a hot-linked thumbnail declares nothing.
        content_type="image/webp" if mirrored else "",
        author=str(row.get("credit") or ""),
        title=title,
        description=str(row.get("description") or ""),
        latitude=_coordinate(row.get("latitude")),
        longitude=_coordinate(row.get("longitude")),
    )


class NearbyMediaSource(GalleryMediaSource):
    """Photographs, videos and webcams REData finds near the pin, less the aerial and street-level ones other tabs show."""

    key = "redata_media"
    cache_source = "redata_media"
    icon = "photo_library"
    title = "Nearby Media"
    # Half of these come from a metasearch on the place's name, with no coordinates of their own.
    judges_relevance: ClassVar[bool] = True

    def gate(self, pin: Pin) -> bool:
        """Requires coordinates and REData."""
        return bool(pin.effective_latitude and pin.effective_longitude) and redata_configured()

    def fetch(self, pin: Pin) -> None:
        """Cache the nearby media rows this tab shows, from the point's shared REData answer."""
        from urbanlens.dashboard.models.cache.location_cache import LocationCache
        from urbanlens.dashboard.services.locations.redata_point_data import media_near, point_key

        latitude = float(pin.effective_latitude or 0)
        longitude = float(pin.effective_longitude or 0)
        envelope = media_near(latitude, longitude)
        rows = [stored_media_row(row) for row in envelope.results if not row.get("is_aerial") and row.get("provider") not in STREET_LEVEL_PROVIDERS]
        LocationCache.set(pin.location, self.cache_source, envelope.marked({"items": rows}), query_key=point_key(latitude, longitude))

    def media_items(self, data: dict) -> list[MediaItem]:
        """One tile per cached row with a picture."""
        return [item for row in (data or {}).get("items") or [] if isinstance(row, dict) and (item := media_item_from_row(row)) is not None]


class NearbyMediaPlugin(UrbanLensPlugin):
    """REData's pooled media index near a pin, as a Media-gallery tab."""

    name: ClassVar[str] = "redata_nearby_media"
    verbose_name: ClassVar[str] = "Nearby Media"
    description: ClassVar[str] = "Adds a Nearby Media tab to the Media gallery: Wikimedia Commons, Flickr, YouTube and National Park Service photographs, videos and webcams REData finds near the pin."
    author: ClassVar[str] = "UrbanLens"

    def get_panel_sources(self) -> list[PanelSource]:
        """Contribute the Nearby Media gallery source."""
        return [NearbyMediaSource()]
