"""Gateway for REData's ``/street-view/`` endpoints.

See ``../REData/docs/api-reference.md``, "GET /street-view/ - dated
street-level photographs of a point" and "GET /street-view/timeline/".
Unlike ``/media/lookup/`` (which returns each network's *current* nearby
photos), these return **every capture at the point across every date** -
Mapillary back to 2013 in most cities - with a real capture timestamp and
compass heading per image. For an app about documenting decay, the dated
history is the product: the timeline shows what a site looked like on every
date a camera passed it.

``download_url`` on a capture is REData's permanently archived copy - what
still resolves after a contributor deletes the sequence - but it requires
REData API auth, so browser-facing consumers use ``image_url``/
``thumbnail_url`` (the network's own copy, which attribution links to).

Google Street View is deliberately not a source here: its historical
panoramas are only reachable through the browser Maps JavaScript API, so the
direct Google provider remains alongside this in the carousel.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, ClassVar

from urbanlens.dashboard.services.apis.locations.redata_context_gateway import RedataLocationContextGateway

if TYPE_CHECKING:
    import datetime

_STREET_VIEW_TIMELINE_PATH = "/api/v1/street-view/timeline/"


class RedataStreetViewGateway(RedataLocationContextGateway):
    """REST client for REData's dated street-level photography endpoints."""

    service_key: ClassVar[str] = "redata_street_view"

    def get_timeline(
        self,
        latitude: float,
        longitude: float,
        *,
        provider: str | None = None,
        since: datetime.date | None = None,
        until: datetime.date | None = None,
    ) -> dict[str, Any]:
        """Fetch the capture-date timeline for a point.

        One entry per capture *date* rather than per frame (a vehicle records
        dozens of frames in seconds); each entry's ``representative`` is the
        frame taken nearest the query point, preferring a panorama on a tie.

        Args:
            latitude: WGS-84 latitude.
            longitude: WGS-84 longitude.
            provider: Restrict to one network (``mapillary``/``panoramax``/
                ``kartaview``).
            since: Only dates on or after this. Filters the response, not the
                fetch.
            until: Only dates on or before this.

        Returns:
            The raw timeline body: ``dates`` (each with ``captured_on``,
            ``count``, ``is_panoramic`` and a full ``representative`` capture
            row), ``years``, ``earliest``, ``latest``,
            ``providers_timeline``, and the standard ``providers`` block.

        Raises:
            LocationContextUnavailableError: The request failed or REData
                rejected a parameter.
        """
        params: dict[str, Any] = {"lat": latitude, "lng": longitude}
        if provider:
            params["provider"] = provider
        if since:
            params["since"] = since.isoformat()
        if until:
            params["until"] = until.isoformat()
        return self.get_json(_STREET_VIEW_TIMELINE_PATH, params)
