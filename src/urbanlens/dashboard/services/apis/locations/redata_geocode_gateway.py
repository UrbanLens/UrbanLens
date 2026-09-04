"""Gateway for REData's ``GET /geocode/`` and ``GET /geocode/reverse/``.

See ``../REData/docs/api-reference.md``, "GET /geocode/ - free text to
places" and "GET /geocode/reverse/ - a coordinate to the place there".
Results are not merged into one cross-provider ranking - each provider's own
``rank`` is preserved and results are concatenated in registry order (see the
doc) - so callers that want "the best answer" take the first result of
whichever provider they trust, not the first result overall.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, ClassVar

from urbanlens.dashboard.services.apis.locations.redata_context_gateway import LocationContextEnvelope, RedataLocationContextGateway


@dataclass(slots=True, kw_only=True)
class RedataGeocodeGateway(RedataLocationContextGateway):
    """REST client for REData's geocoding endpoints."""

    service_key: ClassVar[str] = "redata_geocode"

    def geocode(self, query: str, *, latitude: float | None = None, longitude: float | None = None, limit: int | None = None, provider: str | list[str] | None = None) -> LocationContextEnvelope:
        """Resolve free text to places (``GET /geocode/``).

        Args:
            query: The free-text search string.
            latitude: Optional bias center - narrows results toward this
                point without excluding others. Only meaningful together with
                ``longitude``.
            longitude: Optional bias center - see ``latitude``.
            limit: Bounded positive integer.
            provider: Restrict which source(s) actually run.

        Returns:
            The parsed envelope - ``results`` in registry order, one entry
            per provider's own hit (not merged/re-ranked - see the module docstring).

        Note:
            REData's own ``../REData/docs/api-reference.md`` documents this endpoint's
            request parameters and its shared envelope, but doesn't show a
            full worked example of one result entry's own fields. Callers
            here read ``latitude``/``longitude`` off each result, following
            the convention used throughout the rest of this API - verify
            against a live REData instance once its geocoding endpoint is
            confirmed deployed, and adjust if its real field names differ.
        """
        params: dict[str, Any] = {"q": query}
        if latitude is not None:
            params["lat"] = latitude
        if longitude is not None:
            params["lng"] = longitude
        if limit is not None:
            params["limit"] = limit
        if provider is not None:
            params["provider"] = provider
        return self._get_envelope("/api/v1/geocode/", params)

    def reverse_geocode(self, latitude: float, longitude: float, *, provider: str | list[str] | None = None, force_refresh: bool = False) -> LocationContextEnvelope:
        """Resolve a coordinate to the place there (``GET /geocode/reverse/``).

        Args:
            latitude: WGS-84 latitude.
            longitude: WGS-84 longitude.
            provider: Restrict which source(s) actually run.
            force_refresh: Bypass REData's cache and re-query live.

        Returns:
            The parsed envelope - ``radius_meters`` is nominal here (no
            vendor exposes a search radius for reverse geocoding).
        """
        return self.near_point("/api/v1/geocode/reverse/", latitude, longitude, provider=provider, force_refresh=force_refresh)
