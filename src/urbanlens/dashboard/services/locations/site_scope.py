"""Parcel-vs-building scope for pins and wikis.

A ``Pin`` (and its community ``Wiki``) has always doubled as both *the parcel*
and *the building* at a location, because for an ordinary place those are the
same thing. On a multi-building site they are not: a campus pin rendering
"TOOL SHED (1937), Building Number 154" from NY SHPO's CRIS inventory is
describing one arbitrary structure as if it were the whole property.

This module is the single place that answers "is this marker a parcel or a
building?", consulted by every building-level panel (see
``plugins.builtin.cris_buildings``, ``redata_building_attributes``,
``overture_building_attributes``) and by both detail pages.

Two rules, in order:

1. **An explicit choice always wins.** ``pin_type_is_user_provided`` marks a
   type the user actually picked out of a dialog rather than one this module
   guessed, mirroring how ``name_is_user_provided`` guards ``Pin.name``.
2. **Otherwise, count the buildings nested under it.** A marker with
   :data:`MULTI_BUILDING_THRESHOLD` or more child markers typed as buildings
   is describing the grounds those buildings sit on, not a building.

Deliberately *not* a rule: "the parcel at these coordinates has several
buildings according to REData". That signal is real, and it does drive the
"organize this property?" suggestion (see ``services.pins.pin_restructure``) - but
on its own it would silently reclassify a house with a detached garage, so it
never flips scope by itself. The user accepting the suggestion creates the
child pins, and *those* flip it.

Child markers are classified automatically (:func:`classify_building_pin_type`)
so none of this asks the user to do extra work: a pin dropped on a building
footprint becomes a building, and a pin dropped on an entrance or a gap
between buildings does not.
"""

from __future__ import annotations

import logging
from math import cos, radians
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from urbanlens.dashboard.models.location.model import Location
    from urbanlens.dashboard.models.pin.model import Pin
    from urbanlens.dashboard.models.wiki.model import Wiki

logger = logging.getLogger(__name__)

#: How many child markers typed as buildings make their parent a parcel. Also
#: the floor for offering to bulk-create building pins from external data - a
#: parcel with one building has nothing to offer and nothing to reclassify.
MULTI_BUILDING_THRESHOLD = 2

#: LocationCache source holding every building known on a location's parcel
#: (see ``plugins.builtin.parcel_buildings``).
PARCEL_BUILDINGS_CACHE_SOURCE = "parcel_buildings"

#: How close a marker has to be to a known building's coordinate to be
#: considered "at" that building, when no real footprint polygon is available
#: to test containment against. Roughly the footprint radius of a mid-sized
#: structure - tight enough that an entrance pin on the far side of a
#: courtyard doesn't match, loose enough to absorb the coordinate error in a
#: county GIS building centroid.
BUILDING_MATCH_METERS = 15.0

_METERS_PER_DEGREE_LATITUDE = 111_320.0


def meters_between(latitude_a: float, longitude_a: float, latitude_b: float, longitude_b: float) -> float:
    """Approximate ground distance between two WGS-84 coordinates, in metres.

    An equirectangular approximation, not a great-circle one: every caller
    here compares points that are metres apart on the same parcel, where the
    error is far below the coordinate precision of the underlying data, and
    this avoids a PostGIS round-trip per building on a 100-building campus.

    Args:
        latitude_a: Latitude of the first point.
        longitude_a: Longitude of the first point.
        latitude_b: Latitude of the second point.
        longitude_b: Longitude of the second point.

    Returns:
        The distance in metres.
    """
    mean_latitude = radians((latitude_a + latitude_b) / 2)
    delta_latitude = (latitude_a - latitude_b) * _METERS_PER_DEGREE_LATITUDE
    delta_longitude = (longitude_a - longitude_b) * _METERS_PER_DEGREE_LATITUDE * cos(mean_latitude)
    return (delta_latitude**2 + delta_longitude**2) ** 0.5


# ----------------------------------------------------------------------
# Scope
# ----------------------------------------------------------------------


def _children(target: Pin | Wiki):
    """The target's direct child markers, whichever model it is."""
    from urbanlens.dashboard.models.pin.model import Pin

    return target.detail_pins if isinstance(target, Pin) else target.child_wikis


def building_child_count(target: Pin | Wiki) -> int:
    """How many of the target's direct children are typed as buildings.

    Args:
        target: The pin or wiki whose children to count.

    Returns:
        The number of direct children with ``pin_type == PinType.BUILDING``
        (0 for an unsaved target, which can't have children yet).
    """
    from urbanlens.dashboard.models.pin.model import PinType

    if target.pk is None:
        return 0
    return _children(target).filter(pin_type=PinType.BUILDING).count()


def is_site_scope(target: Pin | Wiki) -> bool:
    """Whether this marker describes a parcel/site rather than a single building.

    Memoized on the instance: a single pin-detail page render asks this from
    several independent panels, and the answer can't change mid-request.

    Args:
        target: The pin or wiki being rendered.

    Returns:
        True when building-level records would misrepresent this marker, and
        a summary of the buildings nested under it should be shown instead.
    """
    cached = getattr(target, "_site_scope_cache", None)
    if cached is not None:
        return cached

    from urbanlens.dashboard.models.pin.model import PinType
    from urbanlens.dashboard.services.places.scope import effective_pin_type

    result = effective_pin_type(target) == PinType.PARCEL

    target._site_scope_cache = result  # noqa: SLF001 - memoizing on the instance we were handed
    return result


# ----------------------------------------------------------------------
# Buildings known on the parcel
# ----------------------------------------------------------------------


def parcel_buildings(location: Location | None) -> list[dict] | None:
    """Every building known on this location's parcel, from cache only.

    Never fetches - the cache is filled by ``ParcelBuildingsPanelSource`` (on
    demand, when a pin detail page asks for its panel) and by that plugin's
    background enrichment source, so a page render only ever reads it.

    Args:
        location: The location whose parcel to look up; None is tolerated.

    Returns:
        The building records, ``[]`` when the providers were asked and found
        none, or None when nothing has ever been cached for this location.
    """
    from urbanlens.dashboard.models.cache.location_cache import LocationCache

    if location is None:
        return None
    cached = LocationCache.get_fresh(location, PARCEL_BUILDINGS_CACHE_SOURCE)
    if cached is None:
        return None
    return list((cached.data or {}).get("buildings") or [])


def has_multiple_buildings(location: Location | None) -> bool:
    """True when the parcel at this location is known to hold several buildings."""
    buildings = parcel_buildings(location)
    return buildings is not None and len(buildings) >= MULTI_BUILDING_THRESHOLD


def nearest_building(buildings: list[dict], latitude: float, longitude: float, *, within_meters: float | None = None) -> dict | None:
    """The building record closest to a coordinate.

    Args:
        buildings: Building records (each optionally carrying
            ``latitude``/``longitude``).
        latitude: WGS-84 latitude of the query point.
        longitude: WGS-84 longitude of the query point.
        within_meters: When given, return None unless the closest building is
            at least this close.

    Returns:
        The nearest building record, or None when there are none (or none
        within ``within_meters``).
    """
    best: dict | None = None
    best_distance = float("inf")
    for building in buildings:
        lat, lng = building.get("latitude"), building.get("longitude")
        if lat is None or lng is None:
            continue
        distance = meters_between(float(lat), float(lng), latitude, longitude)
        if distance < best_distance:
            best, best_distance = building, distance
    if best is None:
        return None
    if within_meters is not None and best_distance > within_meters:
        return None
    return best


# ----------------------------------------------------------------------
# Automatic classification
# ----------------------------------------------------------------------


def looks_like_a_building(location: Location | None) -> bool:
    """Whether a coordinate sits on a known building footprint.

    Two independent signals, cheapest first:

    1. The location resolved onto a ``BUILDING`` place. A building place only
       exists because some provider - county GIS via REData, OSM via Overpass,
       Overture, Microsoft/Google footprints - published a footprint, and the
       location only resolved onto it because that footprint *contains* this
       exact point, so the resolution is itself the answer.
    2. Failing that, proximity to a building record on the parcel (see
       :func:`parcel_buildings`), which covers sources that only publish a
       centroid rather than a footprint.

    Args:
        location: The location to test; None is tolerated.

    Returns:
        True when this coordinate is on a building.
    """
    from urbanlens.dashboard.models.place.model import PlaceKind

    if location is None or location.latitude is None or location.longitude is None:
        return False

    if location.place_id and location.place is not None and location.place.kind == PlaceKind.BUILDING:
        return True

    buildings = parcel_buildings(location) or []
    return nearest_building(buildings, float(location.latitude), float(location.longitude), within_meters=BUILDING_MATCH_METERS) is not None


def classify_building_pin_type(target: Pin | Wiki) -> bool:
    """Type an unclassified marker as a building when it stands on one.

    This answers an *observation* - "is there a structure under this marker?" -
    not the scope question :func:`is_site_scope` answers. The two are
    deliberately separate: a pin on the only building of an ordinary house is
    standing on a building (so a child marker there types as one) while the
    property as a whole still commits to no scope, because parcel and building
    are the same thing there.

    A no-op for anything the user classified themselves, and for a marker that
    isn't on a building - the latter keeps whatever provisional type it was
    created with (Point of Interest), which is exactly right for the entrances,
    hazards, and landmarks users also drop on a pin's map.

    Args:
        target: The pin or wiki to classify.

    Returns:
        True when the target was reclassified and saved.
    """
    from urbanlens.dashboard.models.pin.model import PinType

    if target.pin_type_is_user_provided or target.pin_type == PinType.BUILDING:
        return False
    if not looks_like_a_building(target.location):
        return False

    target.pin_type = PinType.BUILDING
    # A plain save (not queryset.update) so the post_save side effects other
    # features hang off - smart-list/saved-filter resync in particular - still
    # fire for the reclassification. This never runs inside a signal handler.
    target.save(update_fields=["pin_type", "updated"])
    logger.debug("Classified %s %s as a building", type(target).__name__, target.pk)
    return True


def reclassify_markers_on_place(place) -> int:
    """Re-derive the cached scope of every marker standing on a place.

    Scope is a property of the *place*, not of whoever pinned it: the moment a
    property is known to hold several buildings, every user's marker on it is
    describing the grounds, not just the marker belonging to whoever pressed
    the import button. Deriving from the place and fanning out is what keeps
    those consistent - and what makes the answer survive buildings being added
    or removed later.

    Deliberately a no-op on an ordinary single-building property. There is no
    scope to assert there, so rewriting markers would only overwrite the
    observations :func:`classify_building_pin_type` recorded.

    Args:
        place: The place whose markers to re-derive.

    Returns:
        How many pins and wikis were retyped.
    """
    from urbanlens.dashboard.models.pin.model import Pin, PinType
    from urbanlens.dashboard.models.wiki.model import Wiki
    from urbanlens.dashboard.services.places.scope import pin_type_for_place

    if place is None:
        return 0
    implied = pin_type_for_place(place)
    if implied is None or implied == PinType.LOCATION_MARKER:
        return 0

    scoped = {PinType.LOCATION_MARKER, PinType.PARCEL, PinType.BUILDING}
    retyped = 0
    retyped += Pin.objects.filter(location__place=place, pin_type_is_user_provided=False, pin_type__in=scoped).exclude(pin_type=implied).update(pin_type=implied)
    retyped += Wiki.objects.filter(place=place, pin_type_is_user_provided=False, pin_type__in=scoped).exclude(pin_type=implied).update(pin_type=implied)
    return retyped
