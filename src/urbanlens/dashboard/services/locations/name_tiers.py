"""The ranked metric that chooses a place's automatic name, and which candidates may name or alias it.

Every rule the naming pipeline applies to *which kind* of name wins lives here, so it can be read and
tested in one place. The decision and its reasoning: ``docs/adr/0020-place-name-tiers.md``.
"""

from __future__ import annotations

from enum import IntEnum, StrEnum
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from urbanlens.dashboard.models.location.model import Location
    from urbanlens.dashboard.models.place.model import Place
    from urbanlens.dashboard.models.wiki.model import Wiki


class NameTier(IntEnum):
    """How specifically a name identifies a property. Lower wins."""

    #: The Wikipedia article matched to the place. Above a listing, which can name one building on a larger plot.
    ENCYCLOPEDIA = 0
    #: A historic-register listing (NRHP, a state register) whose boundary contains the point.
    HISTORIC_REGISTER = 1
    #: A named site, campus, landuse or park polygon the point is in.
    SITE = 2
    #: One building's name.
    BUILDING = 3
    #: A point of interest near the point.
    POI = 4
    #: A road or street address.
    ROAD = 5


class NamingScope(StrEnum):
    """Whether a location's names describe a property or one building on it."""

    PARCEL = "parcel"
    BUILDING = "building"


#: Tier of each name source whose candidates are always the same kind of name.
SOURCE_TIERS: dict[str, NameTier] = {
    "historic_register": NameTier.HISTORIC_REGISTER,
    "wikipedia": NameTier.ENCYCLOPEDIA,
    "nps": NameTier.SITE,
    "cris": NameTier.BUILDING,
    "redata_building": NameTier.BUILDING,
    "google_places": NameTier.POI,
    "azure_maps": NameTier.POI,
    "epa_echo": NameTier.POI,
}

#: Sources whose tier depends on what the OpenStreetMap object under the point is.
NOMINATIM_SOURCES: frozenset[str] = frozenset({"nominatim", "nominatim_old_name"})

#: Tier of a source this module does not know.
DEFAULT_TIER = NameTier.POI

#: OSM ``highway`` values, which Nominatim reports as ``type`` without the class.
_ROAD_TYPES: frozenset[str] = frozenset(
    {
        "motorway",
        "trunk",
        "primary",
        "secondary",
        "tertiary",
        "unclassified",
        "residential",
        "service",
        "living_street",
        "pedestrian",
        "track",
        "road",
        "footway",
        "path",
        "cycleway",
        "bridleway",
        "steps",
        "motorway_link",
        "trunk_link",
        "primary_link",
        "secondary_link",
        "tertiary_link",
        "busway",
    },
)
#: Nominatim ``type`` values that name an address rather than a thing.
_ADDRESS_TYPES: frozenset[str] = frozenset({"house", "street", "road", "postcode", "house_number"})
#: OSM classes whose areal objects are sites.
_SITE_CLASSES: frozenset[str] = frozenset({"amenity", "landuse", "leisure", "historic", "tourism", "healthcare", "military", "man_made", "boundary"})


def nominatim_tier(data: dict[str, Any]) -> NameTier:
    """The tier of the name Nominatim's reverse geocode gave, from the OSM object it named.

    Nominatim answers with the smallest object under the point, so a courtyard's answer is often the
    service road through it.

    Args:
        data: The cached ``nominatim`` payload.

    Returns:
        ROAD for a way of the highway family or an address, BUILDING for a building, SITE for an areal
        amenity/landuse/leisure/historic/tourism object, else POI.
    """
    osm_class = str(data.get("category") or data.get("class") or "").lower()
    osm_type = str(data.get("type") or "").lower()
    element = str(data.get("osm_url") or "").rstrip("/").rsplit("/", 2)[-2:-1]
    areal = bool(element) and element[0] in ("way", "relation")
    if osm_class in ("highway", "place") or (not osm_class and osm_type in _ROAD_TYPES) or osm_type in _ADDRESS_TYPES:
        return NameTier.ROAD
    if osm_class == "building" or (data.get("building") and not any(data.get(key) for key in ("amenity", "tourism", "historic"))):
        return NameTier.BUILDING
    if areal and (osm_class in _SITE_CLASSES or any(data.get(key) for key in ("amenity", "tourism", "historic"))):
        return NameTier.SITE
    return NameTier.POI


def tier_for(source: str, location: Location | None) -> NameTier:
    """The tier of a name from ``source`` at this location.

    Args:
        source: The name provider's source slug.
        location: The location, for sources whose tier depends on its cached data.

    Returns:
        The tier.
    """
    if source in NOMINATIM_SOURCES:
        from urbanlens.dashboard.models.cache.location_cache import LocationCache

        cached = LocationCache.get_fresh(location, "nominatim") if location is not None and location.pk else None
        data = cached.data if cached is not None else None
        return nominatim_tier(data) if isinstance(data, dict) else DEFAULT_TIER
    return SOURCE_TIERS.get(source, DEFAULT_TIER)


def rank_key(tier: NameTier, scope: NamingScope) -> tuple[int, int]:
    """Sort key for a tier: lower sorts first. A building's own page ranks its building name first.

    Args:
        tier: The candidate's tier.
        scope: The scope of the location being named.

    Returns:
        The sort key.
    """
    if scope == NamingScope.BUILDING:
        return (0 if tier == NameTier.BUILDING else 1, int(tier))
    return (0, int(tier))


def naming_scope(location: Location | None) -> NamingScope:
    """Whether this location's names describe a building rather than a property.

    A location whose own wiki describes a building (:func:`wiki_scope`) is a building's, whatever pins it holds.
    Otherwise one holding only child (building) pins, or a child wiki's own location, is a building's.

    Args:
        location: The location being named.

    Returns:
        The scope.
    """
    if location is None or not location.pk:
        return NamingScope.PARCEL
    from urbanlens.dashboard.models.wiki.model import Wiki

    own = Wiki.objects.filter(location=location).select_related("place__parent").first()
    if own is not None and wiki_scope(own) == NamingScope.BUILDING:
        return NamingScope.BUILDING
    pins = location.pins.all()
    if pins.root_pins().exists():
        return NamingScope.PARCEL
    if pins.exists() or (own is not None and own.parent_wiki_id is not None):
        return NamingScope.BUILDING
    return NamingScope.PARCEL


def wiki_scope(wiki: Wiki) -> NamingScope:
    """Whether a wiki describes one building of several, or something on one, rather than a property.

    A wiki nested under another describes part of it when it holds no place of its own, or holds one of a
    multi-building parcel's buildings (``scope.pin_type_for_place``). One holding a property's place - a parcel under
    its site, the one building of an ordinary property - describes a property. Read from the wiki and its place alone.

    Args:
        wiki: The wiki.

    Returns:
        The scope.
    """
    from urbanlens.dashboard.models.pin.model import PinType
    from urbanlens.dashboard.services.places.scope import pin_type_for_place

    if wiki.parent_wiki_id is None:
        return NamingScope.PARCEL
    place = wiki.place if wiki.place_id else None
    if place is None or pin_type_for_place(place) == PinType.BUILDING:
        return NamingScope.BUILDING
    return NamingScope.PARCEL


def _parcel(location: Location) -> Place | None:
    place = location.place if location.place_id else None
    return place.parcel if place is not None else None


def describes_scope(tier: NameTier, scope: NamingScope) -> bool:
    """Whether a name of this tier can name a location of this scope.

    An article found at a building's point is the campus's or a neighbour's: the match confirms it by the town its
    text mentions, or the Wikipedia panel takes the campus's article for a child pin.

    Args:
        tier: The candidate's tier.
        scope: The scope of the location being named.

    Returns:
        False for an encyclopedia article on a building's own page.
    """
    return scope != NamingScope.BUILDING or tier != NameTier.ENCYCLOPEDIA


def building_name_admissible(source: str, location: Location, scope: NamingScope | None = None) -> bool:
    """Whether a building's name may name, or alias, this location.

    On a building's own page, when the name is the building's own: a CRIS record must stand on it. On a property's,
    only when the property is known to hold exactly one building and the named building is on it: without a parcel
    a building found within a search radius says nothing about this property, and on a campus one building's name
    is not the campus's.

    Args:
        source: The building-tier source slug.
        location: The location being named.
        scope: :func:`naming_scope` for the location, when the caller already has it.

    Returns:
        True when the name may be used.
    """
    if (scope or naming_scope(location)) == NamingScope.BUILDING:
        return source != "cris" or _cris_record_stands_on(location)
    parcel = _parcel(location)
    if parcel is None or parcel.building_child_count != 1:
        return False
    return _building_on_parcel(source, location, parcel)


def _cris_record_stands_on(location: Location) -> bool:
    """Whether the location's cached CRIS building record is the building's own, not a neighbour's."""
    from urbanlens.dashboard.models.cache.location_cache import LocationCache
    from urbanlens.dashboard.services.locations.national_register import stands_on

    cached = LocationCache.get_fresh(location, "cris_building_usn")
    data = cached.data if cached is not None and isinstance(cached.data, dict) else {}
    return stands_on(location, data.get("source_latitude"), data.get("source_longitude"))


def _building_on_parcel(source: str, location: Location, parcel: Place) -> bool:
    """Whether the building ``source`` named is on ``parcel``; REData only names buildings on the parcel."""
    if source != "cris":
        return True
    from django.contrib.gis.geos import Point

    from urbanlens.dashboard.models.cache.location_cache import LocationCache

    cached = LocationCache.get_fresh(location, "cris_building_usn")
    data = cached.data if cached is not None and isinstance(cached.data, dict) else {}
    latitude, longitude = data.get("source_latitude"), data.get("source_longitude")
    if latitude is None or longitude is None or parcel.geometry is None:
        return False
    return bool(parcel.geometry.contains(Point(float(longitude), float(latitude), srid=4326)))


def aliasable(tier: NameTier) -> bool:
    """Whether a name of this tier is kept as an alias. A road passing through a place is not another name for it.

    Args:
        tier: The candidate's tier.

    Returns:
        False for road and address names.
    """
    return tier != NameTier.ROAD
