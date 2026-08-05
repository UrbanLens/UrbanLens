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
