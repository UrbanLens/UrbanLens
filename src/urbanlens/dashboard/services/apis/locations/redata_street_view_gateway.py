"""Gateway for REData's ``/street-view/`` endpoints."""

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
        One entry per capture *date* rather than per frame (a vehicle records dozens of frames in seconds); each entry's ``representative`` is the frame taken nearest the query point, preferring a panorama on a tie.

        Returns:
            The raw timeline body: ``dates`` (each with ``captured_on``, ``count``, ``is_panoramic`` and a full ``representative`` capture row), ``years``, ``earliest``, ``latest``, ``providers_timeline``, and the standard ``providers`` block.

        Raises:
            LocationContextUnavailableError: The request failed or REData rejected a parameter."""
        params: dict[str, Any] = {"lat": latitude, "lng": longitude}
        if provider:
            params["provider"] = provider
        if since:
            params["since"] = since.isoformat()
        if until:
            params["until"] = until.isoformat()
        return self.get_json(_STREET_VIEW_TIMELINE_PATH, params)
