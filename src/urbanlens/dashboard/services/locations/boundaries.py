"""Boundary-provider abstractions for default Location geometry.

The chain resolves *typed* boundaries: a property boundary (parcel/grounds)
and a building boundary (structure footprint) are looked up independently.
Providers that can't distinguish declare themselves as property sources -
ambiguity always resolves to property. There is no static-bbox fallback any
more: when nothing is found, the effective property boundary is the default
circle around the location's coordinates (see ``Boundary.effective_polygon``),
and a missing building boundary simply means "no known building".
"""

from __future__ import annotations

from dataclasses import dataclass, field
import logging
from typing import TYPE_CHECKING

from django.contrib.gis.geos import MultiPolygon, Polygon
from django.utils import timezone

from urbanlens.dashboard.services.apis.locations.base import BoundaryProvider
from urbanlens.dashboard.services.apis.locations.boundaries.google_open_buildings import GoogleOpenBuildingsGateway
from urbanlens.dashboard.services.apis.locations.boundaries.microsoft_buildings import MicrosoftBuildingFootprintsGateway
from urbanlens.dashboard.services.apis.locations.boundaries.overpass import OverpassGateway
from urbanlens.dashboard.services.apis.locations.boundaries.overture_maps import OvertureMapsGateway
from urbanlens.dashboard.services.redact import redact_coordinate

if TYPE_CHECKING:
    from urbanlens.dashboard.models.location.model import Location

logger = logging.getLogger(__name__)


def _as_multipolygon(geom: Polygon | MultiPolygon | None) -> MultiPolygon | None:
    """Normalize a polygonal geometry to MultiPolygon (SRID preserved)."""
    if geom is None:
        return None
    if isinstance(geom, Polygon):
        return MultiPolygon(geom, srid=geom.srid)
    return geom


@dataclass(slots=True)
class ResolvedBoundaries:
    """Typed result of one provider-chain run for a coordinate."""

    property_polygon: MultiPolygon | None = None
    building_polygon: MultiPolygon | None = None

    def polygon_for(self, boundary_type: str) -> MultiPolygon | None:
        """The resolved polygon for a :class:`BoundaryType` value, or None."""
        if boundary_type == "building":
            return self.building_polygon
        return self.property_polygon

    @property
    def complete(self) -> bool:
        """True when both boundary types have been resolved."""
        return self.property_polygon is not None and self.building_polygon is not None


@dataclass(slots=True)
class BoundaryProviderChain:
    """Resolve typed default boundaries by trying providers in order.

    Each provider contributes to whichever boundary-type slots it can fill
    (declared via ``BoundaryProvider.boundary_kind`` or a per-feature
    ``get_typed_boundaries`` override); the chain stops once both slots are
    filled or providers are exhausted. Regrid (parcel/property data) is
    implemented but deliberately excluded - it is a paid service (see
    regrid.py); add ``RegridGateway()`` here to enable parcel boundaries.
    """

    providers: tuple[BoundaryProvider, ...] = field(
        default_factory=lambda: (
            OverpassGateway(),
            OvertureMapsGateway(),
            MicrosoftBuildingFootprintsGateway(),
            GoogleOpenBuildingsGateway(),
        ),
    )

    def get_boundaries(self, latitude: float, longitude: float, *, name: str | None = None) -> ResolvedBoundaries:
        """Run the chain and return typed boundaries for a coordinate.

        Args:
            latitude: WGS-84 latitude.
            longitude: WGS-84 longitude.
            name: Optional place name forwarded to name-aware providers.

        Returns:
            ResolvedBoundaries; either polygon may be None when no provider
            found that boundary type.
        """
        resolved = ResolvedBoundaries()
        for provider in self.providers:
            if resolved.complete:
                break
            # A single-kind provider whose slot is already filled has nothing to add;
            # providers overriding get_typed_boundaries may fill either slot.
            single_kind = type(provider).get_typed_boundaries is BoundaryProvider.get_typed_boundaries
            if single_kind and resolved.polygon_for(provider.boundary_kind) is not None:
                continue
            try:
                typed = provider.get_typed_boundaries(latitude, longitude, name=name)
            except Exception:
                # Deliberately broad: boundary providers are plugins running
                # arbitrary gateway code; one failing provider must never
                # abort boundary resolution for the rest.
                logger.exception("Boundary provider %s failed for %s,%s", provider.service_key, redact_coordinate(latitude), redact_coordinate(longitude))
                continue
            if resolved.property_polygon is None:
                resolved.property_polygon = _as_multipolygon(typed.get("property"))
            if resolved.building_polygon is None:
                resolved.building_polygon = _as_multipolygon(typed.get("building"))
        return resolved

    def get_boundary(self, latitude: float, longitude: float, *, name: str | None = None) -> Polygon | MultiPolygon | None:
        """Untyped convenience lookup: the property boundary, else the building one.

        Args:
            latitude: WGS-84 latitude.
            longitude: WGS-84 longitude.
            name: Optional place name forwarded to name-aware providers.

        Returns:
            The best available polygon, or None when nothing was found.
        """
        resolved = self.get_boundaries(latitude, longitude, name=name)
        return resolved.property_polygon or resolved.building_polygon


def boundary_generation_ran(location: Location) -> bool:
    """True when the provider chain has already run for a Location.

    Args:
        location: The Location to check.

    Returns:
        True when the location-default property row exists with ``generated_at`` set.
    """
    from urbanlens.dashboard.models.boundary.model import Boundary, BoundaryType

    row = Boundary.objects.row_for_location(location, BoundaryType.PROPERTY)
    return row is not None and row.generated_at is not None


def schedule_location_boundary_generation(location: Location, profile=None) -> bool:
    """Ensure default-boundary generation is in flight for a Location, single-flight.

    Used by pages that aren't pin-scoped (the wiki page); pin detail pages go
    through the "boundary" panel source, which shares the same generation
    function and idempotence marker.

    Args:
        location: The Location to generate boundaries for.
        profile: The requesting user's profile; generation is skipped when the
            profile has external APIs disabled.

    Returns:
        True when generation is in flight (newly scheduled or already
        running), False when it already ran or is not allowed.
    """
    from django.core.cache import cache

    if location.latitude is None or location.longitude is None:
        return False
    if profile is not None and not profile.external_apis_enabled:
        return False
    if boundary_generation_ran(location):
        return False
    if cache.add(f"ul_boundary_generation_{location.pk}", 1, 600):
        from urbanlens.dashboard.services.celery import safely_enqueue_task
        from urbanlens.dashboard.tasks import generate_boundaries_for_location

        safely_enqueue_task(generate_boundaries_for_location, location.pk)
    return True


def generate_location_boundaries(location: Location, *, name: str | None = None) -> ResolvedBoundaries:
    """Run the provider chain for a Location and persist the generated geometry.

    Writes both location-default Boundary rows (property and building),
    stamping ``generated_at`` even when nothing was found so the chain is not
    re-run on every page view. ``generated_polygon`` is only ever written when
    currently empty, via a queryset ``update()``, so this can never clobber
    geometry landed concurrently.

    The chain's heavy steps (downloading and gunzipping building-footprint
    shards, shapely geometry work) mean this belongs in a Celery worker, never
    on the request path.

    Args:
        location: The Location to generate default boundaries for.
        name: Optional place name hint; defaults to the location's official name.

    Returns:
        The ResolvedBoundaries from the provider chain.
    """
    from urbanlens.dashboard.models.boundary.model import Boundary, BoundaryType

    latitude = float(location.latitude)
    longitude = float(location.longitude)
    resolved = BoundaryProviderChain().get_boundaries(latitude, longitude, name=name or location.official_name or None)
    now = timezone.now()
    for boundary_type in (BoundaryType.PROPERTY, BoundaryType.BUILDING):
        row, _created = Boundary.objects.get_or_create_location_default(location, boundary_type)
        updates: dict = {"generated_at": now, "updated": now}
        polygon = resolved.polygon_for(boundary_type)
        if polygon is not None and row.generated_polygon is None:
            updates["generated_polygon"] = polygon
        Boundary.objects.filter(pk=row.pk).update(**updates)
    return resolved
