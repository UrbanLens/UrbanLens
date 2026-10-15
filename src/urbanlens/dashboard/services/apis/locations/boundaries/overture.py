"""
Overture Maps buildings and places for a coordinate, from a regional mirror where one covers it and the public release elsewhere.

A mirror (``points.OVERTURE_MIRRORS``) syncs Overture's themes for the regions it covers, so a coordinate it covers
never reaches Overture itself, even when the mirror has nothing or is not configured.

The public reader (``overture_maps``) is imported only when used: importing it loads pyarrow.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, ClassVar

from urbanlens.dashboard.plugins.points import OVERTURE_MIRRORS
from urbanlens.dashboard.services.apis.locations.base import BoundaryProvider, BoundaryProviderDeferredError, best_containing_polygon
from urbanlens.dashboard.services.core.gateway import ProviderUnavailableError, UpstreamBusyError, is_source_outage

if TYPE_CHECKING:
    from django.contrib.gis.geos import Polygon

    from urbanlens.dashboard.services.apis.locations.boundaries.overture_maps import OvertureMapsGateway


class OvertureMirror(ABC):
    """
    A regional copy of Overture Maps, served in place of the public release where it covers a coordinate.

    Attributes:
        key: Names the mirror in a deferral.
    """

    key: ClassVar[str]

    @abstractmethod
    def covers(self, latitude: float, longitude: float) -> bool:
        """
        Whether this mirror serves a coordinate, so the public release is not read for it.

        Independent of whether the mirror is reachable or configured: a covered coordinate is never read from Overture.
        """

    @abstractmethod
    def building_features(self, latitude: float, longitude: float) -> list[dict[str, Any]]:
        """
        Overture building features (GeoJSON, Overture's own properties) around a coordinate, or ``[]`` when the mirror cannot be asked.

        Raises:
            ProviderUnavailableError: The mirror could not answer for now.
        """

    @abstractmethod
    def place_features(self, latitude: float, longitude: float, *, radius_m: float) -> list[dict[str, Any]]:
        """
        Overture place features (GeoJSON, Overture's own properties) within a radius, or ``[]`` when the mirror cannot be asked.

        Raises:
            ProviderUnavailableError: The mirror could not answer for now.
        """

    def available(self) -> bool:
        """Whether this mirror can be asked at all."""
        return True


def overture_mirror_for(latitude: float, longitude: float) -> OvertureMirror | None:
    """
    The mirror that serves a coordinate, if any.

    Args:
        latitude: WGS-84 latitude.
        longitude: WGS-84 longitude.

    Returns:
        The highest-priority mirror covering it, or None where the public release serves it.
    """
    return next((mirror for mirror in OVERTURE_MIRRORS if mirror.covers(latitude, longitude)), None)


@dataclass(slots=True)
class OvertureProvider(BoundaryProvider):
    """Overture building footprints and places for one coordinate, from whichever copy may serve it."""

    service_key: ClassVar[str | None] = "overture"
    boundary_kind: ClassVar[str] = "building"

    def get_boundary(self, latitude: float, longitude: float, *, name: str | None = None) -> Polygon | None:
        """
        The smallest Overture building footprint containing the coordinate.

        Args:
            latitude: WGS-84 latitude.
            longitude: WGS-84 longitude.
            name: Unused; looked up by coordinate only.

        Returns:
            The footprint, or None when no building contains the point.

        Raises:
            BoundaryProviderDeferredError: The mirror covering the coordinate could not answer for now.
        """
        mirror = overture_mirror_for(latitude, longitude)
        if mirror is None:
            return _public_release().get_boundary(latitude, longitude)
        try:
            features = mirror.building_features(latitude, longitude)
        except ProviderUnavailableError as exc:
            if not is_source_outage(exc):
                raise
            retry_after = exc.retry_after if isinstance(exc, UpstreamBusyError) else None
            raise BoundaryProviderDeferredError(self.service_key or mirror.key, retry_after=retry_after) from exc
        return best_containing_polygon(features, latitude, longitude)

    def get_building_attributes(self, latitude: float, longitude: float) -> dict[str, Any] | None:
        """
        The physical attributes of the building at a coordinate.

        Args:
            latitude: WGS-84 latitude.
            longitude: WGS-84 longitude.

        Returns:
            See :func:`~urbanlens.dashboard.services.apis.locations.boundaries.overture_maps.building_attributes`.

        Raises:
            ProviderUnavailableError: The mirror covering the coordinate could not answer.
        """
        mirror = overture_mirror_for(latitude, longitude)
        if mirror is None:
            return _public_release().get_building_attributes(latitude, longitude)
        from urbanlens.dashboard.services.apis.locations.boundaries.overture_maps import building_attributes

        return building_attributes(mirror.building_features(latitude, longitude), latitude, longitude)

    def get_nearby_places(self, latitude: float, longitude: float, *, radius_m: float = 150.0, limit: int = 5) -> list[dict[str, Any]]:
        """
        Named Overture places near a coordinate, nearest first.

        Args:
            latitude: WGS-84 latitude.
            longitude: WGS-84 longitude.
            radius_m: Search radius in meters.
            limit: Maximum number of places to return.

        Returns:
            See :func:`~urbanlens.dashboard.services.apis.locations.boundaries.overture_maps.nearby_places`.

        Raises:
            ProviderUnavailableError: The mirror covering the coordinate could not answer.
        """
        mirror = overture_mirror_for(latitude, longitude)
        if mirror is None:
            return _public_release().get_nearby_places(latitude, longitude, radius_m=radius_m, limit=limit)
        if not mirror.available():
            return []
        from urbanlens.dashboard.services.apis.locations.boundaries.overture_maps import nearby_places

        return nearby_places(mirror.place_features(latitude, longitude, radius_m=radius_m), latitude, longitude, radius_m=radius_m, limit=limit)


def _public_release() -> OvertureMapsGateway:
    from urbanlens.dashboard.services.apis.locations.boundaries.overture_maps import OvertureMapsGateway

    return OvertureMapsGateway()
