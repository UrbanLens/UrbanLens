"""Suggest and apply hierarchy fixes for a pin that covers a whole property.
Both are "this pin's hierarchy doesn't match the ground", both are detectable from the same parcel data, and both are tedious to fix by hand - so they are offered together, once, as a single suggestion (see ``controllers.pin_restructure``)."""

from __future__ import annotations

from dataclasses import dataclass, field
import hashlib
import json
import logging
from typing import TYPE_CHECKING, Any

from django.contrib.gis.gdal.error import GDALException
from django.contrib.gis.geos import GEOSGeometry, Point
from django.contrib.gis.geos.error import GEOSException
from django.core.exceptions import ObjectDoesNotExist
from django.db import transaction

from urbanlens.dashboard.models.pin.model import Pin, PinType
from urbanlens.dashboard.services.locations import site_scope

if TYPE_CHECKING:
    from collections.abc import Iterable, Sequence

    from urbanlens.dashboard.models.location.model import Location
    from urbanlens.dashboard.models.place.model import Place
    from urbanlens.dashboard.models.profile.model import Profile
    from urbanlens.dashboard.models.wiki.model import Wiki
    from urbanlens.dashboard.services.pins.building_clusters import BuildingCluster, Marker, SweptBuilding

logger = logging.getLogger(__name__)

#: Cap on how many child pins one apply will create or nest. Far above any real
#: backstop against a provider returning something pathological.
MAX_RESTRUCTURE_ITEMS = 500

#: Default icon/background styling for auto-created building pins.
#: The generic per-type default (a mid-grey building icon, no background) reads poorly against
#: satellite imagery; these give every imported building marker a legible black icon on a faint white
#: disc without the owner having to style a hundred pins by hand.
BUILDING_PIN_ICON_COLOR = "#000000"
BUILDING_PIN_BG_COLOR = "#ffffff"
BUILDING_PIN_BG_OPACITY = 35

#: Bound on the building-in-building walk up to a parcel; real nesting is two or three deep.
MAX_PARCEL_HOPS = 10

#: Child markers that mark a feature *of* a building rather than a building, so never stand for one.
POINT_FEATURE_TYPES = frozenset({PinType.ENTRANCE, PinType.POINT_OF_INTEREST, PinType.DANGER, PinType.STAIR, PinType.ELEVATOR})


# Geometry helpers


def building_footprint(building: dict[str, Any]) -> GEOSGeometry | None:
    """The building's own footprint polygon, when its source published one.

    Args:
        building: A cached building record (see ``plugins.builtin.parcel_buildings``).

    Returns:
        The footprint as a GEOS geometry, or None when the record carries only a point (or nothing parseable - a malformed ``geometry`` is treated as "no footprint", never an error)."""
    geometry = building.get("geometry")
    if not isinstance(geometry, dict) or geometry.get("type") in (None, "Point"):
        return None
    try:
        shape = GEOSGeometry(json.dumps(geometry), srid=4326)
    except (GEOSException, GDALException, ValueError, TypeError):
        logger.debug("pin_restructure: unparseable building geometry %r", geometry, exc_info=True)
        return None
    # dims 2 == areal. A provider sending a LineString "footprint" has nothing
    # to test containment against, so it falls back to the centroid radius.
    return shape if not shape.empty and shape.dims == 2 else None


def _marker_point(marker) -> Point | None:
    """A pin/wiki marker's coordinates as a GEOS point, or None."""
    latitude, longitude = marker.effective_latitude, marker.effective_longitude
    if latitude is None or longitude is None:
        return None
    return Point(float(longitude), float(latitude), srid=4326)


def marker_covers_building(building: dict[str, Any], marker) -> bool:
    """Whether an existing marker already stands for this building.

    Args:
        building: A cached building record.
        marker: A child pin or child wiki.

    Returns:
        True when this building is already covered by that marker."""
    point = _marker_point(marker)
    if point is None:
        return False

    footprint = building_footprint(building)
    if footprint is not None:
        return bool(footprint.contains(point) or footprint.touches(point))

    latitude, longitude = building.get("latitude"), building.get("longitude")
    if latitude is None or longitude is None:
        return False
    distance = site_scope.meters_between(float(marker.effective_latitude), float(marker.effective_longitude), float(latitude), float(longitude))
    return distance <= site_scope.BUILDING_MATCH_METERS


def match_marker(building: dict[str, Any], candidates: list) -> Any | None:
    """The marker already covering a building, if one does.

    Args:
        building: A cached building record.
        candidates: Markers not yet matched to a building.

    Returns:
        The covering marker, or None."""
    covering = [marker for marker in candidates if marker_covers_building(building, marker)]
    if not covering:
        return None
    latitude, longitude = building.get("latitude"), building.get("longitude")
    if latitude is None or longitude is None:
        return covering[0]
    return min(
        covering,
        key=lambda marker: site_scope.meters_between(float(marker.effective_latitude), float(marker.effective_longitude), float(latitude), float(longitude)),
    )


def unmatched_buildings(buildings: list[dict[str, Any]], markers: list) -> list[dict[str, Any]]:
    """Buildings that no marker covers yet.

    Args:
        buildings: Cached building records.
        markers: Existing child markers to match against.

    Returns:
        The subset of ``buildings`` with usable coordinates and no covering marker."""
    unmatched = list(markers)
    missing: list[dict[str, Any]] = []
    for building in buildings:
        if building.get("latitude") is None or building.get("longitude") is None:
            continue
        covering = match_marker(building, unmatched)
        if covering is not None:
            unmatched.remove(covering)
            continue
        missing.append(building)
    return missing


# What could be restructured


@dataclass(slots=True)
class RestructurePlan:
    """Everything one pin's restructure suggestion would do."""

    #: Buildings on this property with no child pin yet.
    buildings: list[dict[str, Any]] = field(default_factory=list)
    #: The owner's other top-level pins that stand inside this property.
    nestable: list[Pin] = field(default_factory=list)

    @property
    def is_empty(self) -> bool:
        """True when there is nothing worth asking about."""
        return not self.buildings and not self.nestable


def property_polygon(pin: Pin) -> GEOSGeometry | None:
    """The pin's real property boundary, or None when only the fallback circle exists.

    Args:
        pin: The pin whose property boundary to resolve.

    Returns:
        A real (drawn, community, or provider-generated) property polygon, else the parcel the pin's place sits on, or None."""
    from urbanlens.dashboard.models.boundary.model import Boundary, BoundaryType

    polygon, source = Boundary.objects.resolve_for_pin(pin, BoundaryType.PROPERTY)
    if polygon is not None and source != "circle":
        return polygon
    # A campus pin resting on one of its buildings draws no property of its own; the parcel above it does.
    parcel = enclosing_parcel(pin.location.place if pin.location_id and pin.location.place_id else None)
    return parcel.geometry if parcel is not None else None


def enclosing_parcel(place: Place | None) -> Place | None:
    """The nearest place above ``place`` (or ``place`` itself) that is not a building.

    Args:
        place: The place a location resolved onto.

    Returns:
        The parcel or site, or None when the chain ends at a building with no known parcel.
    """
    from urbanlens.dashboard.models.place.model import PlaceKind, PlaceRelation

    hops = 0
    while place is not None and place.kind == PlaceKind.BUILDING and hops < MAX_PARCEL_HOPS:
        place = place.parent if place.parent_id and place.parent_relation == PlaceRelation.PART_OF else None
        hops += 1
    return place if place is not None and place.kind != PlaceKind.BUILDING else None


def nestable_root_pins(pin: Pin) -> list[Pin]:
    """The owner's other top-level pins standing inside this pin's property.

    Args:
        pin: The prospective parent pin.

    Returns:
        Top-level pins of the same profile whose coordinates fall inside the property boundary, excluding this pin and anything that would form a cycle."""
    if pin.pk is None:
        return []
    polygon = property_polygon(pin)
    if polygon is None:
        return []

    # `location__wiki` because the dialog renders `effective_name` for every
    # candidate, and a candidate with no `name` of its own falls through to
    # `Location.display_name`, which reads the wiki.
    candidates = Pin.objects.filter(profile_id=pin.profile_id, parent_pin__isnull=True, location__point__within=polygon).exclude(pk=pin.pk).select_related("location", "location__wiki").order_by("name")
    # would_create_cycle also covers the case of this pin itself being nested
    # under one of the candidates - re-parenting that candidate beneath this
    # pin would close a loop.
    return [candidate for candidate in candidates[: MAX_RESTRUCTURE_ITEMS + 1] if not candidate.would_create_cycle(pin)]


def plan_for(pin: Pin) -> RestructurePlan:
    """What this pin's restructure suggestion would change.
    A read-only survey: it consults only already-cached parcel data and the owner's own pins, and never contacts an external service.

    Args:
        pin: The pin being viewed.

    Returns:
        The plan; check ``is_empty`` before offering anything."""
    return RestructurePlan(buildings=missing_buildings(pin), nestable=nestable_root_pins(pin))


def already_pinned_points(pin: Pin, buildings: list[dict[str, Any]]) -> set[tuple[Any, Any]]:
    """Which of ``buildings``' centroids this profile already has a pin on.
    Mirrors the rule ``pin_creation.resolve_child_pin_location`` enforces: a profile may not hold two pins at one exact point, counting *every* pin it owns rather than only this parcel's children.

    Args:
        pin: The parent pin, for whose profile the check runs.
        buildings: Building records to test.

    Returns:
        The quantized ``(latitude, longitude)`` pairs that are already taken."""
    from urbanlens.dashboard.models.location.queryset import quantize_coordinate

    wanted: set[tuple[Any, Any]] = set()
    for building in buildings:
        latitude, longitude = building.get("latitude"), building.get("longitude")
        if latitude is None or longitude is None:
            continue
        wanted.add((quantize_coordinate(latitude, "latitude"), quantize_coordinate(longitude, "longitude")))
    if not wanted:
        return set()

    # Two `__in`s form a cross-product - one pin's latitude can pair with a
    # different building's longitude - so the pairs are re-checked against
    # `wanted` rather than trusted from the query.
    taken = Pin.objects.filter(
        profile_id=pin.profile_id,
        location__latitude__in=[latitude for latitude, _ in wanted],
        location__longitude__in=[longitude for _, longitude in wanted],
    ).values_list("location__latitude", "location__longitude")
    return {pair for pair in taken if pair in wanted}


def missing_buildings(pin: Pin) -> list[dict[str, Any]]:
    """Buildings on this pin's parcel that no pin of the owner's covers yet, one record per building.

    Args:
        pin: The parent pin.

    Returns:
        The representative record of each uncovered building, or ``[]``."""
    from urbanlens.dashboard.plugins.builtin.parcel_buildings import buildings_on_property

    cached = site_scope.parcel_buildings(pin.location) or []
    children = list(pin.descendants().select_related("location"))
    importable = importable_building_indexes(pin, cached, children, property_polygon(pin))
    if not importable:
        return []
    on_property = buildings_on_property(cached)
    return [on_property[index] for index in sorted(importable)]


def importable_building_indexes(pin: Pin, cached: list[dict[str, Any]], children: Sequence[Marker], boundary: GEOSGeometry | None) -> frozenset[int]:
    """Which of this parcel's buildings the import could actually create a pin for.
    The rule behind :func:`missing_buildings`, expressed as *positions in* ``buildings_on_property(cached)`` rather than as records: one position per building no child covers - its cluster's representative (see ``services.pins.building_clusters``).

    Args:
        pin: The parent pin.
        cached: The parcel's cached building records, unfiltered.
        children: The pin's child markers at any depth, to match against.
        boundary: The property's real (non-circle) boundary, or None.

    Returns:
        Positions in ``buildings_on_property(cached)``, possibly empty."""
    from urbanlens.dashboard.plugins.builtin.parcel_buildings import buildings_on_property
    from urbanlens.dashboard.services.pins.building_clusters import distinct_building_count, match_clusters

    nester = BuildingNester.build(pin, cached, boundary)
    # Gate on distinct buildings, offer every real one: a building that contains others is still a
    # building, and nesting a pin under it is the point.
    if distinct_building_count(nester.clusters) < site_scope.MULTI_BUILDING_THRESHOLD:
        return frozenset()

    matched, _unmatched = match_clusters(nester.clusters, building_markers(children))
    position = {id(record): index for index, record in enumerate(buildings_on_property(cached))}
    missing = [(cluster, nester.points(cluster)) for index, cluster in enumerate(nester.clusters) if index not in matched]
    taken = _taken_points(pin, [point for _cluster, points in missing for point in points])
    return frozenset(position[id(cluster.representative)] for cluster, points in missing if id(cluster.representative) in position and any(_quantized(point) not in taken for point in points))


def should_offer(pin: Pin) -> bool:
    """Whether this pin may show a restructure suggestion at all.
    Checks only the cheap gates (settings, dismissal, hierarchy position), not whether there is anything to suggest - see :func:`plan_for` for that.

    Args:
        pin: The pin being viewed.

    Returns:
        True when a suggestion would be welcome."""
    if pin.restructure_offer_dismissed or pin.parent_pin_id is not None:
        return False
    return bool(pin.profile.suggest_pin_restructure)


# Applying it


def building_name(building: dict[str, Any]) -> str:
    """A usable marker name for a building record.

    Args:
        building: A cached building record.

    Returns:
        The building's own name, else "Building <number>", else "" - which leaves the pin unnamed, falling back to its location's display name exactly like any other nameless pin."""
    name = (building.get("name") or "").strip()
    if name:
        return name
    number = str(building.get("building_number") or "").strip()
    return f"Building {number}" if number else ""


def building_selection_key(building: dict[str, Any]) -> str:
    """Return a stable, opaque key for selecting a cached building record."""
    canonical = json.dumps(building, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(canonical.encode()).hexdigest()[:24]


def select_buildings(buildings: list[dict[str, Any]], selection_keys: list[str]) -> list[dict[str, Any]]:
    """Filter building records by keys produced by :func:`building_selection_key`."""
    selected = set(selection_keys)
    return [building for building in buildings if building_selection_key(building) in selected]


def building_markers[M: Marker](markers: Iterable[M]) -> list[M]:
    """The child markers that can stand for a whole building: not a door, a hazard or a stairwell on one.

    Args:
        markers: Child pins or child wikis.

    Returns:
        Those whose type does not mark a feature of a building.
    """
    return [marker for marker in markers if getattr(marker, "pin_type", None) not in POINT_FEATURE_TYPES]


def property_buildings(buildings: list[dict[str, Any]], boundary: GEOSGeometry | None) -> list[dict[str, Any]]:
    """The records that stand on this property: on it by the provider's word, and inside its real boundary.

    Args:
        buildings: Raw building records from either provider.
        boundary: The property's real boundary, or None to trust the provider alone.

    Returns:
        The records, in their original order.
    """
    from urbanlens.dashboard.plugins.builtin.parcel_buildings import building_within_boundary, buildings_on_property

    on_property = buildings_on_property(buildings)
    if boundary is None:
        return on_property
    return [building for building in on_property if building_within_boundary(building, boundary)]


#: OSM ``building=*`` values that say only "a building".
_GENERIC_BUILDING_TAGS = frozenset({"yes", "true", "1", "building", "construction", "no"})


def building_kind(building: dict[str, Any]) -> str:
    """What sort of building a record describes, from its OSM ``building`` tag, as a label.

    Args:
        building: A cached building record.

    Returns:
        E.g. ``"Garage"``, or ``""`` when no source says anything more specific than "a building".
    """
    sources = [entry for entry in building.get("sources") or [] if isinstance(entry, dict)]
    tags = [building.get("building_type"), *(entry["attributes"].get("building") for entry in sources if isinstance(entry.get("attributes"), dict))]
    for tag in tags:
        label = str(tag or "").strip().replace("_", " ")
        if label and label.casefold() not in _GENERIC_BUILDING_TAGS:
            return label[:1].upper() + label[1:]
    return ""


def building_wiki_name(cluster: BuildingCluster, container_name: str = "", reserved: Iterable[str] = ()) -> str:
    """A public, descriptive name for one building's child wiki.
    Built only from the building records and the campus's own public names, never from anybody's pin.

    Args:
        cluster: The building.
        container_name: The campus's name; used only when it is a real name.
        reserved: Further names the building may not take - what the campus is called, or about to be.

    Returns:
        The building's own name, else its number, else its address, else what it is and when it was built,
        placed on the campus.
    """
    from urbanlens.dashboard.services.locations.naming import is_meaningful_name

    container = container_name if is_meaningful_name(container_name) else ""
    taken = {name.casefold() for name in (container, *reserved) if name}

    def usable(name: str) -> bool:
        return is_meaningful_name(name) and name.casefold() not in taken

    numbered = [f"Building {number}" for member in cluster.members if (number := str(member.get("building_number") or "").strip())]
    candidates = [str(member.get("name") or "").strip() for member in cluster.members] + numbered + [str(member.get("address") or "").strip() for member in cluster.members]
    if (name := next((candidate for candidate in candidates if usable(candidate)), None)) is not None:
        return name
    kind = next((label for member in cluster.members if (label := building_kind(member))), "")
    year = next((str(member["year_built"]) for member in cluster.members if member.get("year_built")), "")
    descriptor = f"{kind or 'Building'} ({year})" if year else kind or "Building"
    if container:
        return f"{descriptor} at {container}"
    return descriptor if kind or year else "Unnamed building"


def _quantized(point: tuple[float, float]) -> tuple[Any, Any]:
    from urbanlens.dashboard.models.location.queryset import quantize_coordinate

    return quantize_coordinate(point[0], "latitude"), quantize_coordinate(point[1], "longitude")


def _taken_points(pin: Pin, points: Sequence[tuple[float, float]]) -> set[tuple[Any, Any]]:
    """Which of these points the pin's owner already has a pin on (see ``pin_creation.resolve_child_pin_location``)."""
    return already_pinned_points(pin, [{"latitude": latitude, "longitude": longitude} for latitude, longitude in points])


@dataclass(slots=True)
class WikiMirror:
    """What :meth:`BuildingNester.mirror_wikis` did."""

    #: Cluster position to the wiki standing for that building - wanted or not, so a new building can nest
    #: under its container's existing wiki.
    wikis: dict[int, Wiki] = field(default_factory=dict)
    #: How many of those wikis it created.
    created: int = 0


@dataclass(eq=False, slots=True)
class BuildingNester:
    """One property's buildings, grouped into physical buildings once, and the places, wikis and pins standing for them.

    Every path that turns building records into markers - the automatic sweep, the "Organize this property?"
    dialog, the building import and the wiki mirror - goes through here, so they agree on what a building is,
    where its marker stands, and which existing marker already covers it.
    """

    pin: Pin
    boundary: GEOSGeometry | None
    known: list[dict[str, Any]]
    clusters: list[BuildingCluster]
    _places: dict[int, Place] | None = None

    @classmethod
    def build(cls, pin: Pin, buildings: list[dict[str, Any]], boundary: GEOSGeometry | None) -> BuildingNester:
        """Group already-fetched records.

        Args:
            pin: The campus pin.
            buildings: Raw building records for its parcel.
            boundary: The property's real boundary, or None.

        Returns:
            The nester.
        """
        from urbanlens.dashboard.services.pins.building_clusters import cluster_buildings

        known = property_buildings(buildings, boundary)[:MAX_RESTRUCTURE_ITEMS]
        return cls(pin=pin, boundary=boundary, known=known, clusters=cluster_buildings(known, boundary))

    @classmethod
    def for_pin(cls, pin: Pin, fallback: list[dict[str, Any]] | None = None) -> BuildingNester:
        """Group the pin's cached parcel buildings.

        Args:
            pin: The campus pin.
            fallback: Records to use when nothing is cached for the pin's location.

        Returns:
            The nester.
        """
        return cls.build(pin, site_scope.parcel_buildings(pin.location) or fallback or [], property_polygon(pin))

    def index_of(self, cluster: BuildingCluster | None) -> int | None:
        """A cluster's position in :attr:`clusters`, or None."""
        if cluster is None:
            return None
        return next((index for index, candidate in enumerate(self.clusters) if candidate is cluster), None)

    def clusters_for(self, buildings: Iterable[dict[str, Any]]) -> set[int]:
        """Positions of the clusters holding any of these records, matched by content so a copy still counts."""
        keys = {building_selection_key(building) for building in buildings}
        return {index for index, cluster in enumerate(self.clusters) if any(building_selection_key(member) in keys for member in cluster.members)}

    def points(self, cluster: BuildingCluster) -> list[tuple[float, float]]:
        """Where this building's marker may stand, best first: its marker point, then elsewhere on it.

        The alternatives matter only when the best point is taken - most often by the campus's own pin or wiki,
        whose coordinate is frequently one of its buildings' centroids. Each is one the building covers, so a
        marker placed there is matched back to it.
        """
        candidates = [(cluster.latitude, cluster.longitude)]
        if cluster.footprint is not None:
            try:
                surface = cluster.footprint.point_on_surface
            except GEOSException:
                logger.debug("BuildingNester: no point on the surface of %s", sorted(cluster.refs))
            else:
                candidates.append((float(surface.y), float(surface.x)))
        for member in cluster.members:
            latitude, longitude = member.get("latitude"), member.get("longitude")
            if latitude is not None and longitude is not None:
                candidates.append((float(latitude), float(longitude)))

        unique: list[tuple[float, float]] = []
        seen: set[tuple[Any, Any]] = set()
        for point in candidates:
            key = _quantized(point)
            if key in seen or not cluster.covers(*point) or (self.boundary is not None and not self.boundary.intersects(Point(point[1], point[0], srid=4326))):
                continue
            seen.add(key)
            unique.append(point)
        return unique

    def parcel(self) -> Place | None:
        """The parcel the campus pin stands on."""
        return enclosing_parcel(self.pin.location.place if self.pin.location_id and self.pin.location.place_id else None)

    def places(self) -> dict[int, Place]:
        """Each cluster's building place, provisioned for every building known on the parcel.

        How many buildings stand on a property is a fact about the property; which of them somebody pinned is
        a fact about that person - so this covers every record, not only the ones being pinned.
        """
        from urbanlens.dashboard.services.places import provisioning

        if self._places is None:
            by_record = provisioning.ensure_building_places(self.parcel(), self.known, provider="redata")
            position = {id(record): index for index, record in enumerate(self.known)}
            self._places = {}
            for index, cluster in enumerate(self.clusters):
                place = next((by_record[position[id(member)]] for member in cluster.members if position.get(id(member)) in by_record), None)
                if place is not None:
                    self._places[index] = place
        return self._places

    def reclassify(self) -> None:
        """Re-derive the marker types on the parcel and every building place."""
        from urbanlens.dashboard.services.locations.site_scope import reclassify_markers_on_place

        if (parcel := self.parcel()) is not None:
            reclassify_markers_on_place(parcel)
        for place in {place.pk: place for place in self.places().values()}.values():
            reclassify_markers_on_place(place)

    # Wikis

    def campus_wiki(self) -> Wiki:
        """The campus pin's own community wiki, locked for the rest of the transaction."""
        from urbanlens.dashboard.models.wiki.model import Wiki

        try:
            wiki = self.pin.location.wiki
        except ObjectDoesNotExist:
            # Ordinarily unreachable: tasks.ensure_wiki_for_location creates the wiki when the pin is made.
            wiki, _created = Wiki.objects.get_or_create_for_location(self.pin.location)
        return Wiki.objects.select_for_update(of=("self",)).select_related("location").get(pk=wiki.pk)

    def mirror_wikis(self, wanted: set[int], profile: Profile | None) -> WikiMirror:
        """Give each wanted building a child wiki under the campus wiki, reusing any that already stands for it.

        Args:
            wanted: Positions in :attr:`clusters` to mirror.
            profile: Who to attribute the import's edit entry to; None for the automatic sweep.

        Returns:
            The wikis now standing for the buildings, and how many were created.
        """
        from urbanlens.dashboard.models.wiki.model import Wiki
        from urbanlens.dashboard.models.wiki_edit import WikiEdit
        from urbanlens.dashboard.services.pins.building_clusters import match_clusters

        campus = self.campus_wiki()
        protected = {campus.pk, *self._ancestor_ids(campus)}
        matched, _unmatched = match_clusters(self.clusters, building_markers(campus.descendants().select_related("location")))
        result = WikiMirror(wikis=dict(matched))
        claimed = {wiki.pk for wiki in result.wikis.values()}
        places = self.places()
        for index, cluster in enumerate(self.clusters):
            if index in result.wikis or index not in wanted:
                continue
            parent_index = self.index_of(cluster.parent)
            parent = result.wikis.get(parent_index, campus) if parent_index is not None else campus
            place = places.get(index)
            holder = Wiki.objects.filter(place=place).select_related("location").first() if place is not None else None
            if holder is not None and holder.pk not in protected and holder.pk not in claimed:
                wiki: Wiki | None = self._adopt(holder, parent, cluster, campus, place)
            else:
                wiki, created = self._place_wiki(cluster, parent, None if holder is not None else place, campus, protected | claimed)
                result.created += created
            if wiki is not None:
                result.wikis[index] = wiki
                claimed.add(wiki.pk)
        for index, wiki in matched.items():
            if index in wanted:
                self._refresh_name(wiki, self.clusters[index], campus)

        if result.created:
            # One entry for the whole import: a hundred separate "child_wiki_added" rows would bury the history.
            WikiEdit.objects.create(wiki=campus, editor=profile, changes={"child_wikis_imported": {"from": None, "to": f"{result.created} building markers"}})
        return result

    @staticmethod
    def _ancestor_ids(wiki: Wiki) -> set[int]:
        ids: set[int] = set()
        current = wiki.parent_wiki
        while current is not None and current.pk not in ids and len(ids) < MAX_PARCEL_HOPS:
            ids.add(current.pk)
            current = current.parent_wiki
        return ids

    def _place_wiki(self, cluster: BuildingCluster, parent: Wiki, place: Place | None, campus: Wiki, unavailable: set[int]) -> tuple[Wiki | None, bool]:
        """Create the building's child wiki at the first free point on it, or adopt a wiki already standing there.

        Returns:
            The wiki (None when every point is taken by one this building may not adopt), and whether it is new.
        """
        from urbanlens.dashboard.controllers.detail_pins import ChildWikiLocationError, _location_for_child_wiki
        from urbanlens.dashboard.models.location.model import Location
        from urbanlens.dashboard.models.wiki.model import Wiki
        from urbanlens.dashboard.services.places.resolution import attach_location

        for latitude, longitude in self.points(cluster):
            try:
                location = _location_for_child_wiki(latitude, longitude)
            except ChildWikiLocationError:
                occupant = Location.objects.get_exact_or_create(latitude, longitude)[0].wiki
                if occupant.pk in unavailable or occupant.pin_type in POINT_FEATURE_TYPES:
                    continue
                # Usually the root wiki a building pin's own save queued before its child wiki existed.
                return self._adopt(occupant, parent, cluster, campus, place), False
            if place is not None:
                attach_location(location, place)
            wiki = Wiki.objects.create(
                # created_by unset: mirrored from building data, not placed by anyone - which is what lets a
                # concealed viewer keep seeing them.
                name=self._wiki_name(cluster, campus),
                pin_type=PinType.BUILDING,
                pin_type_is_user_provided=False,
                parent_wiki=parent,
                place=place,
                location=location,
            )
            return wiki, True
        logger.info("mirror_wikis: no free point on building %s for its wiki", sorted(cluster.refs))
        return None, False

    def _adopt(self, wiki: Wiki, parent: Wiki, cluster: BuildingCluster, campus: Wiki, place: Place | None) -> Wiki:
        """Make an existing wiki this building's: nest it if it is a root, and name it if it has no name of its own."""
        from urbanlens.dashboard.models.wiki.model import Wiki
        from urbanlens.dashboard.services.wiki.wiki_merge import absorb_wiki

        if wiki.parent_wiki_id is None and wiki.pk != parent.pk and not wiki.would_create_cycle(parent):
            absorb_wiki(parent, wiki)
        self._refresh_name(wiki, cluster, campus)
        if wiki.place_id is None and place is not None and not Wiki.objects.filter(place=place).exists():
            wiki.place = place
            wiki.save(update_fields=["place"])
        return wiki

    @staticmethod
    def _campus_names(campus: Wiki) -> list[str]:
        """What the campus is called, or - before enrichment names its wiki - is about to be."""
        from urbanlens.dashboard.services.locations.naming import is_meaningful_name

        return [name for name in (campus.name, campus.location.official_name or "") if is_meaningful_name(name)]

    def _wiki_name(self, cluster: BuildingCluster, campus: Wiki) -> str:
        names = self._campus_names(campus)
        return building_wiki_name(cluster, names[0] if names else "", reserved=names)

    def _refresh_name(self, wiki: Wiki, cluster: BuildingCluster, campus: Wiki) -> None:
        """Rename a building's wiki whose name is a placeholder, the campus's own, or one given before the campus had a name."""
        from urbanlens.dashboard.services.locations.naming import is_meaningful_name

        stale = not is_meaningful_name(wiki.name) or wiki.name.casefold() in {name.casefold() for name in self._campus_names(campus)} or wiki.name == building_wiki_name(cluster)
        wanted = self._wiki_name(cluster, campus)
        if stale and wiki.name != wanted:
            wiki.name = wanted
            wiki.save(update_fields=["name"])

    # Pins

    def create_pins(self, wanted: set[int], wikis: dict[int, Wiki] | None = None, *, swept: Sequence[SweptBuilding] = ()) -> dict[int, Pin]:
        """Give each wanted building a child pin under the campus pin, standing on its wiki's own point.

        Args:
            wanted: Positions in :attr:`clusters` to pin.
            wikis: The buildings' child wikis, from :meth:`mirror_wikis`; a pin stands where its wiki does.
            swept: Where earlier sweeps placed pins, so a building whose pin was deleted or moved stays as it is.

        Returns:
            Position to the pin created for it.
        """
        from urbanlens.dashboard.services.pins.building_clusters import match_clusters
        from urbanlens.dashboard.services.places.resolution import attach_location

        wikis = wikis or {}
        existing: list[Pin | SweptBuilding] = [*building_markers(self.pin.descendants().select_related("location")), *swept]
        matched, _unmatched = match_clusters(self.clusters, existing)
        pins: dict[int, Pin] = {index: marker for index, marker in matched.items() if isinstance(marker, Pin)}
        created: dict[int, Pin] = {}
        places = self.places()
        with transaction.atomic():
            for index, cluster in enumerate(self.clusters):
                if index in matched or index not in wanted:
                    continue
                location = self._pin_location(cluster, wikis.get(index))
                if location is None:
                    logger.debug("create_pins: every point on building %s already carries one of this profile's pins", sorted(cluster.refs))
                    continue
                if (place := places.get(index)) is not None and location.place_id != place.pk:
                    # Attached directly rather than by containment: this import knows which structure the marker
                    # is for, and the marker can legitimately fall on a smaller overlapping footprint.
                    attach_location(location, place)
                parent_index = self.index_of(cluster.parent)
                parent = pins.get(parent_index, self.pin) if parent_index is not None else self.pin
                child = Pin.objects.create(
                    name=cluster.name or None,
                    # Derived from a building record, so external name refreshes may still improve it.
                    name_is_user_provided=False,
                    pin_type=PinType.BUILDING,
                    pin_type_is_user_provided=False,
                    parent_pin=parent,
                    profile=self.pin.profile,
                    location=location,
                    color=BUILDING_PIN_ICON_COLOR,
                    detail_bg_color=BUILDING_PIN_BG_COLOR,
                    detail_bg_opacity=BUILDING_PIN_BG_OPACITY,
                )
                pins[index] = created[index] = child
        if created:
            self.reclassify()
        return created

    def _pin_location(self, cluster: BuildingCluster, wiki: Wiki | None) -> Location | None:
        """The first point on the building this profile has no pin on yet, its wiki's own point first."""
        from urbanlens.dashboard.services.pins.pin_creation import PinCreationError, resolve_child_pin_location

        candidates = self.points(cluster)
        if wiki is not None and wiki.location_id is not None:
            point = (wiki.effective_latitude, wiki.effective_longitude)
            if cluster.covers(*point) and (self.boundary is None or self.boundary.intersects(Point(point[1], point[0], srid=4326))):
                candidates.insert(0, point)
        for latitude, longitude in candidates:
            try:
                return resolve_child_pin_location(self.pin.profile, latitude, longitude)
            except PinCreationError:
                continue
        return None


def create_building_pins(pin: Pin, buildings: list[dict[str, Any]]) -> int:
    """Create a child pin for each given building, in one transaction.

    Args:
        pin: The parent pin.
        buildings: Building records to create pins for; records describing one building produce one pin.

    Returns:
        How many child pins were created."""
    nester = BuildingNester.for_pin(pin, fallback=buildings)
    return len(nester.create_pins(nester.clusters_for(buildings[:MAX_RESTRUCTURE_ITEMS])))


def nest_root_pins(pin: Pin, candidates: list[Pin]) -> int:
    """Re-parent top-level pins under this pin, keeping everything else about them.

    Args:
        pin: The new parent.
        candidates: Top-level pins to nest.

    Returns:
        How many pins were nested."""
    nested = 0
    with transaction.atomic():
        for candidate in candidates[:MAX_RESTRUCTURE_ITEMS]:
            # Re-checked here, not just at suggestion time: the hierarchy may
            # have changed between the page rendering and the owner accepting.
            if candidate.parent_pin_id is not None or candidate.pk == pin.pk or candidate.would_create_cycle(pin):
                continue
            candidate.parent_pin = pin
            candidate.save(update_fields=["parent_pin", "updated"])
            nested += 1
    return nested


def mirror_buildings_to_wiki(pin: Pin, buildings: list[dict[str, Any]], profile: Profile) -> int:
    """Mirror imported buildings as child wikis of the pin's community wiki.

    Args:
        pin: The parent pin, whose location's wiki is the parent wiki.
        buildings: The building records just imported.
        profile: The profile to attribute the resulting WikiEdit to.

    Returns:
        How many child wikis were created; none when the pin's owner has community features off."""
    if not pin.profile.community_enabled:
        return 0
    nester = BuildingNester.for_pin(pin, fallback=buildings)
    with transaction.atomic():
        return nester.mirror_wikis(nester.clusters_for(buildings[:MAX_RESTRUCTURE_ITEMS]), profile).created
