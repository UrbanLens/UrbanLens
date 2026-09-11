"""Street View imagery selection for Street View mode rounds.

See ``docs/designs/drafts/spotguessr.md`` ("Street View mode") for the rules this
encodes.
"""

from __future__ import annotations

from dataclasses import dataclass
import logging
from typing import TYPE_CHECKING

from urbanlens.dashboard.services.apis.locations.base import SlideFetch
from urbanlens.dashboard.services.apis.locations.google.maps import GoogleMapsGateway
from urbanlens.dashboard.services.core.timeout_utils import EXTERNAL_CALL_DEADLINE, call_with_deadline

if TYPE_CHECKING:
    from urbanlens.dashboard.models.location.model import Location

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class StreetViewPanorama:
    """A candidate Street View panorama for a SpotGuessr round.

    Attributes:
        latitude: The panorama's own resolved latitude - may differ slightly
            from ``location``'s coordinates, since coverage lookup can widen
            its search radius to find the nearest pano.
        longitude: The panorama's own resolved longitude.
        image: A base64 ``data:`` URI static image, kept as a fallback for
            the client-side interactive panorama (``google.maps.StreetViewPanorama``)
            to fall back to if it fails to load.
    """

    latitude: float
    longitude: float
    image: str


def candidate_street_view_for_location(location: Location) -> StreetViewPanorama | None:
    """Fetch a Street View panorama for ``location``, or None if unavailable.
    The returned panorama's coordinates are handed to the client so it can render an interactive ``google.maps.StreetViewPanorama`` there (pan/zoom/ walk between connected panos) - unlike the rest of a SpotGuessr round payload, this is a deliberate exception to "never reveal the answer before a guess": true client-side panning requires the browser to talk to Google directly, which is only possible if it knows where to look."""
    no_coverage = SlideFetch([], from_cache=False)
    try:
        # Indexed rather than attribute-read: `call_with_deadline` may return
        # the plain default, and only the slides matter here - one candidate
        # panorama is either found or it isn't, with nothing to re-warm.
        slides = call_with_deadline(
            lambda: GoogleMapsGateway().get_street_view_slides(float(location.latitude), float(location.longitude), limit=1),
            timeout=EXTERNAL_CALL_DEADLINE,
            default=no_coverage,
            name="google_maps.street_view",
        )[0]
    except Exception:
        logger.info("Street View unavailable for location=%s", location.pk, exc_info=True)
        return None
    if not slides:
        return None
    slide = slides[0]
    if slide.latitude is None or slide.longitude is None:
        return None
    return StreetViewPanorama(latitude=slide.latitude, longitude=slide.longitude, image=slide.img_src)
