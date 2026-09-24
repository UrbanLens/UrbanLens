"""Parcel-vs-building scope for pins and wikis.
A ``Pin`` (and its community ``Wiki``) has always doubled as both *the parcel* and *the building* at a location, because for an ordinary place those are the same thing."""

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

#: How close a marker has to be to a known building's coordinate to be considered "at" that building,
#: when no real footprint polygon is available to test containment against.
#: Roughly the footprint radius of a mid-sized structure - tight enough that an entrance pin on the
#: far side of a courtyard doesn't match, loose enough to absorb the coordinate error in a county
BUILDING_MATCH_METERS = 15.0

_METERS_PER_DEGREE_LATITUDE = 111_320.0


def meters_between(latitude_a: float, longitude_a: float, latitude_b: float, longitude_b: float) -> float:
    """Approximate ground distance between two WGS-84 coordinates, in metres.

    Args:
        latitude_a: Latitude of the first point.
        longitude_a: Longitude of the first point.
        latitude_b: Latitude of the second point.
        longitude_b: Longitude of the second point.

    Returns:
        The distance in metres."""
    mean_latitude = radians((latitude_a + latitude_b) / 2)
    delta_latitude = (latitude_a - latitude_b) * _METERS_PER_DEGREE_LATITUDE
    delta_longitude = (longitude_a - longitude_b) * _METERS_PER_DEGREE_LATITUDE * cos(mean_latitude)
    return (delta_latitude**2 + delta_longitude**2) ** 0.5


# Scope


def _children(target: Pin | Wiki):
    """The target's direct child markers, whichever model it is."""
    from urbanlens.dashboard.models.pin.model import Pin

    return target.detail_pins if isinstance(target, Pin) else target.child_wikis


def building_child_count(target: Pin | Wiki) -> int:
    """How many of the target's direct children are typed as buildings.

    Args:
        target: The pin or wiki whose children to count.

    Returns:
        The number of direct children with ``pin_type == PinType.BUILDING`` (0 for an unsaved target, which can't have children yet)."""
    from urbanlens.dashboard.models.pin.model import PinType

    if target.pk is None:
        return 0
    return _children(target).filter(pin_type=PinType.BUILDING).count()


def is_site_scope(target: Pin | Wiki) -> bool:
    """Whether this marker describes a parcel/site rather than a single building.

    Args:
        target: The pin or wiki being rendered.

    Returns:
        True when building-level records would misrepresent this marker, and a summary of the buildings nested under it should be shown instead."""
    cached = getattr(target, "_site_scope_cache", None)
    if cached is not None:
        return cached

    from urbanlens.dashboard.models.pin.model import PinType
    from urbanlens.dashboard.services.places.scope import effective_pin_type

    result = effective_pin_type(target) == PinType.PARCEL

    target._site_scope_cache = result  # noqa: SLF001 - memoizing on the instance we were handed
    return result


# Buildings known on the parcel


def parcel_buildings(location: Location | None) -> list[dict] | None:
    """Every building known on this location's parcel, from cache only.
    Never fetches - the cache is filled by ``ParcelBuildingsPanelSource`` (on demand, when a Private Pin page asks for its panel) and by that plugin's background enrichment source, so a page render only ever reads it.

    Args:
        location: The location whose parcel to look up; None is tolerated.

    Returns:
        The building records, ``[]`` when the providers were asked and found none, or None when nothing has ever been cached for this location."""
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
        buildings: Building records (each optionally carrying ``latitude``/``longitude``).
        latitude: WGS-84 latitude of the query point.
        longitude: WGS-84 longitude of the query point.
        within_meters: When given, return None unless the closest building is at least this close.

    Returns:
        The nearest building record, or None when there are none (or none within ``within_meters``)."""
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


# Markers nested under a site


def nested_locations(location: Location) -> list[Location]:
    """Every location a pin or wiki nested under one standing on ``location`` stands on.

    Args:
        location: The site's location.

    Returns:
        The distinct nested locations, ``location`` itself excluded, each with its ``place`` joined.
    """
    from urbanlens.dashboard.models.location.model import Location as LocationModel
    from urbanlens.dashboard.models.pin.model import Pin
    from urbanlens.dashboard.models.wiki.model import Wiki

    pin_locations = Pin.objects.filter(location=location).with_descendants().values_list("location_id", flat=True)
    wiki_locations = Wiki.objects.filter(location=location).with_descendants().values_list("location_id", flat=True)
    ids = {location_id for location_id in (*pin_locations, *wiki_locations) if location_id is not None and location_id != location.pk}
    return list(LocationModel.objects.filter(pk__in=ids).select_related("place").order_by("pk"))


def enclosing_site_locations(location: Location) -> list[Location]:
    """The locations of the outermost pins and wikis that markers standing on ``location`` are nested under.

    Args:
        location: A nested marker's location.

    Returns:
        The distinct root locations other than ``location``, in the order found.
    """
    from urbanlens.dashboard.models.pin.model import Pin
    from urbanlens.dashboard.models.wiki.model import Wiki

    roots: dict[int, Location] = {}
    markers: list[Pin | Wiki] = [
        *Pin.objects.filter(location=location, parent_pin__isnull=False).order_by("pk"),
        *Wiki.objects.filter(location=location, parent_wiki__isnull=False).order_by("pk"),
    ]
    for marker in markers:
        root = _outermost(marker)
        if root is not None and root.location_id is not None and root.location_id != location.pk:
            roots.setdefault(root.location_id, root.location)
    return list(roots.values())


def _outermost(marker: Pin | Wiki) -> Pin | Wiki | None:
    """The root of a marker's own hierarchy, stopping at a cycle."""
    from urbanlens.dashboard.models.pin.model import Pin

    if isinstance(marker, Pin):
        chain = marker.ancestor_chain()
        return chain[-1] if chain else None
    seen = {marker.pk}
    current = marker.parent_wiki
    root = None
    while current is not None and current.pk not in seen:
        seen.add(current.pk)
        root = current
        current = current.parent_wiki
    return root


# Automatic classification


def looks_like_a_building(location: Location | None) -> bool:
    """Whether a coordinate sits on a known building footprint.

    Args:
        location: The location to test; None is tolerated.

    Returns:
        True when this coordinate is on a building."""
    from urbanlens.dashboard.models.place.model import PlaceKind

    if location is None or location.latitude is None or location.longitude is None:
        return False

    if location.place_id and location.place is not None and location.place.kind == PlaceKind.BUILDING:
        return True

    buildings = parcel_buildings(location) or []
    return nearest_building(buildings, float(location.latitude), float(location.longitude), within_meters=BUILDING_MATCH_METERS) is not None


def classify_building_pin_type(target: Pin | Wiki) -> bool:
    """Type an unclassified marker as a building when it stands on one.

    Args:
        target: The pin or wiki to classify.

    Returns:
        True when the target was reclassified and saved."""
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

    Args:
        place: The place whose markers to re-derive.

    Returns:
        How many pins and wikis were retyped."""
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
