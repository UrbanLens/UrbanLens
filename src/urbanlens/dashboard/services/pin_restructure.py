"""Suggest and apply hierarchy fixes for a pin that covers a whole property.

Two problems share one answer. A campus pinned once has a hundred unmodelled
buildings under it; and a user who pinned those buildings *before* child pins
existed has a hundred top-level pins scattered across their map that all
belong under one property. Both are "this pin's hierarchy doesn't match the
ground", both are detectable from the same parcel data, and both are tedious
to fix by hand - so they are offered together, once, as a single suggestion
(see ``controllers.pin_restructure``).

Nothing here ever acts on its own: every function is either a read
("what *would* change?") or an explicit apply the owner asked for. The
suggestion is gated three ways - the owner's ``Profile.suggest_pin_restructure``
setting, a permanent per-pin ``Pin.restructure_offer_dismissed``, and simply
having nothing to suggest.

Matching an existing marker to a building prefers the building's own footprint
polygon over a proximity radius: on a dense campus, "within 15 m of the
centroid" both misses a pin at the far end of a long hall and wrongly claims
one standing on the neighbouring building. REData publishes real footprints for
most buildings it knows (``geometry``, standard GeoJSON), so containment is
used whenever one is available and the radius is only the fallback for sources
that publish a bare centroid.
"""

from __future__ import annotations

from dataclasses import dataclass, field
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
    from urbanlens.dashboard.models.profile.model import Profile

logger = logging.getLogger(__name__)

#: Cap on how many child pins one apply will create or nest. Far above any real
#: parcel (the largest campus this was built for has ~100 buildings), purely a
#: backstop against a provider returning something pathological.
MAX_RESTRUCTURE_ITEMS = 500


# ----------------------------------------------------------------------
# Geometry helpers
# ----------------------------------------------------------------------


def building_footprint(building: dict[str, Any]) -> GEOSGeometry | None:
    """The building's own footprint polygon, when its source published one.

    Args:
        building: A cached building record (see ``plugins.builtin.parcel_buildings``).

    Returns:
        The footprint as a GEOS geometry, or None when the record carries only
        a point (or nothing parseable - a malformed ``geometry`` is treated as
        "no footprint", never an error).
    """
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

    Prefers the building's real footprint: a marker anywhere inside it counts,
    however far from the centroid, and a marker just outside it does not -
    which is exactly the distinction a fixed radius gets wrong on a campus of
    long halls and tightly packed outbuildings. Falls back to
    ``site_scope.BUILDING_MATCH_METERS`` from the centroid for sources that
    publish no footprint.

    Args:
        building: A cached building record.
        marker: A child pin or child wiki.

    Returns:
        True when this building is already covered by that marker.
    """
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
        The covering marker, or None. When several qualify (overlapping
        footprints, or two pins inside one building) the nearest to the
        building's centroid wins, so the remaining markers stay available for
        the buildings they are actually closest to.
    """
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
        The subset of ``buildings`` with usable coordinates and no covering
        marker. Each marker is consumed by at most one building, so on a dense
        campus one pin can't silently mark several footprints as done.
    """
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


# ----------------------------------------------------------------------
# What could be restructured
# ----------------------------------------------------------------------


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

    ``Boundary.effective_polygon_for_pin`` synthesizes a 50 m circle for any
    location with no known parcel, which must never drive a nesting
    suggestion - every pin within a city block of a house would look like it
    belongs to it.

    Args:
        pin: The pin whose property boundary to resolve.

    Returns:
        A real (drawn, community, or provider-generated) property polygon, or None.
    """
    from urbanlens.dashboard.models.boundary.model import Boundary, BoundaryType

    polygon, source = Boundary.objects.resolve_for_pin(pin, BoundaryType.PROPERTY)
    return polygon if source != "circle" else None


def nestable_root_pins(pin: Pin) -> list[Pin]:
    """The owner's other top-level pins standing inside this pin's property.

    These are almost always pins made before child pins existed (or imported
    in bulk) that describe buildings on a property the owner has since pinned
    as a whole.

    Args:
        pin: The prospective parent pin.

    Returns:
        Top-level pins of the same profile whose coordinates fall inside the
        property boundary, excluding this pin and anything that would form a
        cycle. Empty when the property has no real boundary.
    """
    if pin.pk is None:
        return []
    polygon = property_polygon(pin)
    if polygon is None:
        return []

    candidates = (
        Pin.objects.filter(profile_id=pin.profile_id, parent_pin__isnull=True, location__point__within=polygon)
        .exclude(pk=pin.pk)
        .select_related("location")
        .order_by("name")
    )
    # would_create_cycle also covers the case of this pin itself being nested
    # under one of the candidates - re-parenting that candidate beneath this
    # pin would close a loop.
    return [candidate for candidate in candidates[: MAX_RESTRUCTURE_ITEMS + 1] if not candidate.would_create_cycle(pin)]


def plan_for(pin: Pin) -> RestructurePlan:
    """What this pin's restructure suggestion would change.

    A read-only survey: it consults only already-cached parcel data and the
    owner's own pins, and never contacts an external service.

    Args:
        pin: The pin being viewed.

    Returns:
        The plan; check ``is_empty`` before offering anything.
    """
    return RestructurePlan(buildings=missing_buildings(pin), nestable=nestable_root_pins(pin))


def missing_buildings(pin: Pin) -> list[dict[str, Any]]:
    """Buildings on this pin's parcel that no child pin covers yet.

    Returns nothing for a single-building place: one structure on its own lot
    *is* the pin, and offering to nest a lone child under it would be noise.
    See ``site_scope.MULTI_BUILDING_THRESHOLD``.

    Args:
        pin: The parent pin.

    Returns:
        Uncovered building records, or ``[]``.
    """
    buildings = site_scope.parcel_buildings(pin.location) or []
    if len(buildings) < site_scope.MULTI_BUILDING_THRESHOLD:
        return []
    return unmatched_buildings(buildings, list(pin.detail_pins.select_related("location")))


def should_offer(pin: Pin) -> bool:
    """Whether this pin may show a restructure suggestion at all.

    Checks only the cheap gates (settings, dismissal, hierarchy position), not
    whether there is anything to suggest - see :func:`plan_for` for that.

    Args:
        pin: The pin being viewed.

    Returns:
        True when a suggestion would be welcome.
    """
    if pin.restructure_offer_dismissed or pin.parent_pin_id is not None:
        return False
    return bool(pin.profile.suggest_pin_restructure)


# ----------------------------------------------------------------------
# Applying it
# ----------------------------------------------------------------------


def building_name(building: dict[str, Any]) -> str:
    """A usable marker name for a building record.

    Args:
        building: A cached building record.

    Returns:
        The building's own name, else "Building <number>", else "" - which
        leaves the pin unnamed, falling back to its location's display name
        exactly like any other nameless pin.
    """
    name = (building.get("name") or "").strip()
    if name:
        return name
    number = str(building.get("building_number") or "").strip()
    return f"Building {number}" if number else ""


def create_building_pins(pin: Pin, buildings: list[dict[str, Any]]) -> int:
    """Create a child pin for each given building, in one transaction.

    Args:
        pin: The parent pin.
        buildings: Building records to create pins for.

    Returns:
        How many child pins were created.
    """
    from urbanlens.dashboard.controllers.detail_pins import _location_for_coords

    created = 0
    with transaction.atomic():
        for building in buildings[:MAX_RESTRUCTURE_ITEMS]:
            name = building_name(building)
            Pin.objects.create(
                name=name or None,
                # Derived from a building record, not typed by the user, so
                # external name refreshes may still improve it later.
                name_is_user_provided=False,
                pin_type=PinType.BUILDING,
                # Likewise derived: the coordinate came from a building
                # footprint, so this is exactly the conclusion the classifier
                # would have reached on its own - no need to queue one task per
                # building to re-derive it.
                pin_type_is_user_provided=False,
                parent_pin=pin,
                profile=pin.profile,
                location=_location_for_coords(building["latitude"], building["longitude"]),
            )
            created += 1
    return created


def nest_root_pins(pin: Pin, candidates: list[Pin]) -> int:
    """Re-parent top-level pins under this pin, keeping everything else about them.

    Only the ``parent_pin`` link changes - names, notes, photos, labels, visit
    history, and the pins' own children all travel with them. Nothing is
    deleted or merged.

    Args:
        pin: The new parent.
        candidates: Top-level pins to nest.

    Returns:
        How many pins were nested.
    """
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
    """Mirror imported buildings as child wikis, when the place already has a wiki.

    Never creates a wiki - community pages are only ever created explicitly
    (see ``services.locations.creation.WikiCreationService``). When one already
    exists, though, its readers benefit from the same building markers.

    Args:
        pin: The parent pin, whose location's wiki is the parent wiki.
        buildings: The building records just imported.
        profile: The profile to attribute the resulting WikiEdit to.

    Returns:
        How many child wikis were created.
    """
    from urbanlens.dashboard.controllers.detail_pins import _location_for_child_wiki
    from urbanlens.dashboard.models.wiki.model import Wiki
    from urbanlens.dashboard.models.wiki_edit import WikiEdit

    try:
        wiki = pin.location.wiki
    except ObjectDoesNotExist:
        return 0

    unmatched = list(wiki.child_wikis.select_related("location"))
    created = 0
    with transaction.atomic():
        for building in buildings[:MAX_RESTRUCTURE_ITEMS]:
            existing = match_marker(building, unmatched)
            if existing is not None:
                unmatched.remove(existing)
                continue
            Wiki.objects.create(
                name=building_name(building) or wiki.name,
                pin_type=PinType.BUILDING,
                pin_type_is_user_provided=False,
                parent_wiki=wiki,
                location=_location_for_child_wiki(building["latitude"], building["longitude"]),
            )
            created += 1

    if created:
        # One entry for the whole import: a hundred separate "child_wiki_added"
        # rows would bury every other edit in the wiki's history.
        WikiEdit.objects.create(
            wiki=wiki,
            editor=profile,
            changes={"child_wikis_imported": {"from": None, "to": f"{created} building markers"}},
        )
    return created
