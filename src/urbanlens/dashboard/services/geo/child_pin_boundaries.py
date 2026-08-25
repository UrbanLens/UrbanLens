"""Automatic property-boundary fallbacks fitted around a pin's children."""

from __future__ import annotations

from django.contrib.gis.geos import MultiPoint, MultiPolygon, Point, Polygon
from django.db import transaction
from django.utils import timezone

from urbanlens.dashboard.models.boundary.model import Boundary, BoundaryType
from urbanlens.dashboard.models.boundary.queryset import buffer_point_by_meters
from urbanlens.dashboard.models.pin.model import Pin

# Enough breathing room to make every marker visibly interior without turning
# a child-derived fallback into a parcel-sized claim beyond the known points.
CHILD_BOUNDARY_PADDING_METERS = 10


def _fitted_polygon(pin: Pin, children: list[Pin]) -> MultiPolygon:
    """Return a padded convex hull around the parent marker and its children."""
    locations = [pin.location, *(child.location for child in children)]
    hull_points: list[Point] = []
    for location in locations:
        point = location.point
        circle = buffer_point_by_meters(point, CHILD_BOUNDARY_PADDING_METERS, latitude=float(location.latitude))
        hull_points.extend(Point(x, y, srid=4326) for x, y in circle.exterior_ring.coords)

    hull = MultiPoint(hull_points, srid=4326).convex_hull
    if not isinstance(hull, Polygon):
        raise TypeError(f"Child-pin boundary hull produced {type(hull).__name__}, not Polygon.")
    return MultiPolygon(hull, srid=4326)


def _provider_outline(pin: Pin) -> MultiPolygon | None:
    """The official property outline a provider has for this pin, if any.

    Args:
        pin: The pin whose place to consult.

    Returns:
        The place's property polygon, or None when no provider has offered one
        (or the place has nothing to say about property boundaries - see
        ``services.places.scope.place_polygon``).
    """
    from urbanlens.dashboard.services.places.scope import place_polygon

    location = pin.location if pin.location_id else None
    place = location.place if (location is not None and location.place_id) else None
    return place_polygon(place, BoundaryType.PROPERTY) if place is not None else None


@transaction.atomic
def refit_child_pin_boundary(parent_pin_id: int | None) -> None:
    """Refit one parent's child-generated fallback after a hierarchy change.

    Existing pin/community drawings and official location boundaries take
    precedence and are never created, updated, or removed here. Once this
    function has created a pin-owned child fallback, only that explicitly
    marked row remains eligible for subsequent automatic updates.

    Args:
        parent_pin_id: Primary key of the parent whose direct children changed.
            ``None`` is a no-op for root-pin saves/deletes.
    """
    if parent_pin_id is None:
        return
    # Serializing by parent prevents concurrent bulk add/delete requests from
    # each fitting against a different half-updated child set.
    parent = Pin.objects.select_for_update().filter(pk=parent_pin_id).first()
    if parent is None:
        return

    row = Boundary.objects.row_for_pin(parent, BoundaryType.PROPERTY)
    if row is not None:
        if row.polygon is not None or not row.generated_from_children:
            return
        if _provider_outline(parent) is not None:
            # A provider has supplied the real outline since this stand-in was
            # fitted, so it can never be chosen again (see
            # ``BoundaryManager.resolve_for_pin``). Dropping it keeps the table
            # honest and stops every hierarchy change refitting a shape nothing
            # will draw. Safe to delete unconditionally here: the branch above
            # has already excluded any row carrying a person's own drawing.
            row.delete()
            return
    else:
        _polygon, source = Boundary.objects.resolve_for_pin(parent, BoundaryType.PROPERTY)
        if source not in {None, "circle"}:
            return

    children = list(Pin.objects.filter(parent_pin=parent).select_related("location"))
    if not children:
        if row is not None:
            row.delete()
        return

    polygon = _fitted_polygon(parent, children)
    if row is None:
        row, created = Boundary.objects.get_or_create(
            pin=parent,
            boundary_type=BoundaryType.PROPERTY,
            defaults={
                "profile": parent.profile,
                "location": parent.location,
                "generated_polygon": polygon,
                "generated_from_children": True,
                "generated_at": timezone.now(),
            },
        )
        if created:
            return
        # A manual boundary may have won the get-or-create race.
        if row.polygon is not None or not row.generated_from_children:
            return

    row.generated_polygon = polygon
    row.generated_at = timezone.now()
    row.location = parent.location
    row.save(update_fields=["generated_polygon", "generated_at", "location", "updated"])
