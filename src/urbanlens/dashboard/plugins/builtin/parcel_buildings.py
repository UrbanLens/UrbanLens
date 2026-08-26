"""Parcel buildings plugin: every structure standing on a pin's property.

The counterpart to ``plugins.builtin.redata_building_attributes``, which
answers "what is *this* building?" - this one answers "what buildings are on
this property?", which is the only sensible question for a campus, a mill
complex, or an institutional site where one pin covers a hundred structures.

Two providers, in order:

1. **REData** (``RedataGateway.lookup_parcel_uuid`` → ``lookup_buildings``),
   which combines a county's own building-footprint layer with NY SHPO's CRIS
   inventory and is the only source that carries real building *numbers* and
   *names* - the "Building 154 / Tool Shed" identifiers a site's own signage
   and paperwork use.
2. **Overpass** (``OverpassGateway.buildings_within``) against the location's
   effective property boundary, for anywhere REData has no parcel coverage.
   OSM footprints have no building numbers, but a named-and-located list still
   beats nothing.

The cached list is what powers the "Buildings on this Property" panel on both
the pin detail page and the wiki, the "would you like to add pins for the
buildings here?" offer, and the child-pin classifier's fallback proximity test
(see ``services.locations.site_scope``).
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any, ClassVar

from urbanlens.dashboard.plugins.base import UrbanLensPlugin
from urbanlens.dashboard.services.locations.enrichment import LocationCacheEnrichmentSource
from urbanlens.dashboard.services.locations.site_scope import PARCEL_BUILDINGS_CACHE_SOURCE
from urbanlens.dashboard.services.pins.external_data import LocationCachePanelSource, PanelApiKind

if TYPE_CHECKING:
    from django.contrib.gis.geos import GEOSGeometry

    from urbanlens.dashboard.models.location.model import Location
    from urbanlens.dashboard.models.pin.model import Pin
    from urbanlens.dashboard.services.locations.enrichment import EnrichmentSource
    from urbanlens.dashboard.services.pins.external_data import PanelSource

logger = logging.getLogger(__name__)

#: Human-readable labels for each provider's own reporting system, for the
#: per-row source chip. Shared with ``redata_building_attributes``, which shows
#: the same provenance on its own card.
#:
#: Every tag in REData's ``BUILDING_SOURCES`` needs an entry: a key with no
#: label renders as no chip at all, so a building whose only source is missing
#: here reads as if its provenance were unknown. Four of the six were absent
#: until 2026-08-19, including ``overpass`` - which REData runs by default.
#: ``osm`` is not one of REData's; it is this plugin's own fallback tag for the
#: same OpenStreetMap data, which is why both map to one label.
SOURCE_LABELS: dict[str, str] = {
    "county_gis": "County GIS",
    "assessor": "County Assessor",
    "cris": "NY SHPO (CRIS)",
    "overpass": "OpenStreetMap",
    "osm": "OpenStreetMap",
    "microsoft_buildings": "Microsoft Buildings",
    "google_open_buildings": "Google Open Buildings",
}


def source_chips(sources: list[str]) -> list[str]:
    """Human-readable provenance chips for one building's source keys.

    Args:
        sources: Source keys from :func:`record_sources`.

    Returns:
        Labels in the order given, without repeats - ``overpass`` and ``osm``
        share a label, so a record referencing both would otherwise render
        "OpenStreetMap + OpenStreetMap".
    """
    chips: list[str] = []
    for key in sources:
        label = SOURCE_LABELS.get(key)
        if label and label not in chips:
            chips.append(label)
    return chips


def record_sources(building: dict[str, Any]) -> list[str]:
    """Every source that references one building, richest-information first.

    REData's ``/parcels/{uuid}/buildings/`` now returns one record per physical
    building with a ``sources[]`` array (see its
    ``docs/buildings-dedup-spec.md``), having removed the top-level ``source``
    string a single-observation record used to carry. Reading only the old key
    leaves the provenance chip blank for every REData building; reading only
    the new one drops Overpass, which still answers in the flat shape.

    Args:
        building: A raw building record from either shape.

    Returns:
        Source keys in the order REData ordered them (``BUILDING_SOURCES``
        precedence, so the first is the richest), or a single-entry list for a
        flat record, or empty when neither key is present.
    """
    sources = building.get("sources")
    if isinstance(sources, list):
        keys = [key for entry in sources if isinstance(entry, dict) and (key := entry.get("source"))]
        if keys:
            return keys
    return [key] if (key := building.get("source")) else []


def buildings_on_property(buildings: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """The records that stand on this property, dropping the ones that don't.

    REData over-returns deliberately and labels what it returns. A parcel
    inside a broad CRIS archaeological sensitivity zone gets every surveyed
    building in that zone - roughly a thousand acres' worth here - each flagged
    ``is_on_property: false`` precisely so a consumer can drop it. CRIS's own
    survey roster for this campus is 124 buildings, against the 2604 that
    reached the UI.

    Deliberately *not* filtered here: a record carrying ``child_refs``. Those
    are excluded from counts (see :func:`countable_buildings`) but not from the
    list, because a parent is often a real building in its own right - a large
    building whose wings are separately mapped becomes their parent while
    remaining the building people actually name the site after. Dropping it
    would delete the most significant structure on a campus.

    Args:
        buildings: Raw building records from either provider.

    Returns:
        Only the records on this property. An absent flag is kept: Overpass
        rows carry none, and a missing label is not a negative one.
    """
    return [b for b in buildings if b.get("is_on_property") is not False]


def countable_buildings(buildings: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """The records to count, one per physical building.

    A record carrying ``child_refs`` contains other records in this same list,
    so counting it as well as its children counts a subdivided structure twice.
    Counting the leaves is REData's own stated rule.

    Args:
        buildings: Raw building records from either provider.

    Returns:
        The on-property records that are nobody's parent.
    """
    return [b for b in buildings_on_property(buildings) if not b.get("child_refs")]


def confident_buildings(buildings: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """The on-property records certain enough to act on without asking.

    The dividing line is ``overlap_refs``: REData sets it when a building's
    geometry substantially overlaps another source's building without being
    contained in it or being obviously the same structure - the one case its
    reconciliation deliberately refuses to resolve, because merging would
    destroy real information. If REData cannot say what the record is, neither
    can we, so those wait for a person (the "add buildings" dialog) instead of
    becoming pins on their own.

    Containment is not ambiguity: a parent and its ``child_refs`` children are
    a *verified* relationship, so nested buildings are confident on both sides.

    Args:
        buildings: Raw building records from either provider.

    Returns:
        The on-property records with no unresolved overlap.
    """
    return [b for b in buildings_on_property(buildings) if not b.get("overlap_refs")]


def _tree_ordered(rows: list[dict[str, Any]], *, annotate_depth: bool = True) -> list[dict[str, Any]]:
    """Order rows so each building is followed by the buildings inside it.

    REData reports nesting rather than flattening it, and it is not a flat
    parent/child pair: a child links to its *most specific* container, and that
    container may itself be someone else's child, so the structure is a tree of
    arbitrary depth (a campus block parenting a wing parenting an annex). Nor is
    it always cross-source - OSM models a ``building`` outline over its own
    ``building:part`` segments.

    Each row gains a ``depth``, so a template can indent without walking the
    tree itself.

    Args:
        rows: Rendered rows, each possibly carrying ``ref``/``parent_ref``.

    Returns:
        The same rows, depth-first, siblings in :func:`_row_sort_key` order.
    """
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
    """Order raw building records parents-first, children directly after.

    The importer needs this ordering to mirror REData's building tree as a pin
    tree: a child's pin can only be parented under its building's pin if that
    pin exists by the time the child is created.

    Args:
        buildings: Raw building records carrying ``ref``/``parent_ref``.

    Deliberately does *not* annotate depth: raw cached records are hashed
    whole by ``pin_restructure.building_selection_key``, so writing any key
    into them silently changes their identity and breaks every place/selection
    lookup keyed on it.

    Returns:
        The same records, depth-first (see :func:`_tree_ordered`).
    """
    return _tree_ordered(buildings, annotate_depth=False)


def fetch_parcel_buildings(location: Location) -> dict[str, Any]:
    """Resolve every building on a location's parcel, REData first then Overpass.

    Failures are tolerated the same way ``redata_building_attributes`` and
    ``cris_buildings`` tolerate theirs (broad catch, cache an empty result):
    a missing building list is a low-stakes gap, and retrying it on every
    background enrichment cycle isn't worth the added complexity.

    Args:
        location: The location whose parcel to enumerate.

    Returns:
        ``{"buildings": [...], "provider": "redata"|"osm"}``, or ``{}`` when
        neither provider found anything.
    """
    from urbanlens.dashboard.services.apis.property_records.redata_gateway import PropertyRecordsUnavailableError, RedataGateway

    latitude = float(location.latitude or 0)
    longitude = float(location.longitude or 0)

    try:
        gateway = RedataGateway()
        parcel_uuid = gateway.lookup_parcel_uuid(latitude, longitude)
        buildings = gateway.lookup_buildings(parcel_uuid) if parcel_uuid else []
    except (PropertyRecordsUnavailableError, ValueError):
        logger.debug("parcel_buildings: REData unavailable near %.2f,%.2f", latitude, longitude, exc_info=True)
        buildings = []

    if buildings:
        return {"buildings": list(buildings), "provider": "redata"}

    osm_buildings = _overpass_buildings(location)
    if osm_buildings:
        return {"buildings": osm_buildings, "provider": "osm"}
    return {}


def _overpass_buildings(location: Location) -> list[dict[str, Any]]:
    """OSM buildings inside the location's effective property boundary.

    Skipped entirely when the only "boundary" available is the synthesized
    default circle (see ``Boundary.effective_polygon``) - counting the
    buildings inside an arbitrary 50 m disc around a coordinate would report
    a neighbour's house as being on this parcel.

    Args:
        location: The location whose property boundary bounds the search.

    Returns:
        Building records, or ``[]`` when there's no real boundary to search
        inside or Overpass found nothing.
    """
    from urbanlens.dashboard.services.apis.locations.boundaries.overpass import OverpassGateway
    from urbanlens.dashboard.services.places.scope import parcel_polygon_for_location

    polygon = parcel_polygon_for_location(location)
    if polygon is None:
        return []

    try:
        return OverpassGateway().buildings_within(polygon)
    except Exception:
        # Matches OverpassGateway's own callers (services.locations.boundaries):
        # every failure mode here - transient mirror outage, rate limit, a
        # malformed ring - is a missing list, never a broken page.
        logger.debug("parcel_buildings: Overpass lookup failed for location %s", location.pk, exc_info=True)
        return []


def building_rows(buildings: list[dict[str, Any]], children: list, url_for=None, boundary_polygon: GEOSGeometry | None = None) -> list[dict[str, Any]]:
    """Pair each known building with the child marker that already covers it.

    Shared by the pin detail panel and the wiki's equivalent view so both
    render identically from the same cached data - the only difference being
    whether ``children`` are child pins or child wikis, which both expose the
    same ``effective_latitude``/``effective_longitude``/``pin_type`` surface.

    Matching is delegated to ``services.pins.pin_restructure.match_marker`` so this
    panel's idea of "already pinned" can never drift from what the restructure
    suggestion would actually create.

    Args:
        buildings: Cached building records (see :func:`fetch_parcel_buildings`).
        children: The marker's direct children, to match against.
        url_for: Optional callable turning a matched child into a link target;
            omit for child wikis, which are markers on their parent's page
            rather than pages of their own.
        boundary_polygon: The property's real (non-circle) boundary, when
            known. REData's per-parcel building list isn't guaranteed to
            align with our own boundary - a building that falls outside it is
            dropped, unless a child marker already covers it (a pinned
            building belongs on the list regardless of where the parcel data
            says it sits). Pass None to skip this check entirely, which is
            right wherever there's no real boundary to test against (only the
            synthesized fallback circle) - filtering against an arbitrary
            circle would drop real buildings for no reason.

    Returns:
        One row per building, sorted by building number then name, each with
        ``name``, ``building_number``, ``year_built``, ``source``,
        ``source_label``, ``latitude``, ``longitude``, ``geometry``,
        ``has_geometry``, ``child_name``, ``child_uuid``, and ``child_url`` -
        the child fields empty/None when this building has no marker yet.

        ``child_*`` are deliberately marker-neutral rather than ``child_pin_*``:
        the wiki view passes child *wikis* here, so naming them for pins would
        be a lie on one of the two callers. The external API renames them to
        ``child_pin_*`` at its own boundary, where they really are pins.
    """
    from urbanlens.dashboard.services.pins.pin_restructure import match_marker

    rows: list[dict[str, Any]] = []
    unmatched = list(children)
    for building in buildings_on_property(buildings):
        child = match_marker(building, unmatched)
        if child is not None:
            # One child can only stand for one building - on a dense campus
            # the same pin would otherwise claim several neighbouring
            # footprints and leave real ones looking unpinned.
            unmatched.remove(child)
        elif boundary_polygon is not None and not building_within_boundary(building, boundary_polygon):
            continue
        geometry = building_footprint_geojson(building)
        sources = record_sources(building)
        rows.append(
            {
                "name": building.get("name") or "",
                "building_number": building.get("building_number") or "",
                "year_built": building.get("year_built") or "",
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
                "child_name": _marker_name(child) if child is not None else "",
                "child_uuid": str(child.uuid) if child is not None and getattr(child, "uuid", None) else "",
                "child_url": (url_for(child) if url_for is not None else "") if child is not None else "",
            },
        )

    return _tree_ordered(rows)


def building_within_boundary(building: dict[str, Any], boundary_polygon: GEOSGeometry) -> bool:
    """Whether an unpinned building actually sits on the given boundary.

    Prefers the building's own footprint (an intersection test, so a
    structure straddling the boundary edge still counts) and falls back to
    its centroid when the provider published only a point.

    A provider's own "on property" opinion (REData's ``is_on_property``,
    which :func:`buildings_on_property` filters on) is not guaranteed to
    agree with our own boundary - see this module's docstring - so this is
    the check both :func:`building_rows` (the property panel) and
    ``services.pins.pin_restructure.missing_buildings`` (the "add unpinned
    buildings" dialog, and the button count that promises what it will do)
    run against a *suggestion*, not against something already pinned by hand.

    Args:
        building: One cached building record.
        boundary_polygon: The property's real boundary.

    Returns:
        True when the building overlaps the boundary, or has no usable
        geometry to test at all (never silently drop a record we can't place).
    """
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
    """A building record's real footprint geometry, if it has one.

    REData always returns a ``geometry`` for a building, but degrades to a bare
    GeoJSON ``Point`` when the county's layer carried no outline - and a Point
    is exactly the ``latitude``/``longitude`` already on the record, so keeping
    it would double a hundred-building payload's size to say nothing. Overpass
    records (the OSM fallback) carry no geometry at all. Both cases collapse to
    None, which is what makes ``has_geometry`` a usable "can I draw this
    outline?" flag rather than a "did the provider send a geometry key?" one.

    Named ``..._geojson`` to stay distinct from
    ``services.pins.pin_restructure.building_footprint``, which answers the same
    question for the marker-matching code but returns a parsed *shapely* shape
    (and rejects non-areal geometry) rather than the raw GeoJSON a client wants.

    Args:
        building: One cached building record.

    Returns:
        The GeoJSON geometry dict for a real outline, or None.
    """
    geometry = building.get("geometry")
    if not isinstance(geometry, dict) or geometry.get("type") == "Point":
        return None
    return geometry


def _marker_name(marker) -> str:
    """Display name of a child pin or child wiki (Wiki has no ``effective_name``)."""
    return getattr(marker, "effective_name", None) or getattr(marker, "name", "") or ""


def _row_sort_key(row: dict[str, Any]) -> tuple:
    """Sort buildings by number (numerically when they are numbers), then name.

    Building numbers on a campus are the identifiers people actually navigate
    by, and they are almost always numeric - so "Building 9" has to sort
    before "Building 10", which a plain string sort gets wrong. Anything
    non-numeric falls back to its own string, after all the numbered ones.
    """
    number = str(row.get("building_number") or "").strip()
    if number.isdigit():
        return (0, int(number), "")
    if number:
        return (1, 0, number.casefold())
    return (2, 0, str(row.get("name") or "").casefold())


class ParcelBuildingsPanelSource(LocationCachePanelSource):
    """Every building on the pin's parcel, for the "Buildings on this Property" panel.

    Rendered by ``PinController.parcel_buildings`` rather than the generic
    ``panel_info`` dispatch: each row links to (or offers to create) the child
    pin for that building, which is well past what
    ``_simple_info_panel.html``'s label/value grid can express.
    """

    key = PARCEL_BUILDINGS_CACHE_SOURCE
    cache_source = PARCEL_BUILDINGS_CACHE_SOURCE
    section_id = "parcel-buildings-section"
    icon = "apartment"
    title = "Buildings on this Property"
    # Its own read shape, because neither of the uniform ones fits: a building
    # row is a place with coordinates, a footprint, and a link to the child pin
    # covering it - which is neither an information card nor a media item. This
    # is also where a separately-requested "list a pin's buildings" endpoint
    # ended up: it would have been the same query, the same authorization, and
    # the same cache row as this panel, so folding it in here means one thing
    # to keep correct instead of two that must agree.
    api_kinds: ClassVar[frozenset[PanelApiKind]] = frozenset({PanelApiKind.BUILDINGS})

    def gate(self, pin: Pin) -> bool:
        """Only for a root pin with coordinates - a child pin has no sub-buildings."""
        if pin.parent_pin_id is not None:
            return False
        return bool(pin.effective_latitude and pin.effective_longitude)

    def fetch(self, pin: Pin) -> None:
        """Enumerate the parcel's buildings and cache them against the pin's location."""
        from urbanlens.dashboard.models.cache.location_cache import LocationCache
        from urbanlens.dashboard.services.pins.auto_nest import auto_nest_location

        location = pin.location
        payload = fetch_parcel_buildings(location)
        LocationCache.set(location, self.cache_source, payload, query_key=f"{float(location.latitude or 0):.5f},{float(location.longitude or 0):.5f}")
        # The moment the list lands is the moment the default structure can be
        # built - confident buildings become child pins with no dialog.
        auto_nest_location(location)

    def api_payload(self, pin: Pin) -> dict[str, Any] | None:
        """Every building on the pin's parcel, each paired with its child pin.

        The child-pin pairing is the part a client cannot compute for itself:
        deciding whether an existing marker already "covers" a footprint is a
        geometry-then-nearest-centroid contest (see
        ``services.pins.pin_restructure.match_marker``), and a client guessing at it
        would offer to create duplicate pins for buildings that are already
        pinned. It is resolved server-side through the very same
        :func:`building_rows` the web panel renders, so both surfaces agree on
        which buildings are still unpinned.

        ``geometry`` carries a real outline only when the provider supplied one
        (see :func:`building_footprint`); ``has_geometry`` says so without the
        client having to inspect it. A client drawing markers reads
        ``latitude``/``longitude`` and can ignore the geometry entirely.

        Args:
            pin: The pin whose parcel is being read. Its direct children are
                the markers matched against - a child pin nested deeper is not
                a candidate, matching what the web panel offers to create.

        Returns:
            ``{"buildings": [...], "provider": ..., "unpinned_count": ...}``,
            or None when nothing has landed yet, the gate rejects this pin (a
            child pin has no sub-buildings), or the parcel has no buildings.
        """
        if not self.gate(pin):
            return None
        data = self.cached_data(pin)
        if data is None:
            return None
        buildings = data.get("buildings") or []
        if not buildings:
            return None

        from urbanlens.dashboard.services.pins.pin_restructure import property_polygon

        rows = building_rows(buildings, list(pin.detail_pins.select_related("location")), boundary_polygon=property_polygon(pin))
        return {
            PanelApiKind.BUILDINGS.value: [
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
                    # Renamed from building_rows' marker-neutral child_* keys:
                    # on this surface the children really are pins, and a uuid
                    # (not the slug the web panel links by) is what the mobile
                    # pin endpoints address a pin with.
                    "child_pin_uuid": row["child_uuid"] or None,
                    "child_pin_name": row["child_name"] or None,
                }
                for row in rows
            ],
            "provider": data.get("provider") or "",
            # Counts what the "add buildings" dialog would actually offer, which
            # is every unmatched on-property record including envelope parents -
            # see pin_restructure.missing_buildings, "offer every real one".
            # Excluding parents here (as this did until 2026-08-19) made the
            # mobile panel advertise fewer buildings than the web panel on the
            # subdivided campuses reconciliation exists for.
            "unpinned_count": sum(1 for row in rows if not row["child_name"]),
        }


class ParcelBuildingsEnrichmentSource(LocationCacheEnrichmentSource):
    """Background-fills the parcel buildings cache per Location - what the wiki page reads."""

    key: ClassVar[str] = PARCEL_BUILDINGS_CACHE_SOURCE
    verbose_name: ClassVar[str] = "Buildings on the parcel"
    cache_source: ClassVar[str] = PARCEL_BUILDINGS_CACHE_SOURCE
    service_keys: ClassVar[tuple[str, ...]] = ("redata_api", "overpass")
    calls_per_item: ClassVar[int] = 2

    def gate(self) -> bool:
        """Requires REData to be configured - this source has no other backend.

        Without it the cycle picks candidates, every fetch raises, and the run
        logs one exception per location. Answering here skips the source for
        the whole cycle instead, which is what "unavailable" means to
        ``self_reported_skip``.
        """
        from urbanlens.dashboard.services.apis.locations.redata_context_gateway import redata_configured

        return redata_configured()

    def fetch(self, location: Location) -> tuple[dict | None, str]:
        """Enumerate the location's parcel buildings and return them for caching."""
        payload = fetch_parcel_buildings(location)
        return payload, f"{float(location.latitude or 0):.5f},{float(location.longitude or 0):.5f}"

    def enrich(self, location: Location) -> bool:
        """Cache the building list, then build the default pin structure from it."""
        from urbanlens.dashboard.services.pins.auto_nest import auto_nest_location

        result = super().enrich(location)
        auto_nest_location(location)
        return result


class ParcelBuildingsPlugin(UrbanLensPlugin):
    """Lists every building standing on a pinned property, via REData or OpenStreetMap."""

    name: ClassVar[str] = "parcel_buildings"
    verbose_name: ClassVar[str] = "Parcel Buildings"
    description: ClassVar[str] = (
        "Enumerates every building on a pin's parcel - names and building numbers from REData "
        "(county GIS building-footprint layers plus NY SHPO CRIS), falling back to OpenStreetMap "
        "footprints inside the property boundary. Powers the 'Buildings on this Property' panel, the "
        "offer to bulk-create child pins for a multi-building site, and automatic building "
        "classification of child pins."
    )
    author: ClassVar[str] = "UrbanLens"

    # No get_service_defaults() override - both providers' service keys
    # ("redata_api", "overpass") are already registered by
    # plugins.builtin.property_records and the boundary provider chain.

    def get_panel_sources(self) -> list[PanelSource]:
        """Contribute the parcel buildings pin-detail panel."""
        return [ParcelBuildingsPanelSource()]

    def get_enrichment_sources(self) -> list[EnrichmentSource]:
        """Contribute background-fill of parcel buildings for every pinned/wiki'd Location."""
        return [ParcelBuildingsEnrichmentSource()]
