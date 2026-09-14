"""Geodesic distance helpers for SpotGuessr scoring."""

from __future__ import annotations

from typing import TYPE_CHECKING

from django.contrib.gis.db.models import GeometryField
from django.contrib.gis.db.models.functions import Distance
from django.db.models import Value

from urbanlens.dashboard.models.boundary.queryset import circle_for_coordinates
from urbanlens.dashboard.models.location.model import Location

if TYPE_CHECKING:
    from django.contrib.gis.geos import GEOSGeometry


def location_boundary_polygon(location: Location) -> GEOSGeometry | None:
    """The location's *shared* effective property boundary."""
    from urbanlens.dashboard.services.places.scope import parcel_polygon_for_location

    polygon = parcel_polygon_for_location(location)
    if polygon is not None:
        return polygon
    return circle_for_coordinates(location.latitude, location.longitude)


def geodesic_distance_meters(anchor_location: Location, geometry_a: GEOSGeometry, geometry_b: GEOSGeometry) -> float:
    """Geodesic distance in meters between two geometries (0 when one contains/touches the other).
    ``anchor_location`` only supplies the single database row the annotation runs against - both geometries are passed as literal values, never read from a field, so this works for any point/point, point/polygon, or polygon/polygon pair."""
    result = (
        Location.objects.filter(pk=anchor_location.pk)
        .annotate(
            _sg_distance=Distance(
                Value(geometry_a, output_field=GeometryField(geography=True, srid=4326)),
                Value(geometry_b, output_field=GeometryField(geography=True, srid=4326)),
            ),
        )
        .values_list("_sg_distance", flat=True)
        .first()
    )
    return result.m if result is not None else 0.0
