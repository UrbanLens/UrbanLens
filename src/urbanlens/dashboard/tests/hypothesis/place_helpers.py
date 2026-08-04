"""Shared helpers for tests that need a location to have official geometry.

Before the Place model, "this location has an official boundary" was written as
a location-default ``Boundary`` row. Official geometry now lives on ``Place``,
and a location *resolves onto* one - so the equivalent setup is "make a place
with this outline, and put this location on it", which is what
:func:`official_geometry` does.

Kept in one place because a dozen test modules need it, and because getting it
subtly wrong (forgetting to re-resolve the locations already inside the new
outline) produces tests that pass for the wrong reason.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from urbanlens.dashboard.models.place.model import Place, PlaceKind, PlaceRelation
from urbanlens.dashboard.services.places import lineage, resolution

if TYPE_CHECKING:
    from django.contrib.gis.geos import MultiPolygon

    from urbanlens.dashboard.models.location.model import Location


def make_place(
    kind: str,
    geometry: MultiPolygon | None,
    *,
    parent: Place | None = None,
    relation: str = PlaceRelation.PART_OF,
    name: str = "",
) -> Place:
    """Create a place with its derived columns filled, as provisioning would."""
    place = Place.objects.create(kind=kind, geometry=geometry, name=name)
    if geometry is not None:
        resolution.refresh_area(place)
    if parent is not None:
        lineage.set_parent(place, parent, relation)
        parent.refresh_from_db()
    return place


def official_geometry(
    location: Location,
    polygon: MultiPolygon,
    *,
    kind: str = PlaceKind.PARCEL,
    parent: Place | None = None,
    relation: str = PlaceRelation.PART_OF,
) -> Place:
    """Give a location official geometry, the way the provider chain would.

    Args:
        location: The location the geometry was fetched for.
        polygon: The official outline.
        kind: What sort of thing the outline describes.
        parent: A containing place, if this is (say) a building on a parcel.
        relation: How it attaches to ``parent``.

    Returns:
        The new place. Every location already standing inside the outline is
        re-resolved onto it, and ``location`` is attached even if the outline
        doesn't strictly contain its point - the chain was asked about that
        coordinate, so its answer applies to it.
    """
    place = make_place(kind, polygon, parent=parent, relation=relation)
    resolution.resolve_locations_in(polygon)
    location.refresh_from_db()
    if location.place_id != place.pk and resolution.resolve_location_place(location) is None:
        resolution.attach_location(location, place)
    return place


def nest_by_containment(place: Place) -> Place:
    """Attach a place to whichever existing place geometrically encloses it.

    Only a test convenience: it reproduces the lineage the provider chain
    builds for real (a footprint ``PART_OF`` the parcel around it) without
    having to spell it out at every call site. The application deliberately
    does *not* infer lineage from geometry - see
    ``services.places.provisioning``.

    Args:
        place: The newly-created place to slot into the hierarchy.

    Returns:
        The same place, re-parented and with any places it encloses pulled
        underneath it.
    """
    if place.geometry is None:
        return place

    container = (
        Place.objects.current()
        .filter(geometry__isnull=False, geometry__contains=place.geometry)
        .exclude(pk=place.pk)
        .order_by("area_sqm", "pk")
        .first()
    )
    if container is not None:
        lineage.set_parent(place, container, PlaceRelation.PART_OF)

    for inner in Place.objects.current().filter(geometry__isnull=False, geometry__within=place.geometry, parent__isnull=True).exclude(pk=place.pk):
        lineage.set_parent(inner, place, PlaceRelation.PART_OF)

    place.refresh_from_db()
    return place
