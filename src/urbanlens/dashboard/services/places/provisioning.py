"""
Turning provider geometry into places.
The provider chain answers a *coordinate*: "what parcel and what building footprint are here?" This module is what makes that answer converge onto one row per real-world thing instead of one row per person who pinned it."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import timedelta
import logging
from typing import TYPE_CHECKING

from django.contrib.gis.db.models.functions import Distance
from django.contrib.gis.geos import GEOSException
from django.db import transaction
from django.db.models import Q
from django.utils import timezone

from urbanlens.dashboard.models.place.model import STRONG_EVIDENCE, Place, PlaceEvidence, PlaceKind, PlaceRelation, PlaceStatus, is_plausible_area
from urbanlens.dashboard.models.place.queryset import FOOTPRINT_BUFFER_METERS
from urbanlens.dashboard.services.geo.area import area_sqm, buffer_meters
from urbanlens.dashboard.services.places import geometry_versions, lineage, resolution

if TYPE_CHECKING:
    from django.contrib.gis.geos import MultiPolygon, Point

    from urbanlens.dashboard.models.location.model import Location
    from urbanlens.dashboard.services.locations.boundaries import ResolvedBoundaries

logger = logging.getLogger(__name__)

#: Upstream chain runs one split may spend finding successors. A location left unprobed keeps working through the
#: superseded parcel's domain and finds its own parcel on its next refresh.
MAX_SUBDIVISION_PROBES = 20

#: Boundary-provider keys whose building outlines are machine-learned footprints rather than surveyed or registry
#: geometry. Every other named source (OSM, registry records) is official.
FOOTPRINT_SOURCES: frozenset[str] = frozenset({"microsoft_building_footprints", "overture", "overture_maps", "google_open_buildings"})


def evidence_for_source(source: str) -> str:
    """
    The :class:`PlaceEvidence` a building outline from provider ``source`` carries.

    A blank ``source`` means nobody attributed the outline: it is not known to be surveyed, so it counts as footprint, never official.
    """
    return PlaceEvidence.OFFICIAL if source and source not in FOOTPRINT_SOURCES else PlaceEvidence.FOOTPRINT


def evidence_for_record(building: dict) -> str:
    """
    The :class:`PlaceEvidence` of a cached building record, by its first-listed source (the one the record's geometry comes from).

    Overture records carry no per-feature dataset here, and Overture mixes machine-derived footprints with OSM, so a record led by Overture counts as footprint.
    County GIS, assessor, CRIS and OSM records are official; a record naming no source is footprint (see :func:`evidence_for_source`).
    """
    from urbanlens.dashboard.plugins.builtin.parcel_buildings import record_sources

    sources = record_sources(building)
    return evidence_for_source(str(sources[0]) if sources else "")


def geometry_stale(place: Place) -> bool:
    """
    Whether a place's geometry is older than the site's boundary cache window.

    Args:
        place: The place to check.

    Returns:
        True when the result is older than ``SiteSettings.boundary_cache_days``, or when the provider chain has never run for this place at all."""
    from urbanlens.dashboard.models.site_settings import SiteSettings

    if place.geometry_generated_at is None:
        return True
    return timezone.now() - place.geometry_generated_at > timedelta(days=SiteSettings.get_current().boundary_cache_days)


def find_matching_place(kind: str, polygon: MultiPolygon, *, provider: str = "", provider_key: str = "", exclude_pk: int | None = None) -> Place | None:
    """
    Find the existing place a freshly-fetched polygon describes, if any.

    Args:
        kind: The :class:`PlaceKind` the polygon describes.
        polygon: The candidate geometry.
        provider: Provider key namespace (e.g. ``"osm"``).
        provider_key: The provider's stable id for this record, when it has one.
        exclude_pk: A place to never match, however well its geometry fits - for a parcel being split, its own (still-``CURRENT`` until the split transaction commits) centroid very often lands inside one of its own successors, and without this a successor's upsert...
    """
    if provider and provider_key:
        if existing := Place.objects.filter(provider=provider, provider_key=provider_key, kind=kind).exclude(pk=exclude_pk).first():
            return existing

    # No stable id match: treat two polygons as the same thing when each contains the other's
    # centroid.
    # Providers disagree about exact edges all the time, and an overlap-fraction threshold would
    # need tuning per kind; mutual centroid containment needs none and cannot match two side-by-side
    centroid = polygon.centroid
    centroid_holders = Place.objects.current().of_kind(kind).filter(geometry__isnull=False, geometry__contains=centroid, evidence__in=STRONG_EVIDENCE).exclude(pk=exclude_pk)
    # A keyed place is the better home for a keyless record, so look at it first.
    keyed = bool(provider and provider_key)
    for candidate in sorted(centroid_holders, key=lambda place: not place.provider_key):
        # Two records that each carry a provider id are two things, whatever the geometry says: the
        # provider (or two providers) has told us so. This matters most for nested buildings - a
        # source that reconciles a block and the wings inside it into separate records on purpose, and an
        # L-shaped block can contain a wing's centroid.
        if keyed and candidate.provider_key:
            continue
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
    exclude_pk: int | None = None,
    evidence: str = PlaceEvidence.OFFICIAL,
    detect_splits: bool = True,
) -> Place | None:
    """
    Create or update the place a provider record describes.

    Args:
        kind: The :class:`PlaceKind` to create.
        polygon: Official geometry.
        provider: Provider key namespace.
        provider_key: The provider's stable id, when it has one.
        name: Admin-facing label.
        parent: The containing or aggregating place.
        relation: How it attaches to ``parent``.
        exclude_pk: Passed through to :func:`find_matching_place` - a place that must never be matched, however well its geometry fits.
        evidence: How strong the polygon is. A footprint matched onto an existing place never replaces its geometry
            unless it has a parent, so a machine-derived outline cannot overwrite an official one or a reconciled sibling; an
            official polygon upgrades a matched footprint place.
        detect_splits: For a newly created root parcel, look in the older outlines of nearby parcels for a split it completes.

    Returns:
        The place, or None when there is neither plausible geometry nor a provider key to identify it by."""
    if polygon is not None and not is_plausible_area(kind, area := area_sqm(polygon)):
        logger.warning("Refused a %s outline of %.1f km² as implausible", kind, area / 1_000_000)
        polygon = None
    if polygon is None and not provider_key:
        return None

    place = find_matching_place(kind, polygon, provider=provider, provider_key=provider_key, exclude_pk=exclude_pk) if polygon is not None else None
    if place is None and provider and provider_key:
        place = Place.objects.filter(provider=provider, provider_key=provider_key, kind=kind).exclude(pk=exclude_pk).first()

    now = timezone.now()
    created = place is None
    footprint_keeps_geometry = False
    if place is None:
        place = Place(kind=kind, name=(name or "")[:255], provider=provider, provider_key=provider_key, geometry=polygon, geometry_generated_at=now, valid_from=now, evidence=evidence)
        place.save()
        if polygon is not None:
            geometry_versions.record_version(place, polygon, provider=provider, recorded_at=now)
    else:
        updates: dict = {"geometry_generated_at": now, "updated": now}
        footprint_keeps_geometry = polygon is not None and evidence == PlaceEvidence.FOOTPRINT and (place.evidence != PlaceEvidence.FOOTPRINT or parent is None)
        if polygon is not None and not footprint_keeps_geometry:
            geometry_versions.set_geometry(place, polygon, provider=provider, recorded_at=now)
            if evidence == PlaceEvidence.OFFICIAL and place.evidence == PlaceEvidence.FOOTPRINT:
                updates["evidence"] = PlaceEvidence.OFFICIAL
        if name and not place.name:
            updates["name"] = name[:255]
        if provider and provider_key and not place.provider_key:
            updates["provider"], updates["provider_key"] = provider, provider_key
        Place.objects.filter(pk=place.pk).update(**updates)
        place.refresh_from_db()

    if polygon is not None and not footprint_keeps_geometry:
        resolution.refresh_area(place)
    if parent is not None and place.parent_id != parent.pk:
        nested_footprint = place.evidence == PlaceEvidence.FOOTPRINT and place.parent_id is None
        lineage.set_parent(place, parent, relation)
        if nested_footprint:
            # TODO(UL-369): notify the members whose footprint wiki now sits in the parcel's domain.
            logger.info("footprint place nested under parcel", extra={"event": "place.footprint_nested", "place_id": place.pk, "parent_id": parent.pk})
    if created and detect_splits and kind == PlaceKind.PARCEL and parent is None and polygon is not None:
        from urbanlens.dashboard.services.places import splits

        splits.detect_split_from_versions(place)
        place.refresh_from_db()
    return place


@dataclass(slots=True)
class ProvisionOutcome:
    """
    What one resolution of a coordinate produced.

    Attributes:
        place: The most specific place now covering the coordinate, or None.
        deferred: Providers that declined for now rather than answering.
        retry_after: The longest wait a deferring provider asked for, in seconds.
        backs_off: Whether a retry should back off; False when every deferring provider was computing its answer.
    """

    place: Place | None = None
    deferred: list[str] = field(default_factory=list)
    retry_after: int | None = None
    backs_off: bool = True


def ensure_place_for_location(location: Location, *, name: str | None = None, force: bool = False, detect_splits: bool = True) -> Place | None:
    """
    Resolve a Location onto a place, provisioning geometry only if needed.

    Args:
        location: The Location to place.
        name: Optional place-name hint forwarded to name-aware providers.
        force: Re-run the provider chain even when the coordinate already resolves onto a fresh place.
        detect_splits: Probe for a subdivision when the parcel shrinks; a caller that re-homes the old outline's locations itself can skip it.

    Returns:
        The resolved place, or None when no provider knows this coordinate."""
    return ensure_place_outcome(location, name=name, force=force, detect_splits=detect_splits).place


def ensure_place_outcome(location: Location, *, name: str | None = None, force: bool = False, detect_splits: bool = True) -> ProvisionOutcome:
    """
    :func:`ensure_place_for_location`, also reporting which providers deferred.

    Args:
        location: The Location to place.
        name: Optional place-name hint forwarded to name-aware providers.
        force: Re-run the provider chain even when the coordinate already resolves onto a fresh place.
        detect_splits: Probe for a subdivision when the parcel shrinks.

    Returns:
        The outcome; ``deferred`` is empty when the answer came from known geometry."""
    if location.latitude is None or location.longitude is None:
        return ProvisionOutcome()

    existing = Place.objects.resolve_for_point(location.latitude, location.longitude)
    if existing is not None and not force and not geometry_stale(existing):
        resolution.attach_location(location, existing)
        return ProvisionOutcome(place=existing)

    return provision_outcome_for_coordinate(location, name=name, detect_splits=detect_splits)


def provision_places_for_coordinate(location: Location, *, name: str | None = None, detect_splits: bool = True) -> Place | None:
    """
    Run the provider chain for a coordinate and persist the places it describes.
    That separation is what lets a building's page draw its own footprint instead of the grounds it stands on, while keeping both in one access domain.

    Args:
        location: The Location whose coordinate to resolve.
        name: Optional place-name hint forwarded to name-aware providers.
        detect_splits: Probe for a subdivision when the parcel shrinks.

    Returns:
        The most specific place now covering the coordinate, or None."""
    return provision_outcome_for_coordinate(location, name=name, detect_splits=detect_splits).place


def provision_outcome_for_coordinate(location: Location, *, name: str | None = None, detect_splits: bool = True) -> ProvisionOutcome:
    """
    :func:`provision_places_for_coordinate`, also reporting which providers deferred.

    Args:
        location: The Location whose coordinate to resolve.
        name: Optional place-name hint forwarded to name-aware providers.
        detect_splits: Probe for a subdivision when the parcel shrinks.
    """
    from urbanlens.dashboard.services.locations.boundaries import BoundaryProviderChain

    latitude, longitude = float(location.latitude), float(location.longitude)
    resolved = BoundaryProviderChain().get_boundaries(latitude, longitude, name=name or location.official_name or None)

    if resolved.deferred and (known := Place.objects.resolve_for_point(location.latitude, location.longitude)) is not None:
        # A fallback's outline must not overwrite one the deferring provider gave earlier; keep it until that provider answers.
        if not _parcel_covers(location):
            kept = ensure_footprint_places([(source, outline) for source, outline in _building_outlines(resolved) if not _would_overwrite(source, outline)])
            for place in kept:
                _resolve_locations_around(place)
            known = Place.objects.resolve_for_point(location.latitude, location.longitude) or known
        resolution.attach_location(location, known)
        return ProvisionOutcome(place=known, deferred=list(resolved.deferred), retry_after=resolved.retry_after, backs_off=resolved.backs_off)

    if detect_splits and not resolved.deferred:
        detect_subdivision(location, resolved.property_polygon, source=resolved.property_source)

    parcel = upsert_place(PlaceKind.PARCEL, resolved.property_polygon, name=name or location.official_name or "", detect_splits=detect_splits)
    building = None
    footprints: list[Place] = []
    if resolved.building_polygon is not None:
        if parcel is None and not _parcel_covers(location):
            # Stored, not bound: the pin reaches one of these only through resolution below, which allows the buffer and no further.
            footprints = ensure_footprint_places(_building_outlines(resolved))
        else:
            # No parcel came back this time, but a stored one may hold the point: nest under it so the building stays in its access domain.
            container = parcel or _covering_parcel(location.point)
            building = upsert_place(PlaceKind.BUILDING, resolved.building_polygon, name="", parent=container, evidence=evidence_for_source(_building_source(resolved)))

    if parcel is not None:
        record_boundary_candidates(parcel, resolved.property_candidates)

    # A newly-arrived parcel or footprint changes the answer for every
    # coordinate under it, not just this one - a building footprint appearing
    # inside a parcel re-homes every pin standing on that building.
    for touched in (parcel, building, *footprints):
        _resolve_locations_around(touched)

    standing_on = resolution.resolve_location_place(location)
    if standing_on is None and (building or parcel) is not None:
        # The chain was asked about *this* coordinate, so its answer applies to it even when the
        # outline it returned doesn't strictly contain the point - providers do disagree with
        # themselves by a metre or two, and discarding geometry we just paid for would leave the
        # location looking unknown until the next refresh made the same mistake again.
        standing_on = building or parcel
        resolution.attach_location(location, standing_on)
    return ProvisionOutcome(place=standing_on, deferred=list(resolved.deferred), retry_after=resolved.retry_after, backs_off=resolved.backs_off)


def _probe_neighbours(location: Location, previous: Place, new_parcel_polygon: MultiPolygon, known: list[MultiPolygon], source: str) -> list[MultiPolygon]:
    """
    Ask the provider chain about the domain's locations that fall outside the new outline, at most :data:`MAX_SUBDIVISION_PROBES` times.

    A probed outline answers to the rules :func:`~urbanlens.dashboard.services.places.splits.candidate_parcels` holds
    stored parcels to: drawn by ``source``, :func:`~urbanlens.dashboard.services.places.splits.is_remainder_piece`,
    and :func:`~urbanlens.dashboard.services.places.splits.is_safe_outline`.
    With no ``source`` there is nothing to match against, so the chain's selected outline stands in.
    """
    from urbanlens.dashboard.models.location.model import Location as LocationModel
    from urbanlens.dashboard.services.locations.boundaries import BoundaryProviderChain
    from urbanlens.dashboard.services.places import splits

    remainder = splits.remainder_of(previous, new_parcel_polygon)
    if remainder is None:
        return []
    found: list[MultiPolygon] = []
    probes = 0
    # Filtering on a null root would be IS NULL, matching every rootless place and every placeless location.
    if previous.domain_root_id is None:
        in_domain = Q(place=previous) | Q(place__parent=previous, place__parent_relation=PlaceRelation.PART_OF)
    else:
        in_domain = Q(place__domain_root_id=previous.domain_root_id)
    neighbours = LocationModel.objects.filter(in_domain).exclude(pk=location.pk).exclude(point__within=new_parcel_polygon).annotate(distance=Distance("point", location.point)).order_by("distance")
    for other in neighbours.iterator():
        if any(successor.contains(other.point) for successor in [*known, *found]):
            continue
        if probes >= MAX_SUBDIVISION_PROBES:
            break
        probes += 1
        chain_result = BoundaryProviderChain().get_boundaries(float(other.latitude), float(other.longitude), name=other.official_name or None)
        if source:
            polygon = next((candidate for service_key, candidate in chain_result.property_candidates if service_key == source), None)
        else:
            polygon = chain_result.property_polygon
        if polygon is None or not splits.is_remainder_piece(polygon, new_parcel_polygon, remainder):
            continue
        if any(candidate.equals(polygon) for candidate in [*known, *found]):
            continue
        if not splits.is_safe_outline(previous, polygon):
            continue
        found.append(polygon)
    return found


def detect_subdivision(location: Location, new_parcel_polygon: MultiPolygon | None, *, source: str = "") -> Place | None:
    """
    Only the shrink case is detected from a single coordinate: the provider now returns a much smaller parcel here than the one on record, which means the rest of the old parcel became something else.

    Args:
        location: The coordinate whose refresh triggered this.
        new_parcel_polygon: The parcel outline the providers now return here.
        source: The provider that drew that outline, when known. Every outline a split is built from has to come from one provider.

    Returns:
        The superseded place, or None when nothing was subdivided, including when an outline is too broken for GEOS
        to compare: the refresh goes on without split detection rather than failing."""
    try:
        return _detect_subdivision(location, new_parcel_polygon, source)
    except GEOSException:
        logger.warning("split detection skipped for location %s: GEOS could not compare its outlines", location.pk, exc_info=True)
        return None


def _detect_subdivision(location: Location, new_parcel_polygon: MultiPolygon | None, source: str) -> Place | None:
    """:func:`detect_subdivision`, letting GEOS errors through."""
    from urbanlens.dashboard.services.places import splits

    previous = location.place.parcel if (location.place_id and location.place is not None) else None
    if previous is None or new_parcel_polygon is None:
        return None
    # Another provider drawing this parcel smaller is a disagreement between sources, not a shrink.
    # Place.provider is a key namespace, so who drew the outline is read from its versions.
    recorded = geometry_versions.outline_source(previous)
    if recorded and source and recorded != source:
        return None
    # The provider chain records its outlines unnamed, so the refresh is usually the only source to hold probes to.
    source = recorded or source
    ratio = splits.shrink_ratio(previous, new_parcel_polygon)
    heavy = ratio is not None and ratio < (1 - splits.SPLIT_SHRINK_RATIO)
    # A mild shrink only counts once the missing part turns up as another parcel; a stored one is enough.
    if not heavy and not splits.is_mild_shrink(previous, new_parcel_polygon):
        return None

    # Stored parcels first: a location one of them covers needs no provider asked about it.
    successors = [new_parcel_polygon, *splits.candidate_parcels(previous, new_parcel_polygon)]
    if heavy or not splits.looks_like_a_split(previous, new_parcel_polygon):
        probed = _probe_neighbours(location, previous, new_parcel_polygon, successors, source)
        if not heavy and not splits.looks_like_a_split(previous, new_parcel_polygon, seen=probed):
            return None
        successors += probed
    if len(successors) < 2:
        return None
    return previous if splits.process_split(previous, successors).status == PlaceStatus.SUPERSEDED else None


def ensure_building_places(parcel: Place | None, buildings: list[dict], *, provider: str = "") -> dict[int, Place]:
    """
    Create the building places a bulk import already has the footprints for.
    It keeps identity, lineage, and the ability to hold its own wiki, and can never be resolved onto - which is the right answer for a structure we know exists but cannot locate precisely.

    Args:
        parcel: The parcel these buildings stand on. With None, a record whose outline lies on a stored parcel or site is
            provisioned under that one, and every other record that has an outline becomes a top-level footprint place
            (see :func:`ensure_footprint_places`).
        buildings: Cached building records (see ``services.places.building_records``).
        provider: Provider namespace for stable-id matching.

    Returns:
        Dict mapping each record's index in ``buildings`` to its place."""
    from urbanlens.dashboard.services.pins.pin_restructure import building_footprint, building_name
    from urbanlens.dashboard.services.places.building_records import building_key, place_provider

    if parcel is None:
        return _footprints_without_parcel(buildings, provider=provider)
    source = place_provider(provider)
    rekey = source.place_keyer() if source is not None else None

    # A reconciled record reports nesting: a coarse footprint enclosing finer ones becomes
    # their `parent_ref` rather than a duplicate of them.
    # Parenting every building to the parcel regardless made an envelope and the wings inside it
    by_ref: dict[str, int] = {}
    for index, building in enumerate(buildings):
        ref = str(building.get("ref") or "").strip()
        if ref and ref not in by_ref:
            by_ref[ref] = index

    def _parent_index(index: int) -> int | None:
        """The index this building nests under, or None for parcel-level."""
        parent_ref = str(buildings[index].get("parent_ref") or "").strip()
        candidate = by_ref.get(parent_ref) if parent_ref else None
        return candidate if candidate is not None and candidate != index else None

    def _key(building: dict) -> str:
        """The building's identity: global ids as they are, a building number only within this parcel."""
        # "stable_ref" (else "ref") is the record's id and doubles as the key floorplan lookups use - prefer it.
        if ref := building_key(building) or str(building.get("uuid") or building.get("id") or "").strip():
            return ref
        if osm_id := building.get("osm_id"):
            # The ref a source of reconciled records gives an OSM building, so its later answers find this place.
            return f"osm:{building.get('osm_type') or 'way'}/{osm_id}"
        if number := str(building.get("building_number") or "").strip():
            return f"parcel:{parcel.pk}:{number}"
        return ""

    def _adopt_legacy_number_key(key: str) -> None:
        """Rekey this parcel's place filed under the bare building number, so it is updated rather than forked."""
        number = key.removeprefix(f"parcel:{parcel.pk}:")
        if number == key or Place.objects.filter(provider=provider, provider_key=key, kind=PlaceKind.BUILDING).exists():
            return
        Place.objects.filter(provider=provider, provider_key=number, kind=PlaceKind.BUILDING, parent=parcel).update(provider_key=key)

    def _create(index: int, parent_place: Place) -> None:
        building = buildings[index]
        footprint = _as_multipolygon(building_footprint(building))
        key = _key(building)
        if rekey is not None and key:
            key = rekey(building, footprint, key)
        _adopt_legacy_number_key(key)
        place = upsert_place(
            PlaceKind.BUILDING,
            footprint,
            provider=provider if key else "",
            provider_key=key,
            name=building_name(building),
            parent=parent_place,
            relation=PlaceRelation.PART_OF,
            evidence=evidence_for_record(building),
        )
        if place is not None:
            created[index] = place

    created: dict[int, Place] = {}
    # Parents before children, to whatever depth the source reports (a campus block parenting a wing
    # parenting an annex).
    # Resolved by repeated passes rather than recursion so a `parent_ref` cycle - which
    # `parcel_buildings` already guards against in its own ordering - cannot recurse forever.
    pending = list(range(len(buildings)))
    while pending:
        deferred: list[int] = []
        progressed = False
        for index in pending:
            parent_index = _parent_index(index)
            if parent_index is None:
                _create(index, parcel)
                progressed = True
            elif parent_index in created:
                _create(index, created[parent_index])
                progressed = True
            elif parent_index in pending:
                deferred.append(index)
            else:
                # Its parent produced no place (no geometry, or upsert declined).
                # The parcel is the nearest real ancestor.
                _create(index, parcel)
                progressed = True
        if not progressed:
            # Every remaining building is waiting on another that is also
            # waiting - a cycle. Attach them to the parcel rather than dropping
            # buildings the import told the user it would create.
            for index in deferred:
                _create(index, parcel)
            break
        pending = deferred

    lineage.refresh_derived_flags([parcel.pk])
    parcel.refresh_from_db()
    for place in created.values():
        if place.geometry is not None:
            resolution.resolve_locations_in(place.geometry)
    return created


def _resolve_locations_around(place: Place | None) -> None:
    """Re-resolve the locations a place can claim: those in its outline, plus the buffer ring when it is a parcel-less footprint."""
    if place is None or place.geometry is None:
        return
    reach = buffer_meters(place.geometry, FOOTPRINT_BUFFER_METERS) if place.evidence == PlaceEvidence.FOOTPRINT and place.parent_id is None else place.geometry
    resolution.resolve_locations_in(reach)


def _covering_parcel(point: Point) -> Place | None:
    """The most specific stored, resolvable parcel or site holding ``point``, or None."""
    return Place.objects.containing_point(point).filter(kind__in=(PlaceKind.PARCEL, PlaceKind.SITE)).first()


def _parcel_covers(location: Location) -> bool:
    """Whether a stored, resolvable parcel or site already holds the location's point."""
    return _covering_parcel(location.point) is not None


def _parcel_under(outline: MultiPolygon) -> Place | None:
    """The stored parcel or site a building outline stands on, judged at a point inside the outline, or None."""
    try:
        point = outline.point_on_surface
    except GEOSException:
        return None
    point.srid = point.srid or 4326
    return _covering_parcel(point)


def _building_outlines(resolved: ResolvedBoundaries) -> list[tuple[str, MultiPolygon]]:
    """
    Every building outline the chain returned, as ``(source, outline)`` in chain order, ``building_polygon`` first.

    An outline with no attributed source has a blank source, so it is never taken for another provider's.
    """
    outlines = list(resolved.building_candidates)
    if resolved.building_polygon is not None and not any(outline == resolved.building_polygon for _source, outline in outlines):
        outlines.insert(0, ("", resolved.building_polygon))
    return outlines


def _building_source(resolved: ResolvedBoundaries) -> str:
    """The provider ``building_polygon`` came from; blank when no provider is attributed to that outline."""
    return next((source for source, outline in _building_outlines(resolved) if outline == resolved.building_polygon), "")


def _would_overwrite(source: str, outline: MultiPolygon) -> bool:
    """Whether storing this outline would replace a stored place's geometry; only an official one can, and only by matching that place."""
    return evidence_for_source(source) != PlaceEvidence.FOOTPRINT and find_matching_place(PlaceKind.BUILDING, outline) is not None


def ensure_footprint_places(candidates: list[tuple[str, MultiPolygon]]) -> list[Place]:
    """
    Create the top-level building places for outlines found where no parcel exists.

    Outlines of one building from several sources reconcile by mutual centroid containment (see
    :func:`find_matching_place`), so the first source in ``candidates`` wins and later duplicates add nothing.
    The true outline is stored; resolution reaches :data:`FOOTPRINT_BUFFER_METERS` past it only while no place
    contains the point (``PlaceQuerySet.footprints_near``). An implausibly large outline is refused. An outline
    that stands on a stored parcel or site nests under it instead, so it stays in that place's access domain.

    Args:
        candidates: ``(provider service_key, outline)`` pairs, in chain order.

    Returns:
        The places, one per distinct building.
    """
    return _footprint_places([(evidence_for_source(source), outline) for source, outline in candidates])


def _footprint_places(outlines: list[tuple[str, MultiPolygon]]) -> list[Place]:
    """:func:`ensure_footprint_places` for outlines whose evidence is already decided."""
    places: list[Place] = []
    for evidence, outline in outlines:
        if not is_plausible_area(PlaceKind.BUILDING, area_sqm(outline)):
            continue
        place = upsert_place(PlaceKind.BUILDING, outline, parent=_parcel_under(outline), evidence=evidence)
        if place is not None and all(place.pk != other.pk for other in places):
            places.append(place)
    return places


def _footprints_without_parcel(buildings: list[dict], *, provider: str = "") -> dict[int, Place]:
    """
    :func:`ensure_building_places` with no parcel given, keyed by each record's index in ``buildings``.

    A record standing on a stored parcel or site goes through the parcel path under that place; the rest become
    top-level footprint places.
    """
    from urbanlens.dashboard.services.pins.pin_restructure import building_footprint

    created: dict[int, Place] = {}
    on_parcel: dict[int, tuple[Place, list[int]]] = {}
    for index, building in enumerate(buildings):
        outline = _as_multipolygon(building_footprint(building))
        if outline is None:
            continue
        if (container := _parcel_under(outline)) is not None:
            on_parcel.setdefault(container.pk, (container, []))[1].append(index)
        elif places := _footprint_places([(evidence_for_record(building), outline)]):
            created[index] = places[0]
    for place in created.values():
        _resolve_locations_around(place)
    for container, indexes in on_parcel.values():
        nested = ensure_building_places(container, [buildings[index] for index in indexes], provider=provider)
        created.update((indexes[position], place) for position, place in nested.items())
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
    """
    Persist one votable candidate row per provider that offered a parcel outline.
    Candidate geometry refreshes freely: it is never user-drawn, so a newer provider answer can only be better data.

    Args:
        place: The parcel place the candidates describe.
        candidates: ``(provider service_key, polygon)`` pairs from the chain."""
    from urbanlens.dashboard.models.boundary.model import Boundary, BoundaryType
    from urbanlens.dashboard.services.geo.boundary_sources import vote_sources
    from urbanlens.dashboard.services.geo.boundary_voting import apply_winning_boundary

    now = timezone.now()
    for service_key, polygon in candidates:
        source = vote_sources().get(service_key)
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
