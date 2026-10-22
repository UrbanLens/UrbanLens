"""Parcel buildings plugin: every structure standing on a pin's property."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any, ClassVar

from urbanlens.dashboard.plugins.base import UrbanLensPlugin
from urbanlens.dashboard.services.core.gateway import is_source_outage
from urbanlens.dashboard.services.locations.enrichment import LocationCacheEnrichmentSource
from urbanlens.dashboard.services.locations.site_scope import PARCEL_BUILDINGS_CACHE_SOURCE
from urbanlens.dashboard.services.pins.external_data import LocationCachePanelSource, PanelApiKind
from urbanlens.dashboard.services.places.building_records import (
    BuildingRecordProvider,
    BuildingsAnswer,
    bulk_enrichment_available,
    fetch_parcel_buildings,
    service_keys,
    source_labels,
)

if TYPE_CHECKING:
    from django.contrib.gis.geos import GEOSGeometry

    from urbanlens.dashboard.models.location.model import Location
    from urbanlens.dashboard.models.pin.model import Pin
    from urbanlens.dashboard.plugins.base import PluginContext

logger = logging.getLogger(__name__)

#: The ``source`` of a building record a viewer's browser found and posted back; the panel labels it as such.
BROWSER_OSM_SOURCE = "osm_browser"

#: Human-readable labels for the public sources' own reporting systems, for the per-row source chip. A plugin's
#: provider labels its own (``BuildingRecordProvider.source_labels``).
SOURCE_LABELS: dict[str, str] = {
    "overpass": "OpenStreetMap",
    "osm": "OpenStreetMap",
    BROWSER_OSM_SOURCE: "OpenStreetMap (found by a viewer's browser)",
    "microsoft_buildings": "Microsoft Buildings",
    "google_open_buildings": "Google Open Buildings",
}


def source_chips(sources: list[str]) -> list[str]:
    """
    Human-readable provenance chips for one building's source keys.

    Args:
        sources: Source keys from :func:`record_sources`.

    Returns:
        Labels in the order given, without repeats - ``overpass`` and ``osm`` share a label, so a record referencing both would otherwise render "OpenStreetMap + OpenStreetMap"."""
    labels = {**SOURCE_LABELS, **source_labels()}
    chips: list[str] = []
    for key in sources:
        label = labels.get(key)
        if label and label not in chips:
            chips.append(label)
    return chips


def record_sources(building: dict[str, Any]) -> list[str]:
    """
    Every source that references one building, richest-information first.
    Reading only the ``source`` key leaves the provenance chip blank for every reconciled record; reading only ``sources`` drops Overpass, which still answers in the flat shape.

    Args:
        building: A raw building record from either shape.

    Returns:
        Source keys in the order the record lists them (richest first), or a single-entry list for a flat record, or empty when neither key is present."""
    sources = building.get("sources")
    if isinstance(sources, list):
        keys = [key for entry in sources if isinstance(entry, dict) and (key := entry.get("source"))]
        if keys:
            return keys
    return [key] if (key := building.get("source")) else []


def buildings_on_property(buildings: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """
    The records that stand on this property, dropping the ones that don't.
    A parcel inside a broad archaeological sensitivity zone gets every surveyed building in that zone - roughly a thousand acres' worth here - each flagged ``is_on_property: false`` precisely so a consumer can drop it.

    Args:
        buildings: Raw building records from either provider.

    Returns:
        Only the records on this property."""
    return [b for b in buildings if b.get("is_on_property") is not False]


def countable_buildings(buildings: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """
    The records to count, one per physical building.
    A record carrying ``child_refs`` contains other records in this same list, so counting it as well as its children counts a subdivided structure twice.

    Args:
        buildings: Raw building records from either provider.

    Returns:
        The on-property records that are nobody's parent."""
    return [b for b in buildings_on_property(buildings) if not b.get("child_refs")]


def confident_buildings(buildings: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """
    The on-property records certain enough to act on without asking.

    Args:
        buildings: Raw building records from either provider.

    Returns:
        The on-property records with no unresolved overlap."""
    return [b for b in buildings_on_property(buildings) if not b.get("overlap_refs")]


def _tree_ordered(rows: list[dict[str, Any]], *, annotate_depth: bool = True) -> list[dict[str, Any]]:
    """
    Order rows so each building is followed by the buildings inside it.

    Args:
        rows: Rendered rows, each possibly carrying ``ref``/``parent_ref``.

    Returns:
        The same rows, depth-first, siblings in :func:`_row_sort_key` order."""
    known = {row["ref"] for row in rows if row.get("ref")}
    children: dict[str, list[dict[str, Any]]] = {}
    roots: list[dict[str, Any]] = []
    for row in rows:
        parent = row.get("parent_ref")
        # A parent_ref pointing outside this list (filtered out as off-property,
        # say) leaves the child a root rather than orphaning it entirely.
        if parent and parent in known:
            children.setdefault(parent, []).append(row)
        else:
            roots.append(row)

    ordered: list[dict[str, Any]] = []
    placed: set[int] = set()

    def walk(row: dict[str, Any], depth: int) -> None:
        if id(row) in placed:
            return
        placed.add(id(row))
        if annotate_depth:
            row["depth"] = depth
        ordered.append(row)
        for child in sorted(children.get(row.get("ref") or "", []), key=_row_sort_key):
            walk(child, depth + 1)

    for root in sorted(roots, key=_row_sort_key):
        walk(root, 0)
    # Anything left is in a parent_ref cycle; show it rather than lose it.
    for row in rows:
        if id(row) not in placed:
            if annotate_depth:
                row["depth"] = 0
            ordered.append(row)
    return ordered


def building_tree_order(buildings: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """
    Order raw building records parents-first, children directly after.
    The importer needs this ordering to mirror a source's building tree as a pin tree: a child's pin can only be parented under its building's pin if that pin exists by the time the child is created.

    Args:
        buildings: Raw building records carrying ``ref``/``parent_ref``.

    Returns:
        The same records, depth-first (see :func:`_tree_ordered`)."""
    return _tree_ordered(buildings, annotate_depth=False)


def _overpass_buildings(location: Location) -> list[dict[str, Any]]:
    """
    OSM buildings inside the location's effective property boundary.

    Args:
        location: The location whose property boundary bounds the search.

    Returns:
        Building records, or ``[]`` when there's no real boundary to search inside or Overpass found nothing.

    Raises:
        Exception: Overpass could not be asked (see ``is_source_outage``).
    """
    from urbanlens.dashboard.services.apis.locations.boundaries.overpass import OverpassGateway
    from urbanlens.dashboard.services.places.scope import parcel_polygon_for_location

    polygon = parcel_polygon_for_location(location)
    if polygon is None:
        return []

    try:
        return OverpassGateway().buildings_within(polygon)
    except Exception as exc:
        logger.debug("parcel_buildings: Overpass lookup failed for location %s", location.pk, exc_info=True)
        if is_source_outage(exc):
            raise
        return []


#: The most vertices of the parcel outline sent to a browser to query Overpass with.
MAX_QUERY_RING_POINTS = 300


def overpass_poly_clause(polygon: GEOSGeometry) -> str:
    """
    The parcel's outline as an Overpass ``poly:`` filter, for the viewer's browser to query with.

    Args:
        polygon: The parcel boundary (WGS-84); a MultiPolygon is queried by its largest part, as the server does.

    Returns:
        ``"lat lon lat lon ..."`` of at most :data:`MAX_QUERY_RING_POINTS` points, or ``""`` when there is no usable ring.
    """
    from django.contrib.gis.geos import MultiPolygon, Polygon

    largest = max(polygon, key=lambda part: part.area) if isinstance(polygon, MultiPolygon) and len(polygon) else polygon
    if not isinstance(largest, Polygon) or largest.empty:
        return ""
    tolerance = 0.0
    while len(largest.exterior_ring.coords) > MAX_QUERY_RING_POINTS and tolerance < 0.01:
        tolerance = tolerance * 2 or 0.000005
        simpler = largest.simplify(tolerance, preserve_topology=True)
        if isinstance(simpler, Polygon) and not simpler.empty:
            largest = simpler
    ring = largest.exterior_ring.coords
    return "" if len(ring) > MAX_QUERY_RING_POINTS else " ".join(f"{lat:.6f} {lon:.6f}" for lon, lat in ring)


def browser_provided_buildings(data: dict, viewer_id: int | None) -> list[dict[str, Any]]:
    """
    The buildings a browser found that this viewer is owed, less any the list already holds.

    Nothing a browser posts can be checked, so a submitter sees their own and anyone else only a building enough distinct
    accounts submitted. They are shown in the panel and nowhere else: not in ``buildings``, so nothing that acts on the list
    (nesting, importing) reads them.

    Args:
        data: The cached parcel list payload.
        viewer_id: The viewer's user pk.

    Returns:
        Building records.
    """
    from urbanlens.dashboard.services.pins.external_data import USER_PROVIDED_KEY
    from urbanlens.dashboard.services.search.corroboration import readable_by

    known = {(b.get("osm_type"), b.get("osm_id")) for b in data.get("buildings") or [] if isinstance(b, dict) and b.get("osm_id")}
    return [
        {key: value for key, value in held.items() if key not in {"submitters", "submitted_at"}}
        for held in data.get(USER_PROVIDED_KEY) or []
        if isinstance(held, dict) and readable_by(held.get("submitters", []), viewer_id) and (held.get("osm_type"), held.get("osm_id")) not in known
    ]


class OverpassBuildingRecords(BuildingRecordProvider):
    """The public source of building records: OpenStreetMap footprints inside the property boundary."""

    key: ClassVar[str] = "overpass"
    tag: ClassVar[str] = "osm"
    place_namespace: ClassVar[str] = "osm"
    accepts_supplements: ClassVar[bool] = True
    service_keys: ClassVar[tuple[str, ...]] = ("overpass",)

    def available(self) -> bool:
        """Always: Overpass needs no credentials."""
        return True

    def buildings_on_parcel(self, location: Location) -> BuildingsAnswer:
        """The OSM buildings inside the location's parcel."""
        return BuildingsAnswer(_overpass_buildings(location))


def building_rows(buildings: list[dict[str, Any]], children: list, url_for=None, boundary_polygon: GEOSGeometry | None = None) -> list[dict[str, Any]]:
    """
    Pair each known building with the child marker that already covers it.
    Matching goes through ``services.pins.building_clusters`` - the same grouping and matching auto-nest and the restructure suggestion use - so this panel's idea of "already pinned" can never drift from what they would actually create.

    Args:
        buildings: Cached building records (see :class:`~urbanlens.dashboard.services.places.building_records.BuildingRecord`).
        children: The marker's child markers at any depth, to match against.
        url_for: Optional callable turning a matched child into a link target; omit for child wikis, which are markers on their parent's page rather than pages of their own.
        boundary_polygon: The property's real (non-circle) boundary, when known.

    Returns:
        One row per building record, sorted by building number then name, each with ``name``, ``building_number``, ``year_built`` (the building's own, else ``""``: see ``services.pins.build_dates.own_build_year``), ``source``, ``source_label``, ``latitude``, ``longitude``, ``geometry``, ``has_geometry``, ``child_name``, ``child_uuid``, and... Records describing one physical building share its child."""
    rows, _unmatched = match_buildings_to_children(buildings, children, url_for=url_for, boundary_polygon=boundary_polygon)
    return rows


def match_buildings_to_children(
    buildings: list[dict[str, Any]],
    children: list,
    url_for=None,
    boundary_polygon: GEOSGeometry | None = None,
) -> tuple[list[dict[str, Any]], list]:
    """
    The matching :func:`building_rows` wraps, also returning the leftover children.

    Records inside the boundary are grouped into physical buildings first, and each building claims at most one
    child; every record of that building shows it. A record outside the boundary is listed only when a leftover
    child covers it on its own.

    Returns:
        ``(rows, unmatched)`` - ``rows`` exactly as :func:`building_rows` returns them, ``unmatched`` the children left over once every building has claimed at most one."""
    from urbanlens.dashboard.services.pins.building_clusters import cluster_buildings, match_clusters
    from urbanlens.dashboard.services.pins.pin_restructure import building_markers, match_marker

    on_property = list(enumerate(buildings_on_property(buildings)))
    inside = [(index, building) for index, building in on_property if boundary_polygon is None or building_within_boundary(building, boundary_polygon)]
    clusters = cluster_buildings([building for _index, building in inside], boundary_polygon)
    candidates = building_markers(children)
    matched, unmatched_candidates = match_clusters(clusters, candidates)
    child_of: dict[int, Any] = {id(member): matched[position] for position, cluster in enumerate(clusters) if position in matched for member in cluster.members}

    inside_ids = {id(building) for _index, building in inside}
    rows: list[dict[str, Any]] = []
    for record_index, building in on_property:
        if id(building) in inside_ids:
            child = child_of.get(id(building))
        else:
            child = match_marker(building, unmatched_candidates)
            if child is None:
                continue
            unmatched_candidates.remove(child)
        rows.append(_building_row(building, record_index, child, url_for))

    candidate_ids = {id(child) for child in candidates}
    leftover_ids = {id(child) for child in unmatched_candidates}
    return _tree_ordered(rows), [child for child in children if id(child) not in candidate_ids or id(child) in leftover_ids]


def _building_row(building: dict[str, Any], record_index: int, child: Any, url_for) -> dict[str, Any]:
    """One building record as a panel row, with the child marker standing for it."""
    from urbanlens.dashboard.services.pins.build_dates import own_build_year

    geometry = building_footprint_geojson(building)
    sources = record_sources(building)
    year_built = own_build_year(building)
    return {
        "name": building.get("name") or "",
        "building_number": building.get("building_number") or "",
        # A parcel's year on a row would read as when this building was built.
        "year_built": year_built if year_built is not None else "",
        "source": sources[0] if sources else "",
        "source_label": " + ".join(source_chips(sources)),
        "latitude": building.get("latitude"),
        "longitude": building.get("longitude"),
        "geometry": geometry,
        "has_geometry": geometry is not None,
        "ref": building.get("ref") or "",
        "parent_ref": building.get("parent_ref") or "",
        "child_refs": list(building.get("child_refs") or []),
        "depth": 0,
        "selection_key": building.get("_selection_key") or "",
        # This row's position in buildings_on_property(buildings) - the handle a caller uses to pair a rendered
        # row with the record it came from, after tree-ordering and boundary drops have reordered and thinned
        # the list. Not a substitute for selection_key, which is what a POST correlates by.
        "record_index": record_index,
        "origin": "external",
        "child_name": _marker_name(child) if child is not None else "",
        "child_uuid": str(child.uuid) if child is not None and getattr(child, "uuid", None) else "",
        "child_slug": getattr(child, "slug", "") or "" if child is not None else "",
        "child_url": (url_for(child) if url_for is not None else "") if child is not None else "",
    }


def unpinned_building_child_rows(unmatched_children: list, url_for=None) -> list[dict[str, Any]]:
    """
    Row-shape a leftover ``BUILDING`` child pin/wiki that no known building record covers.
    ``building_rows``' matching loop only ever *consumes* markers that cover an external building record - a ``BUILDING`` pin the owner created by hand (no source ever reported it, or the parcel has no external data at all) is simply never mentioned.

    Args:
        unmatched_children: The ``unmatched`` list :func:`match_buildings_to_children` returned - children with no external building record.
        url_for: Optional callable turning a child pin into a link target, as in :func:`building_rows`.

    Returns:
        One row per ``BUILDING``-typed child, tree-ordered the same way :func:`building_rows`' output is (they share the same sort key)."""
    from urbanlens.dashboard.models.pin.model import PinType

    rows: list[dict[str, Any]] = [
        {
            # No separate "official" name to fall back from here (unlike an external row, where
            # `name` and `child_name` can legitimately differ) - the pin's own name is the only name
            # this row has, and using it for both is also what makes _row_sort_key (which only reads
            # `name`) sort these alphabetically instead of by insertion order.
            "name": _marker_name(child),
            "building_number": "",
            "year_built": "",
            "source": "",
            "source_label": "",
            "latitude": getattr(child, "effective_latitude", None),
            "longitude": getattr(child, "effective_longitude", None),
            "geometry": None,
            "has_geometry": False,
            "ref": "",
            "parent_ref": "",
            "child_refs": [],
            "depth": 0,
            "selection_key": "",
            "origin": "pin",
            "child_name": _marker_name(child),
            "child_uuid": str(child.uuid) if getattr(child, "uuid", None) else "",
            "child_slug": getattr(child, "slug", "") or "",
            "child_url": url_for(child) if url_for is not None else "",
        }
        for child in unmatched_children
        if getattr(child, "pin_type", None) == PinType.BUILDING
    ]
    return _tree_ordered(rows, annotate_depth=False)


def child_pin_rows(children: list, url_for=None) -> list[dict[str, Any]]:
    """
    Row-shape every direct child pin, of any type, for the panel's Child pins tab.

    Args:
        children: The pin's direct children.
        url_for: Optional callable turning a child into a link target.

    Returns:
        One ``{"name", "pin_type", "type_label", "child_uuid", "child_slug", "child_url"}`` row per child, grouped by
        type in the order ``PinType`` lists them, then by name.
    """
    from urbanlens.dashboard.models.pin.model import PinType

    order = {value: index for index, value in enumerate(PinType.values)}
    rows = [
        {
            "name": _marker_name(child),
            "pin_type": child.pin_type,
            "type_label": PinType(child.pin_type).label if child.pin_type in order else "",
            "child_uuid": str(child.uuid),
            "child_slug": child.slug or "",
            "child_url": url_for(child) if url_for is not None else "",
        }
        for child in children
    ]
    rows.sort(key=lambda row: (order.get(row["pin_type"], len(order)), row["name"].casefold()))
    return rows


def building_within_boundary(building: dict[str, Any], boundary_polygon: GEOSGeometry) -> bool:
    """
    Whether an unpinned building actually sits on the given boundary.
    Prefers the building's own footprint (an intersection test, so a structure straddling the boundary edge still counts) and falls back to its centroid when the provider published only a point.

    Args:
        building: One cached building record.
        boundary_polygon: The property's real boundary.

    Returns:
        True when the building overlaps the boundary, or has no usable geometry to test at all (never silently drop a record we can't place)."""
    from django.contrib.gis.geos import Point

    from urbanlens.dashboard.services.pins.pin_restructure import building_footprint

    footprint = building_footprint(building)
    if footprint is not None:
        return bool(boundary_polygon.intersects(footprint))

    latitude, longitude = building.get("latitude"), building.get("longitude")
    if latitude is None or longitude is None:
        return True
    point = Point(float(longitude), float(latitude), srid=4326)
    return bool(boundary_polygon.intersects(point))


def building_footprint_geojson(building: dict[str, Any]) -> dict[str, Any] | None:
    """
    A building record's real footprint geometry, if it has one.
    Both cases collapse to None, which is what makes ``has_geometry`` a usable "can I draw this outline?" flag rather than a "did the provider send a geometry key?" one.

    Args:
        building: One cached building record.

    Returns:
        The GeoJSON geometry dict for a real outline, or None."""
    geometry = building.get("geometry")
    if not isinstance(geometry, dict) or geometry.get("type") == "Point":
        return None
    return geometry


def _marker_name(marker) -> str:
    """Display name of a child pin or child wiki (Wiki has no ``effective_name``)."""
    return getattr(marker, "effective_name", None) or getattr(marker, "name", "") or ""


def _row_sort_key(row: dict[str, Any]) -> tuple:
    """Sort buildings by number (numerically when they are numbers), then name."""
    number = str(row.get("building_number") or "").strip()
    if number.isdigit():
        return (0, int(number), "")
    if number:
        return (1, 0, number.casefold())
    return (2, 0, str(row.get("name") or "").casefold())


def building_list_cached(location: Location) -> None:
    """
    Act on a building list just cached for ``location``.

    The moment the list lands is the moment the default structure can be built - every building becomes a child pin
    with no dialog. On a campus it also says which building each pinned location stands on, so those given no wiki of
    their own while the campus's stood in for it get their building's.

    Args:
        location: The Location the list was cached for.
    """
    from django.db import transaction

    from urbanlens.dashboard.models.wiki.model import Wiki
    from urbanlens.dashboard.services.pins.auto_nest import auto_nest_location

    auto_nest_location(location)
    if not Wiki.objects.filter(location_id=location.pk, place__isnull=False).exists():
        return

    def _enqueue() -> None:
        from urbanlens.dashboard.services.core.celery import safely_enqueue_task
        from urbanlens.dashboard.tasks import ensure_building_wikis

        safely_enqueue_task(ensure_building_wikis, location.pk)

    transaction.on_commit(_enqueue)


class ParcelBuildingsPanelSource(LocationCachePanelSource):
    """Every building on the pin's parcel, for the "Buildings on this Property" panel."""

    key = PARCEL_BUILDINGS_CACHE_SOURCE
    cache_source = PARCEL_BUILDINGS_CACHE_SOURCE
    section_id = "parcel-buildings-section"
    icon = "apartment"
    title = "Buildings on this Property"
    # Its own read shape, because neither of the uniform ones fits: a building row is a place with
    # coordinates, a footprint, and a link to the child pin covering it - which is neither an
    # information card nor a media item.
    api_kinds: ClassVar[frozenset[PanelApiKind]] = frozenset({PanelApiKind.BUILDINGS})

    def gate(self, pin: Pin) -> bool:
        """Only for a root pin with coordinates - a child pin has no sub-buildings."""
        if pin.parent_pin_id is not None:
            return False
        return bool(pin.effective_latitude and pin.effective_longitude)

    def has_content(self, data: dict | None) -> bool:
        """Whether the parcel lookup found any building."""
        return bool(data and data.get("buildings"))

    def always_shows(self, pin: Pin) -> bool:
        """A pin with child pins lists them here whatever the parcel lookup found."""
        return pin.detail_pins.exists()

    def fetch(self, pin: Pin) -> None:
        """Enumerate the parcel's buildings and cache them against the pin's location."""
        from urbanlens.dashboard.models.cache.location_cache import LocationCache

        location = pin.location
        payload = fetch_parcel_buildings(location)
        LocationCache.set(location, self.cache_source, payload, query_key=f"{float(location.latitude or 0):.5f},{float(location.longitude or 0):.5f}")
        building_list_cached(location)

    def api_payload(self, pin: Pin) -> dict[str, Any] | None:
        """
        Every building on the pin's parcel, each paired with its child pin.
        It is resolved server-side through the very same :func:`building_rows` the web panel renders, so both surfaces agree on which buildings are still unpinned.

        Args:
            pin: The pin whose parcel is being read. Its child pins at every
                depth are the markers matched against - a building nested
                under its container's pin is still pinned.

        Returns:
            ``{"buildings": [...], "provider": ..., "unpinned_count": ...}``, or None when nothing has landed yet, the gate rejects this pin (a child pin has no sub-buildings), or the parcel has no buildings.
        """
        if not self.gate(pin):
            return None
        data = self.cached_data(pin)
        if data is None:
            return None
        buildings = data.get("buildings") or []
        if not buildings:
            return None

        from urbanlens.dashboard.services.pins.auto_nest import request_sweep
        from urbanlens.dashboard.services.pins.pin_restructure import importable_building_indexes, property_polygon

        children = list(pin.descendants().select_related("location__wiki"))
        boundary = property_polygon(pin)
        importable = importable_building_indexes(pin, buildings, children, boundary)
        rows = building_rows(buildings, children, boundary_polygon=boundary)
        if any(not row["child_uuid"] for row in rows):
            request_sweep(pin)
        serialized = [
            {
                "name": row["name"],
                "building_number": row["building_number"],
                "year_built": row["year_built"],
                "source": row["source"],
                "source_label": row["source_label"],
                "latitude": row["latitude"],
                "longitude": row["longitude"],
                "has_geometry": row["has_geometry"],
                "geometry": row["geometry"],
                # Renamed from building_rows' marker-neutral child_* keys: on this surface the
                # children really are pins, and a uuid (not the slug the web panel links by) is what
                # the mobile pin endpoints address a pin with.
                "child_pin_uuid": row["child_uuid"] or None,
                "child_pin_name": row["child_name"] or None,
                # Whether "add this building" would actually produce a pin.
                "can_create": not row["child_name"] and row["record_index"] in importable,
            }
            for row in rows
        ]
        return {
            PanelApiKind.BUILDINGS.value: serialized,
            "provider": data.get("provider") or "",
            # Exactly the rows flagged can_create, so the number and the list it ships beside can
            # never disagree inside one response.
            "unpinned_count": sum(1 for entry in serialized if entry["can_create"]),
        }


class ParcelBuildingsEnrichmentSource(LocationCacheEnrichmentSource):
    """Background-fills the parcel buildings cache per Location - what the wiki page reads."""

    key: ClassVar[str] = PARCEL_BUILDINGS_CACHE_SOURCE
    verbose_name: ClassVar[str] = "Buildings on the parcel"
    cache_source: ClassVar[str] = PARCEL_BUILDINGS_CACHE_SOURCE
    calls_per_item: ClassVar[int] = 2

    @property
    def service_keys(self) -> tuple[str, ...]:
        """What the providers that can be asked spend."""
        return service_keys()

    def gate(self) -> bool:
        """
        Requires a provider the background cycle may ask for every location; the public one is too costly to ask of all of them.
        Without one the cycle picks candidates, every fetch comes back empty, and the run caches an empty list for each location."""
        return bulk_enrichment_available()

    def fetch(self, location: Location) -> tuple[dict | None, str]:
        """Enumerate the location's parcel buildings and return them for caching."""
        payload = fetch_parcel_buildings(location)
        return payload, f"{float(location.latitude or 0):.5f},{float(location.longitude or 0):.5f}"

    def enrich(self, location: Location) -> bool:
        """Cache the building list, then act on it."""
        result = super().enrich(location)
        building_list_cached(location)
        return result


class ParcelBuildingsPlugin(UrbanLensPlugin):
    """Lists every building standing on a pinned property, from the sources that contribute building records."""

    name: ClassVar[str] = "parcel_buildings"
    verbose_name: ClassVar[str] = "Parcel Buildings"
    description: ClassVar[str] = (
        "Enumerates every building on a pin's parcel - OpenStreetMap footprints inside the property "
        "boundary, plus whatever other sources contribute building records. Powers the 'Buildings on this Property' panel, the "
        "offer to bulk-create child pins for a multi-building site, and automatic building "
        "classification of child pins."
    )
    author: ClassVar[str] = "UrbanLens"

    # No service defaults of its own: the Overpass service key is registered by the boundary provider chain.

    def setup(self, plugin: PluginContext) -> None:
        """The parcel buildings pin-detail panel and background fill of parcel buildings for every pinned or wiki'd Location."""
        from urbanlens.dashboard.plugins.points import BUILDING_RECORD_PROVIDERS, ENRICHMENT_SOURCES, PANEL_SOURCES

        plugin.extend(PANEL_SOURCES, [ParcelBuildingsPanelSource()])
        plugin.add(BUILDING_RECORD_PROVIDERS, OverpassBuildingRecords(), priority=100)

        plugin.extend(ENRICHMENT_SOURCES, [ParcelBuildingsEnrichmentSource()])
