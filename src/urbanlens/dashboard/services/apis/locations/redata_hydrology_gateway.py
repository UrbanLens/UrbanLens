"""Gateway for REData's ``/hydrology/`` near-a-coordinate endpoint.

See ``../REData/docs/api-reference.md``, "GET /hydrology/ - water near a
point": mapped streams, waterbodies and wetlands within 1 km (USGS NHD),
National Wetlands Inventory polygons (USFWS NWI - the authoritative federal
wetlands map), and the HUC12 sub-watershed containing the point (USGS WBD).
All USA-only and keyless.

Null handling a consumer must preserve: ``distance_meters`` is null for a
watershed (it *contains* the point) and for every NWI row (the source layer
returns no geometry) - unmeasurable, not missing. A blank ``name`` is normal
and meaningful: most mapped headwater streams are genuinely unnamed.
"""

from __future__ import annotations

from typing import ClassVar

from urbanlens.dashboard.services.apis.locations.redata_context_gateway import LocationContextEnvelope, RedataLocationContextGateway

_HYDROLOGY_PATH = "/api/v1/hydrology/"

#: REData's four-value ``kind`` vocabulary (collapsing ~70 USGS feature
#: codes; the source's own code survives in ``feature_type``).
HYDROLOGY_KIND_LABELS: dict[str, str] = {
    "stream": "Stream",
    "waterbody": "Waterbody",
    "wetland": "Wetland",
    "watershed": "Watershed",
}


class RedataHydrologyGateway(RedataLocationContextGateway):
    """REST client for REData's hydrology endpoint."""

    service_key: ClassVar[str] = "redata_hydrology"

    def get_hydrology(
        self,
        latitude: float,
        longitude: float,
        *,
        limit: int | None = None,
        force_refresh: bool = False,
    ) -> LocationContextEnvelope:
        """Fetch streams, waterbodies, wetlands and the containing watershed.

        Args:
            latitude: WGS-84 latitude.
            longitude: WGS-84 longitude.
            limit: Maximum number of features to return.
            force_refresh: Bypass REData's cache and re-query live.

        Returns:
            The parsed envelope. Entries carry ``kind`` (see
            :data:`HYDROLOGY_KIND_LABELS`), ``name`` (blank is "unnamed", not
            unknown), ``feature_type``, nullable ``distance_meters`` (null
            for watersheds and NWI wetlands - see module docstring), and
            ``area_sq_km`` (read it to tell a pond from a bay - an estuary is
            one polygon). NWI rows decode their classification under
            ``attributes`` (``nwi_code``, ``system``, ``wetland_class``,
            ``water_regime``).

        Raises:
            LocationContextUnavailableError: Every covering source failed to
                answer, or the request itself failed.
        """
        return self.near_point(
            _HYDROLOGY_PATH,
            latitude,
            longitude,
            force_refresh=force_refresh,
            limit=limit,
        )
