"""Gateway for REData's ``/walkability/`` endpoint.

See ``../REData/docs/api-reference.md``, "GET /walkability/ - EPA National
Walkability Index". One provider (``epa_walkability``), keyless, USA-only.

The score describes a **census block group**, not a coordinate - two
addresses a street apart share a score, and a block group containing a park
or rail yard is scored as a whole (``block_group_geoid`` says exactly what
the number covers). ``transit_distance_meters`` is null where no stop is in
range, and null is the *majority* answer nationally - ordinary, not missing
data.
"""

from __future__ import annotations

from typing import ClassVar

from urbanlens.dashboard.services.apis.locations.redata_context_gateway import LocationContextEnvelope, RedataLocationContextGateway

_WALKABILITY_PATH = "/api/v1/walkability/"


class RedataWalkabilityGateway(RedataLocationContextGateway):
    """REST client for REData's EPA walkability-index endpoint."""

    service_key: ClassVar[str] = "redata_walkability"

    def get_walkability(
        self,
        latitude: float,
        longitude: float,
        *,
        force_refresh: bool = False,
    ) -> LocationContextEnvelope:
        """Fetch the EPA National Walkability Index for the block group at a point.

        Args:
            latitude: WGS-84 latitude.
            longitude: WGS-84 longitude.
            force_refresh: Bypass REData's cache and re-query live.

        Returns:
            The parsed envelope. The (at most one) result carries ``index``
            (EPA's 1-20 scale), ``band`` (EPA's own label for that score,
            e.g. "Most walkable"), ``intersection_density`` (street
            intersections per square mile), nullable
            ``transit_distance_meters`` (a stop on the parcel is a
            legitimate ``0``), and ``block_group_geoid``.

        Raises:
            LocationContextUnavailableError: The source failed to answer, or
                the request itself failed.
        """
        return self.near_point(_WALKABILITY_PATH, latitude, longitude, force_refresh=force_refresh)
