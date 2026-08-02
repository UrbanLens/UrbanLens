"""Street View imagery selection for Street View mode rounds.

See ``docs/designs/spotguessr.md`` ("Street View mode") for the rules this
encodes.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from urbanlens.dashboard.services.apis.locations.google.maps import GoogleMapsGateway
from urbanlens.dashboard.services.core.timeout_utils import EXTERNAL_CALL_DEADLINE, call_with_deadline

if TYPE_CHECKING:
    from urbanlens.dashboard.models.location.model import Location
    from urbanlens.dashboard.services.apis.locations.base import StreetViewSlide

logger = logging.getLogger(__name__)


def candidate_street_view_for_location(location: Location) -> str | None:
    """Fetch a Street View image data URI for ``location``, or None if unavailable.

    Reuses the existing ``GoogleMapsGateway`` (the same server-side,
    cache-backed fetch already powering the pin-detail Street View
    carousel), including its coverage-metadata check. The API key never
    reaches the client - the gateway returns a base64 ``data:`` URI, not a
    URL to call directly.

    A paid, rate-limited external API sits on the critical path of picking
    a round here, so any failure (no coverage, network error, rate limit)
    is swallowed and treated as "not eligible" - round generation must
    degrade to trying another location, never crash (mirrors how Photos
    mode treats "no usable photo"). This is called once per candidate
    location - up to ``session._MAX_LOCATION_ATTEMPTS`` times synchronously
    inside the SpotGuessr start/round request - so, same as the pin-detail
    carousel's own fetch of this method (``controllers.pin``), the call is
    bounded by ``call_with_deadline`` rather than trusting the gateway's own
    per-socket timeout: a slow trickle of bytes from a degraded provider can
    keep a request handler blocked far longer than one candidate's share of
    the request should cost.
    """
    no_coverage: tuple[list[StreetViewSlide], bool] = ([], False)
    try:
        slides, _from_cache = call_with_deadline(
            lambda: GoogleMapsGateway().get_street_view_slides(float(location.latitude), float(location.longitude), limit=1),
            timeout=EXTERNAL_CALL_DEADLINE,
            default=no_coverage,
            name="google_maps.street_view",
        )
    except Exception:
        logger.info("Street View unavailable for location=%s", location.pk, exc_info=True)
        return None
    if not slides:
        return None
    return slides[0].img_src
