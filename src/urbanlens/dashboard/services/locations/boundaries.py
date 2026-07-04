"""Boundary-provider abstractions for default Location geometry."""

from __future__ import annotations

from dataclasses import dataclass, field
import logging

from django.contrib.gis.geos import MultiPolygon, Polygon

from urbanlens.dashboard.services.apis.locations.base import BoundaryProvider, StaticBoundaryProvider, default_bbox
from urbanlens.dashboard.services.apis.locations.boundaries.google_open_buildings import GoogleOpenBuildingsGateway
from urbanlens.dashboard.services.apis.locations.boundaries.microsoft_buildings import MicrosoftBuildingFootprintsGateway
from urbanlens.dashboard.services.apis.locations.boundaries.overpass import OverpassGateway
from urbanlens.dashboard.services.apis.locations.boundaries.overture_maps import OvertureMapsGateway

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class BoundaryProviderChain:
    """Resolve default boundaries by trying providers in order until one succeeds."""

    providers: tuple[BoundaryProvider, ...] = field(
        default_factory=lambda: (
            OverpassGateway(),
            OvertureMapsGateway(),
            MicrosoftBuildingFootprintsGateway(),
            GoogleOpenBuildingsGateway(),
            StaticBoundaryProvider(),
        ),
    )

    def get_boundary(self, latitude: float, longitude: float, *, name: str | None = None) -> Polygon:
        for provider in self.providers:
            try:
                boundary = provider.get_boundary(latitude, longitude, name=name)
            except Exception:
                # TODO: Catch specific exception
                logger.exception("Boundary provider %s failed for %s,%s", provider.service_key, latitude, longitude)
                continue
            if boundary is not None:
                return boundary
        return default_bbox(latitude, longitude)


def boundary_as_multipolygon(latitude: float, longitude: float, *, name: str | None = None) -> MultiPolygon:
    """Resolve the default boundary for a coordinate and normalise it to MultiPolygon.

    Runs the full BoundaryProviderChain (Overpass → Regrid → Overture → Microsoft
    Buildings → Google Open Buildings → static bbox fallback) and always returns a
    MultiPolygon, ready to store in ``Campus.polygon``.

    Args:
        latitude: WGS84 latitude.
        longitude: WGS84 longitude.
        name: Optional place name forwarded to providers that support name-based lookup.

    Returns:
        A MultiPolygon in SRID 4326.
    """
    geom = BoundaryProviderChain().get_boundary(latitude, longitude, name=name)
    if isinstance(geom, Polygon):
        return MultiPolygon(geom, srid=geom.srid)
    return geom
