"""Turning provider geometry into places.

The provider chain answers a *coordinate*: "what parcel and what building
footprint are here?" This module is what makes that answer converge onto one
row per real-world thing instead of one row per person who pinned it.

Two ideas do the work:

**Ask once per place, not once per location.** Before any provider runs,
:func:`ensure_place_for_location` checks whether a known place already contains
the coordinate. On a campus where somebody has already fetched the parcel, the
124th pin costs zero API calls and resolves onto the same parcel row - which is
precisely the duplication that used to give one property 125 copies of its own
outline.

**Match new geometry to existing places before creating any.** A provider key
when the source publishes a stable id, mutual centroid containment otherwise
(see :func:`find_matching_place`) - so a provider nudging a parcel outline
between runs updates the place it already had rather than growing a second one.
"""

from __future__ import annotations

from datetime import timedelta
import logging
from typing import TYPE_CHECKING

from django.db import transaction
from django.utils import timezone

from urbanlens.dashboard.models.place.model import Place, PlaceKind, PlaceRelation
from urbanlens.dashboard.services.places import lineage, resolution

if TYPE_CHECKING:
    from django.contrib.gis.geos import MultiPolygon

    from urbanlens.dashboard.models.location.model import Location

logger = logging.getLogger(__name__)


def geometry_stale(place: Place) -> bool:
    """Whether a place's geometry is older than the site's boundary cache window.

    Args:
        place: The place to check.

    Returns:
        True when the result is older than ``SiteSettings.boundary_cache_days``,
        or when the provider chain has never run for this place at all.

        A null timestamp means the latter and nothing else: ``upsert_place``
        stamps one on both its branches, so the only rows carrying a null are
        those ``0027_places_backfill`` created from pre-places location
        boundaries. Treating those as fresh pinned them to that geometry
        permanently - no provider was ever asked, so a better parcel (REData's,
        say) could only ever be recorded as a losing candidate.
    """
    from urbanlens.dashboard.models.site_settings import SiteSettings

    if place.geometry_generated_at is None:
        return True
    return timezone.now() - place.geometry_generated_at > timedelta(days=SiteSettings.get_current().boundary_cache_days)


def find_matching_place(kind: str, polygon: MultiPolygon, *, provider: str = "", provider_key: str = "") -> Place | None:
    """Find the existing place a freshly-fetched polygon describes, if any.

    Args:
        kind: The :class:`PlaceKind` the polygon describes.
        polygon: The candidate geometry.
        provider: Provider key namespace (e.g. ``"redata"``).
        provider_key: The provider's stable id for this record, when it has one.

    Returns:
        The matching place, or None when this is genuinely new.
    """
    if provider and provider_key:
        if existing := Place.objects.filter(provider=provider, provider_key=provider_key, kind=kind).first():
            return existing

    # No stable id: treat two polygons as the same thing when each contains the
    # other's centroid. Providers disagree about exact edges all the time, and
    # an overlap-fraction threshold would need tuning per kind; mutual centroid
    # containment needs none and cannot match two side-by-side parcels.
    centroid = polygon.centroid
    for candidate in Place.objects.current().of_kind(kind).filter(geometry__isnull=False, geometry__contains=centroid):
        if candidate.geometry is not None and polygon.contains(candidate.geometry.centroid):
            return candidate
    return None


@transaction.atomic
def upsert_place(
    kind: str,
    polygon: MultiPolygon | None,
    *,
    provider: str = "",
    provider_key: str = "",
    name: str = "",
    parent: Place | None = None,
    relation: str = PlaceRelation.PART_OF,
) -> Place | None:
    """Create or update the place a provider record describes.

    Args:
        kind: The :class:`PlaceKind` to create.
        polygon: Official geometry. None creates a geometry-less place, which
            keeps identity and lineage (and can hold a wiki) without ever
            being resolved onto - right for a building nobody has a footprint
            for.
        provider: Provider key namespace.
        provider_key: The provider's stable id, when it has one.
        name: Admin-facing label.
        parent: The containing or aggregating place.
        relation: How it attaches to ``parent``.

    Returns:
        The place, or None when there is neither geometry nor a provider key
        to identify it by.
    """
    if polygon is None and not provider_key:
        return None

    place = find_matching_place(kind, polygon, provider=provider, provider_key=provider_key) if polygon is not None else None
    if place is None and provider and provider_key:
        place = Place.objects.filter(provider=provider, provider_key=provider_key, kind=kind).first()

    now = timezone.now()
    if place is None:
        place = Place(kind=kind, name=name[:255], provider=provider, provider_key=provider_key, geometry=polygon, geometry_generated_at=now)
        place.save()
    else:
        updates: dict = {"geometry_generated_at": now, "updated": now}
        if polygon is not None:
            updates["geometry"] = polygon
        if name and not place.name:
            updates["name"] = name[:255]
        if provider and provider_key and not place.provider_key:
            updates["provider"], updates["provider_key"] = provider, provider_key
        Place.objects.filter(pk=place.pk).update(**updates)
        place.refresh_from_db()

    if polygon is not None:
        resolution.refresh_area(place)
    if parent is not None and place.parent_id != parent.pk:
        lineage.set_parent(place, parent, relation)
    return place


def ensure_place_for_location(location: Location, *, name: str | None = None, force: bool = False) -> Place | None:
    """Resolve a Location onto a place, provisioning geometry only if needed.

    Args:
        location: The Location to place.
        name: Optional place-name hint forwarded to name-aware providers.
        force: Re-run the provider chain even when the coordinate already
            resolves onto a fresh place.

    Returns:
        The resolved place, or None when no provider knows this coordinate.
    """
    if location.latitude is None or location.longitude is None:
        return None

    existing = Place.objects.resolve_for_point(location.latitude, location.longitude)
    if existing is not None and not force and not geometry_stale(existing):
        resolution.attach_location(location, existing)
        return existing

    return provision_places_for_coordinate(location, name=name)


def provision_places_for_coordinate(location: Location, *, name: str | None = None) -> Place | None:
    """Run the provider chain for a coordinate and persist the places it describes.

    The parcel and the building footprint become *separate* places, the
    building ``PART_OF`` the parcel. That separation is what lets a building's
    page draw its own footprint instead of the grounds it stands on, while
    keeping both in one access domain.

    Args:
        location: The Location whose coordinate to resolve.
        name: Optional place-name hint forwarded to name-aware providers.

    Returns:
        The most specific place now covering the coordinate, or None.
    """
    from urbanlens.dashboard.services.locations.boundaries import BoundaryProviderChain

    latitude, longitude = float(location.latitude), float(location.longitude)
    resolved = BoundaryProviderChain().get_boundaries(latitude, longitude, name=name or location.official_name or None)

    detect_subdivision(location, resolved.property_polygon)

    parcel = upsert_place(PlaceKind.PARCEL, resolved.property_polygon, name=name or location.official_name or "")
    building = None
    if resolved.building_polygon is not None:
        building = upsert_place(PlaceKind.BUILDING, resolved.building_polygon, name="", parent=parcel)

    if parcel is not None:
        record_boundary_candidates(parcel, resolved.property_candidates)

    # A newly-arrived parcel or footprint changes the answer for every
    # coordinate under it, not just this one - a building footprint appearing
    # inside a parcel re-homes every pin standing on that building.
    for place in (parcel, building):
        if place is not None and place.geometry is not None:
            resolution.resolve_locations_in(place.geometry)

    standing_on = resolution.resolve_location_place(location)
    if standing_on is None and (building or parcel) is not None:
        # The chain was asked about *this* coordinate, so its answer applies to
        # it even when the outline it returned doesn't strictly contain the
        # point - providers do disagree with themselves by a metre or two, and
        # discarding geometry we just paid for would leave the location looking
        # unknown until the next refresh made the same mistake again.
        standing_on = building or parcel
        resolution.attach_location(location, standing_on)
    return standing_on


def detect_subdivision(location: Location, new_parcel_polygon: MultiPolygon | None) -> Place | None:
    """Retire the parcel this coordinate used to be on, if it has been divided.

    Only the shrink case is detected from a single coordinate: the provider now
    returns a much smaller parcel here than the one on record, which means the
    rest of the old parcel became something else. The successor outlines are
    whatever the *other* pinned coordinates inside the old boundary now resolve
    to, which is exactly the set that matters - a piece nobody has pinned needs
    no place until somebody does.

    Args:
        location: The coordinate whose refresh triggered this.
        new_parcel_polygon: The parcel outline the providers now return here.

    Returns:
        The superseded place, or None when nothing was subdivided.
    """
    from urbanlens.dashboard.models.location.model import Location as LocationModel
    from urbanlens.dashboard.services.locations.boundaries import BoundaryProviderChain
    from urbanlens.dashboard.services.places import splits

    previous = location.place.parcel if (location.place_id and location.place is not None) else None
    if previous is None or new_parcel_polygon is None or not splits.looks_like_a_split(previous, new_parcel_polygon):
        return None

    successors = [new_parcel_polygon]
    for other in LocationModel.objects.filter(place__domain_root_id=previous.domain_root_id).exclude(pk=location.pk).exclude(point__within=new_parcel_polygon):
        chain_result = BoundaryProviderChain().get_boundaries(float(other.latitude), float(other.longitude), name=other.official_name or None)
        if chain_result.property_polygon is not None and not any(candidate.equals(chain_result.property_polygon) for candidate in successors):
            successors.append(chain_result.property_polygon)

    if len(successors) < 2:
        return None
    return splits.process_split(previous, successors)


def ensure_building_places(parcel: Place | None, buildings: list[dict], *, provider: str = "") -> dict[int, Place]:
    """Create the building places a bulk import already has the footprints for.

    The import knows exactly which structures it is about - it fetched the
    parcel's building list to offer them. Persisting those footprints directly
    is both cheaper and *more correct* than letting each new child pin's
    coordinate be looked up on its own: a point lookup for a building returns
    the parcel when no footprint provider covers it, which is how 124 markers
    ended up each claiming to be the whole hospital.

    A building with no footprint still gets a place, with null geometry. It
    keeps identity, lineage, and the ability to hold its own wiki, and can
    never be resolved onto - which is the right answer for a structure we know
    exists but cannot locate precisely.

    Args:
        parcel: The parcel these buildings stand on; None skips the whole step,
            since a building place with no parcel has no domain to join.
        buildings: Cached building records (see ``plugins.builtin.parcel_buildings``).
        provider: Provider namespace for stable-id matching.

    Returns:
        Dict mapping each record's index in ``buildings`` to its place.
    """
    from urbanlens.dashboard.services.pins.pin_restructure import building_footprint, building_name

    if parcel is None:
        return {}

    created: dict[int, Place] = {}
    for index, building in enumerate(buildings):
        footprint = building_footprint(building)
        key = str(building.get("uuid") or building.get("id") or building.get("building_number") or "").strip()
        place = upsert_place(
            PlaceKind.BUILDING,
            _as_multipolygon(footprint),
            provider=provider if key else "",
            provider_key=key,
            name=building_name(building),
            parent=parcel,
            relation=PlaceRelation.PART_OF,
        )
        if place is not None:
            created[index] = place

    lineage.refresh_derived_flags([parcel.pk])
    parcel.refresh_from_db()
    for place in created.values():
        if place.geometry is not None:
            resolution.resolve_locations_in(place.geometry)
    return created


def _as_multipolygon(geometry):
    """Normalize a polygonal geometry to MultiPolygon, or None."""
    from django.contrib.gis.geos import MultiPolygon, Polygon

    if geometry is None:
        return None
    if isinstance(geometry, Polygon):
        return MultiPolygon(geometry, srid=geometry.srid or 4326)
    return geometry if isinstance(geometry, MultiPolygon) else None


def record_boundary_candidates(place: Place, candidates: list[tuple[str, MultiPolygon]]) -> None:
    """Persist one votable candidate row per provider that offered a parcel outline.

    Candidate geometry refreshes freely: it is never user-drawn, so a newer
    provider answer can only be better data. The winning candidate is
    materialised onto ``Place.geometry`` by
    ``services.geo.boundary_voting.apply_winning_boundary``.

    Args:
        place: The parcel place the candidates describe.
        candidates: ``(provider service_key, polygon)`` pairs from the chain.
    """
    from urbanlens.dashboard.models.boundary.model import Boundary, BoundaryType
    from urbanlens.dashboard.services.geo.boundary_voting import apply_winning_boundary
    from urbanlens.dashboard.services.locations.boundaries import PROVIDER_BOUNDARY_SOURCES

    now = timezone.now()
    for service_key, polygon in candidates:
        source = PROVIDER_BOUNDARY_SOURCES.get(service_key)
        if source is None:
            continue
        candidate, _created = Boundary.objects.get_or_create(
            place=place,
            boundary_type=BoundaryType.PROPERTY,
            source=source,
            location=None,
            pin=None,
            wiki=None,
            profile=None,
            defaults={"generated_polygon": polygon, "generated_at": now},
        )
        Boundary.objects.filter(pk=candidate.pk).update(generated_polygon=polygon, generated_at=now, updated=now)

    apply_winning_boundary(place)
