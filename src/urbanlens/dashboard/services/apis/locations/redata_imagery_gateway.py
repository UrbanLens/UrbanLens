"""Gateway for REData's ``/imagery/`` endpoint - pictures of a place.

See ``../REData/docs/api-reference.md``, "GET /imagery/ - pictures of a
place". Backs :class:`~urbanlens.dashboard.plugins.builtin.satellite_imagery.RedataSatelliteProvider`,
which requests only the providers not already covered - more richly - by
UrbanLens's own direct Esri integration (current + historical Wayback
releases) and its separate USGS Historical Topo Maps panel; see that module
for exactly which REData imagery providers are requested and why.

Three of REData's imagery providers (``mapbox``, ``bing_maps``,
``azure_maps``) need a vendor credential REData holds, not UrbanLens, so
their ``url`` points at REData's own ``/imagery/{uuid}/download/`` proxy
instead of a publicly fetchable image - see :meth:`RedataImageryGateway.download_bytes`.
"""

from __future__ import annotations

from typing import Any, ClassVar

from urbanlens.dashboard.services.apis.locations.redata_context_gateway import (
    REASON_SOURCE_ERROR,
    LocationContextUnavailableError,
    RedataLocationContextGateway,
)

_IMAGERY_PATH = "/api/v1/imagery/"
_TIMELINE_PATH = "/api/v1/imagery/timeline/"
_DOWNLOAD_TIMEOUT = 30


class RedataImageryGateway(RedataLocationContextGateway):
    """REST client for REData's cross-provider imagery endpoint."""

    service_key: ClassVar[str] = "redata_imagery"

    def get_imagery(self, latitude: float, longitude: float, *, providers: list[str] | None = None) -> list[dict[str, Any]]:
        """Return REData's normalized imagery results for a coordinate.

        Args:
            latitude: WGS-84 latitude.
            longitude: WGS-84 longitude.
            providers: Restrict to these REData provider tags; omit for
                every provider REData has configured.

        Returns:
            Provider-tagged imagery result dicts (``provider``, ``kind``,
            ``url``, ``delivery``, ``captured_on``, ``captured_label``,
            ``attribution``, and an ``attributes`` blob that carries
            ``subdomains`` for a ``tile_template`` delivery) - empty when
            nothing answered.

        Raises:
            LocationContextUnavailableError: Every requested provider failed
                to answer, or the request to REData failed outright.
        """
        envelope = self.near_point(_IMAGERY_PATH, latitude, longitude, provider=providers)
        return envelope.results

    def get_timeline(self, latitude: float, longitude: float, *, trigger_archive: bool = False) -> dict[str, Any]:
        """Return which dates imagery exists for at a coordinate.

        Two shapes come back and both matter, because sources answer
        differently and neither can be expressed as the other:

        * ``captures`` - concrete dated images, each carrying a full
          ``/imagery/`` row as its ``asset``.
        * ``providers_timeline[].time_series`` - continuous-coverage layers
          where the available dates are a *range*. NASA GIBS alone publishes
          four, each independently addressable; ``intervals`` and
          ``time_series_asset_uuid`` never merge across layers, so a date is
          always attributable to the layer it came from.

        ``since``/``until`` are deliberately not exposed: REData documents them
        as filtering the response rather than the fetch, and its cache holds
        one complete answer per point, so narrowing here would only hide
        captures a later call needs.

        Args:
            latitude: WGS-84 latitude.
            longitude: WGS-84 longitude.
            trigger_archive: Use ``POST``, which re-queries every source live
                and queues permanent archiving of what it finds. REData
                documents this as the call to make when about to show a time
                slider; the ``GET`` never archives. Costs a live fetch, so it
                is off by default.

        Returns:
            The timeline envelope (``earliest``, ``latest``, ``years``,
            ``captures``, ``providers_timeline``, ``providers``), or an empty
            dict when nothing answered.

        Raises:
            LocationContextUnavailableError: The request to REData failed.
        """
        params = {"lat": latitude, "lng": longitude}
        if trigger_archive:
            return self.post_json(_TIMELINE_PATH, params) or {}
        return self.get_json(_TIMELINE_PATH, params) or {}

    def download_bytes(self, url: str) -> bytes:
        """Fetch a credentialed imagery source's bytes through REData's authenticated proxy.

        Args:
            url: The ``url`` field from an imagery result whose ``delivery``
                needs REData's own auth (the three keyed providers - see the
                module docstring) - either absolute or relative to ``base_url``.

        Returns:
            The raw image bytes.

        Raises:
            LocationContextUnavailableError: The request failed, or REData
                answered with a non-200 status.
        """
        base_url = self.base_url
        if base_url is None:
            raise LocationContextUnavailableError(REASON_SOURCE_ERROR, "UL_REDATA_API_URL is not configured.")
        target = url if url.startswith(("http://", "https://")) else f"{base_url.rstrip('/')}/{url.lstrip('/')}"
        try:
            response = self.session.get(target, headers=self._headers, timeout=_DOWNLOAD_TIMEOUT)
        except OSError as exc:
            raise LocationContextUnavailableError(REASON_SOURCE_ERROR, f"Could not reach REData: {exc}") from exc
        if response.status_code != 200:
            self._raise_for_error_status(response, url)
        return response.content
