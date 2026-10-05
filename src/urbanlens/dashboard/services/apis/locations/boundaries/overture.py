"""Overture Maps buildings and places for a coordinate, from REData's mirror inside the US and the public release elsewhere.

REData syncs Overture's themes for every US state and territory and serves them from ``/buildings/`` and the
``overture`` points-of-interest provider, so a coordinate there never reaches Overture itself, even when REData
has nothing or is not configured.

The public reader (``overture_maps``) is imported only when used: importing it loads pyarrow.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, ClassVar

from urbanlens.dashboard.services.apis.locations.base import BoundaryProvider, BoundaryProviderDeferredError, best_containing_polygon
from urbanlens.dashboard.services.apis.locations.redata_context_gateway import LocationContextUnavailableError, redata_configured
from urbanlens.dashboard.services.core.gateway import UpstreamBusyError, is_source_outage
from urbanlens.dashboard.services.geo.geo_filter import is_usa_coordinates

if TYPE_CHECKING:
    from django.contrib.gis.geos import Polygon

    from urbanlens.dashboard.services.apis.locations.boundaries.overture_maps import OvertureMapsGateway

#: REData's provider tag for its Overture mirror, on both endpoints.
_REDATA_PROVIDER = "overture"

#: Only a footprint containing the coordinate is used, and its distance is zero, so the search need not reach further.
_BUILDING_SEARCH_RADIUS_METERS = 10.0


def served_by_redata(latitude: float, longitude: float) -> bool:
    """Whether REData's Overture mirror covers a coordinate, so the public release is not read for it.

    The US-and-territories test REData gates its own Overture providers on, with the same boxes.

    Args:
        latitude: WGS-84 latitude.
        longitude: WGS-84 longitude.

    Returns:
        True inside the US and its territories.
    """
    return is_usa_coordinates(latitude, longitude)


@dataclass(slots=True)
class OvertureProvider(BoundaryProvider):
    """Overture building footprints and places for one coordinate, from whichever copy may serve it."""

    service_key: ClassVar[str | None] = "overture"
    boundary_kind: ClassVar[str] = "building"

    def get_boundary(self, latitude: float, longitude: float, *, name: str | None = None) -> Polygon | None:
        """The smallest Overture building footprint containing the coordinate.

        Args:
            latitude: WGS-84 latitude.
            longitude: WGS-84 longitude.
            name: Unused; looked up by coordinate only.

        Returns:
            The footprint, or None when no building contains the point.

        Raises:
            BoundaryProviderDeferredError: REData could not answer for now.
        """
        if not served_by_redata(latitude, longitude):
            return _public_release().get_boundary(latitude, longitude)
        try:
            features = _redata_building_features(latitude, longitude)
        except LocationContextUnavailableError as exc:
            if not is_source_outage(exc):
                raise
            retry_after = exc.retry_after if isinstance(exc, UpstreamBusyError) else None
            raise BoundaryProviderDeferredError(self.service_key or _REDATA_PROVIDER, retry_after=retry_after) from exc
        return best_containing_polygon(features, latitude, longitude)

    def get_building_attributes(self, latitude: float, longitude: float) -> dict[str, Any] | None:
        """The physical attributes of the building at a coordinate.

        Args:
            latitude: WGS-84 latitude.
            longitude: WGS-84 longitude.

        Returns:
            See :func:`~urbanlens.dashboard.services.apis.locations.boundaries.overture_maps.building_attributes`.

        Raises:
            LocationContextUnavailableError: REData could not answer.
        """
        if not served_by_redata(latitude, longitude):
            return _public_release().get_building_attributes(latitude, longitude)
        from urbanlens.dashboard.services.apis.locations.boundaries.overture_maps import building_attributes

        return building_attributes(_redata_building_features(latitude, longitude), latitude, longitude)

    def get_nearby_places(self, latitude: float, longitude: float, *, radius_m: float = 150.0, limit: int = 5) -> list[dict[str, Any]]:
        """Named Overture places near a coordinate, nearest first.

        Args:
            latitude: WGS-84 latitude.
            longitude: WGS-84 longitude.
            radius_m: Search radius in meters.
            limit: Maximum number of places to return.

        Returns:
            See :func:`~urbanlens.dashboard.services.apis.locations.boundaries.overture_maps.nearby_places`.

        Raises:
            LocationContextUnavailableError: REData could not answer.
        """
        if not served_by_redata(latitude, longitude):
            return _public_release().get_nearby_places(latitude, longitude, radius_m=radius_m, limit=limit)
        if not redata_configured():
            return []
        from urbanlens.dashboard.services.apis.locations.boundaries.overture_maps import nearby_places
        from urbanlens.dashboard.services.apis.locations.redata_points_of_interest_gateway import RedataPointsOfInterestGateway

        rows = RedataPointsOfInterestGateway().find_near(latitude, longitude, provider=_REDATA_PROVIDER, radius_meters=radius_m)
        return nearby_places([_place_feature(row) for row in rows], latitude, longitude, radius_m=radius_m, limit=limit)


def _public_release() -> OvertureMapsGateway:
    from urbanlens.dashboard.services.apis.locations.boundaries.overture_maps import OvertureMapsGateway

    return OvertureMapsGateway()


def _redata_building_features(latitude: float, longitude: float) -> list[dict]:
    if not redata_configured():
        return []
    from urbanlens.dashboard.services.apis.locations.redata_buildings_gateway import RedataBuildingsGateway

    records = RedataBuildingsGateway().find_near(latitude, longitude, provider=_REDATA_PROVIDER, radius_meters=_BUILDING_SEARCH_RADIUS_METERS)
    return [_building_feature(record) for record in records]


def _building_feature(record: dict[str, Any]) -> dict[str, Any]:
    """A REData ``BuildingRecord`` from its Overture mirror, as the feature it was mirrored from.

    Its ``attributes`` are the Overture row's own properties: the promoted ``subtype``/``class``/``height``/
    ``num_floors``/``sources`` plus every other column REData stores, the roof columns among them. The name is taken
    from the record's published ``name`` (Overture's ``names.primary``), falling back to the untyped ``attributes``.
    """
    properties = dict(record.get("attributes") or {})
    if record.get("name"):
        properties["names"] = {"primary": record["name"]}
    return {"type": "Feature", "geometry": record.get("geometry"), "properties": properties}


def _place_feature(row: dict[str, Any]) -> dict[str, Any]:
    """A REData ``overture`` point of interest as the Overture place it was mirrored from.

    REData flattens ``names.primary`` into ``name`` and the taxonomy's primary category into ``category``, and keeps
    the rest of the row (``confidence``, ``operating_status``, and anything it does not promote) in ``attributes``.
    """
    attributes = row.get("attributes") or {}
    return {
        "type": "Feature",
        "geometry": {"type": "Point", "coordinates": [row.get("longitude"), row.get("latitude")]},
        "properties": {**attributes, "names": {"primary": row.get("name")}, "categories": {"primary": row.get("category")}},
    }
