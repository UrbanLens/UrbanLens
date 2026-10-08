"""Parcel-vs-building scope for pins and wikis.
A ``Pin`` (and its community ``Wiki``) has always doubled as both *the parcel* and *the building* at a location, because for an ordinary place those are the same thing."""

from __future__ import annotations

from dataclasses import dataclass
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


@dataclass(frozen=True, slots=True)
class CachedBuildings:
    """The building list cached for a location's parcel.

    Attributes:
        buildings: The building records; ``[]`` when the providers were asked and found none.
        complete: False when a provider did not answer, so the list is a floor: a building it leaves out may stand
            there, and one it holds may lack what the missing provider knew.
    """

    buildings: list[dict]
    complete: bool


def cached_parcel_buildings(location: Location | None) -> CachedBuildings | None:
    """The building list cached for this location's parcel, and whether it is all of them.
    Never fetches - the cache is filled by ``ParcelBuildingsPanelSource`` (on demand, when a Private Pin page asks for its panel) and by that plugin's background enrichment source, so a page render only ever reads it.

    Args:
        location: The location whose parcel to look up; None is tolerated.

    Returns:
        The cached list, or None when nothing fresh is cached for this location."""
    from urbanlens.dashboard.models.cache.location_cache import UNANSWERED_SOURCES_KEY, LocationCache

    if location is None:
        return None
    cached = LocationCache.get_fresh(location, PARCEL_BUILDINGS_CACHE_SOURCE)
    if cached is None:
        return None
    data = cached.data or {}
    return CachedBuildings(buildings=list(data.get("buildings") or []), complete=not data.get(UNANSWERED_SOURCES_KEY))


def parcel_buildings(location: Location | None) -> list[dict] | None:
    """Every building known on this location's parcel, from cache only (see :func:`cached_parcel_buildings`).

    Args:
        location: The location whose parcel to look up; None is tolerated.

    Returns:
        The building records, ``[]`` when the providers were asked and found none, or None when nothing has ever been cached for this location."""
    cached = cached_parcel_buildings(location)
    return None if cached is None else cached.buildings


def site_buildings(location: Location) -> list[dict]:
    """Every building known on the parcel ``location`` stands on, from cache only.

    Args:
        location: A building's or a site's location.

    Returns:
        The location's own parcel list, else that of the first site it is nested under that has one, else ``[]``
        (also for an unsaved location, which has neither).
    """
    if location.pk is None:
        return []
    buildings = parcel_buildings(location)
    if buildings:
        return buildings
    for site in enclosing_site_locations(location):
        if buildings := parcel_buildings(site):
            return buildings
    return []


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
    chain = marker.ancestor_chain()
    return chain[-1] if chain else None


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


def reclassify_markers_on_place(place, *, owner=None) -> int:
    """Re-derive the cached scope of the wikis standing on a place, and of *owner*'s pins there.

    Another account's pins are never retyped from here: shared place data does not write to a private pin.
    Its owner's next visit re-derives it (:func:`rederive_pin_type`).

    Args:
        place: The place whose markers to re-derive.
        owner: The profile whose own action changed the place, if any.

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
    wikis = Wiki.objects.filter(place=place, pin_type_is_user_provided=False, pin_type__in=scoped).exclude(pin_type=implied)
    pins = Pin.objects.filter(profile=owner, location__place=place, pin_type_is_user_provided=False, pin_type__in=scoped).exclude(pin_type=implied) if owner is not None else Pin.objects.none()
    if implied == PinType.PARCEL:
        # The parcel's own buildings - see implied_pin_type.
        wikis = wikis.exclude(pin_type=PinType.BUILDING, parent_wiki__isnull=False)
        pins = pins.exclude(pin_type=PinType.BUILDING, parent_pin__isnull=False)
    return wikis.update(pin_type=implied) + pins.update(pin_type=implied)


def rederive_pin_type(pin: Pin) -> bool:
    """Store the type an owner's pin now reads as, which place changes since its last visit may have moved.

    A stored building is never demoted to a plain marker: it records a footprint under the pin, and its parent's
    building count reads it.

    Args:
        pin: The pin, being opened by its owner.

    Returns:
        True when the stored type changed."""
    from urbanlens.dashboard.models.pin.model import Pin, PinType
    from urbanlens.dashboard.services.places.scope import effective_pin_type

    scoped = {PinType.LOCATION_MARKER, PinType.PARCEL, PinType.BUILDING}
    if pin.pin_type_is_user_provided or pin.pin_type not in scoped:
        return False
    implied = effective_pin_type(pin)
    if implied == pin.pin_type or implied not in scoped or (pin.pin_type == PinType.BUILDING and implied == PinType.LOCATION_MARKER):
        return False
    Pin.objects.filter(pk=pin.pk).update(pin_type=implied)
    pin.pin_type = implied
    return True
