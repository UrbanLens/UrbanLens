"""Street-level photos plugin: one Media-gallery tile per date a volunteer network photographed the pin from the street.

The same REData answer feeds the street-view carousel (``redata_media_gateway``'s Mapillary, KartaView and Panoramax
providers), so the tab costs no call of its own. Its tiles open REData's permanent archive of each frame, which still
shows a demolished building after its contributor deletes the sequence upstream.
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

#: The ``StreetViewCaptureSerializer`` fields a tile is built from.
_STORED_CAPTURE_FIELDS = ("uuid", "provider", "captured_on", "heading_degrees", "is_panoramic", "image_url", "thumbnail_url", "credit", "license", "latitude", "longitude")


def _stored_date(entry: dict[str, Any]) -> dict[str, Any]:
    representative = entry.get("representative")
    capture = representative if isinstance(representative, dict) else {}
    return {
        "captured_on": str(entry.get("captured_on") or "")[:10],
        "provider": str(entry.get("provider") or capture.get("provider") or ""),
        "count": entry.get("count") or 1,
        "is_panoramic": bool(entry.get("is_panoramic")),
        "representative": {name: capture.get(name) for name in _STORED_CAPTURE_FIELDS if capture.get(name) not in (None, "")},
    }


def _archive_url(capture: dict[str, Any]) -> str:
    """This site's proxy for REData's archived copy of a capture, or ``""`` when the row names no capture."""
    from django.urls import reverse

    try:
        capture_uuid = uuid.UUID(str(capture.get("uuid") or ""))
    except ValueError:
        return ""
    return reverse("pin.redata.street_view", args=[capture_uuid])


def _caption(entry: dict[str, Any], capture: dict[str, Any], network: str) -> str:
    parts = [part for part in (network, str(entry.get("captured_on") or "")) if part]
    if entry.get("is_panoramic"):
        parts.append("360°")
    heading = capture.get("heading_degrees")
    if isinstance(heading, int | float) and not isinstance(heading, bool):
        parts.append(f"facing {round(heading) % 360}°")
    return " · ".join(parts)


def street_level_item(entry: dict[str, Any]) -> MediaItem | None:
    """A gallery tile for one cached capture date.

    Args:
        entry: A cached date entry (see :meth:`StreetLevelPhotosSource.fetch`).

    Returns:
        The tile, or None when the date's representative frame has no picture.
    """
    from urbanlens.dashboard.plugins.builtin.redata_nearby_media import provider_label

    representative = entry.get("representative")
    capture: dict[str, Any] = representative if isinstance(representative, dict) else {}
    network_thumbnail = str(capture.get("thumbnail_url") or capture.get("image_url") or "")
    archive = _archive_url(capture)
    if not network_thumbnail and not archive:
        return None
    network = provider_label(str(entry.get("provider") or ""))
    latitude, longitude = capture.get("latitude"), capture.get("longitude")
    return MediaItem(
        # The lightbox opens the archive; the tile reads the network's own thumbnail, which costs REData nothing.
        url=archive or network_thumbnail,
        thumb_url=network_thumbnail or archive,
        caption=_caption(entry, capture, network),
        source=network,
        page_url=str(capture.get("image_url") or ""),
        author=str(capture.get("credit") or ""),
        latitude=float(latitude) if isinstance(latitude, int | float) else None,
        longitude=float(longitude) if isinstance(longitude, int | float) else None,
    )


class StreetLevelPhotosSource(GalleryMediaSource):
    """Every date a street-level network photographed the pin, newest first, as a Media-gallery tab."""

    key = "redata_street_level"
    cache_source = "redata_street_level"
    icon = "streetview"
    title = "Street-level"

    def gate(self, pin: Pin) -> bool:
        """Requires coordinates and REData; without REData the carousel's own providers are the street-level view."""
        return bool(pin.effective_latitude and pin.effective_longitude) and redata_configured()

    def fetch(self, pin: Pin) -> None:
        """Cache the point's capture dates, unless no network answered."""
        from urbanlens.dashboard.models.cache.location_cache import LocationCache
        from urbanlens.dashboard.services.locations.redata_point_data import point_key, street_view_dates

        latitude = float(pin.effective_latitude or 0)
        longitude = float(pin.effective_longitude or 0)
        found = street_view_dates(latitude, longitude)
        if not found.complete and not found.dates:
            # An outage, not an answer; leaving the row absent keeps it retryable.
            return
        LocationCache.set(pin.location, self.cache_source, {"dates": [_stored_date(entry) for entry in found.dates]}, query_key=point_key(latitude, longitude))

    def media_items(self, data: dict) -> list[MediaItem]:
        """One tile per cached date with a picture, in cached (newest-first) order."""
        return [item for entry in (data or {}).get("dates") or [] if isinstance(entry, dict) and (item := street_level_item(entry)) is not None]


class StreetLevelPhotosPlugin(UrbanLensPlugin):
    """Dated street-level photographs of pinned locations, sourced through REData."""

    name: ClassVar[str] = "redata_street_level"
    verbose_name: ClassVar[str] = "Street-level Photos"
    description: ClassVar[str] = "Adds a Street-level tab to the Media gallery: one photograph per date Mapillary, KartaView or Panoramax captured the pin, opening REData's permanent archive of each frame."
    author: ClassVar[str] = "UrbanLens"

    def get_panel_sources(self) -> list[PanelSource]:
        """Contribute the Street-level gallery source."""
        return [StreetLevelPhotosSource()]
