"""Historic-register listing names, as place-name candidates of the highest tier.

Only a listing whose own boundary contains the point is a candidate. Registers list neighbours too: at
the Hudson River State Hospital campus REData's National Register answer can hold the Isaac Roosevelt
House, a point half a kilometre away, beside the campus's own listing.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from urbanlens.dashboard.services.locations.name_resolution import NameProvider

if TYPE_CHECKING:
    from urbanlens.dashboard.models.location.model import Location

#: The key a cached register row or CRIS site record carries once its boundary was tested against the point.
CONTAINS_POINT_KEY = "contains_point"

#: CRIS site-record fields that hold its name, in preference order.
_CRIS_SITE_NAME_KEYS = ("HistoricName", "DistrictName", "USNName", "Name")

#: Register rows that name no property.
_NOT_A_PROPERTY = frozenset({"archaeological_buffer_area"})


def geometry_contains_point(geometry: Any, latitude: float, longitude: float) -> bool:
    """Whether a GeoJSON polygon contains a coordinate. A point or missing geometry never does.

    Args:
        geometry: A GeoJSON geometry dict, or anything else.
        latitude: WGS-84 latitude.
        longitude: WGS-84 longitude.

    Returns:
        True only for an areal geometry containing the point.
    """
    from django.contrib.gis.geos import Point

    from urbanlens.dashboard.services.apis.locations.base import geojson_polygon_to_geos

    if not isinstance(geometry, dict) or geometry.get("type") not in ("Polygon", "MultiPolygon"):
        return False
    polygon = geojson_polygon_to_geos(geometry)
    return polygon is not None and bool(polygon.contains(Point(float(longitude), float(latitude), srid=4326)))


def register_listing_names(location: Location) -> list[str]:
    """Names of the register listings whose boundary contains this location, site-level first.

    Args:
        location: The location being named.

    Returns:
        Listing names from REData's registers, then CRIS's National Register listing or district.
    """
    from urbanlens.dashboard.models.cache.location_cache import LocationCache

    names: list[str] = []
    cached = LocationCache.get_fresh(location, "redata_historic_registers")
    rows = (cached.data or {}).get("resources") if cached is not None and isinstance(cached.data, dict) else None
    listed = [row for row in rows or [] if isinstance(row, dict) and row.get(CONTAINS_POINT_KEY) is True and str(row.get("resource_type") or "") not in _NOT_A_PROPERTY and str(row.get("name") or "").strip()]
    names.extend(str(row["name"]).strip() for row in sorted(listed, key=lambda row: row.get("scope") != "site"))

    if name := cris_register_listing(location):
        names.append(name)
    return names


def cris_register_listing(location: Location) -> str | None:
    """The name of CRIS's National Register listing or district for this location, when its boundary contains it.

    Args:
        location: The location being named.

    Returns:
        The listing's name, or None.
    """
    from urbanlens.dashboard.models.cache.location_cache import LocationCache

    cris = LocationCache.get_fresh(location, "cris_building_usn")
    district = (cris.data or {}).get("district") if cris is not None and isinstance(cris.data, dict) else None
    if not isinstance(district, dict) or district.get(CONTAINS_POINT_KEY) is not True:
        return None
    return next((str(district[key]).strip() for key in _CRIS_SITE_NAME_KEYS if str(district.get(key) or "").strip()), None)


class HistoricRegisterNameProvider(NameProvider):
    """Contributes the names of the register listings containing a location."""

    def __init__(self) -> None:
        """Initialize the provider under the ``historic_register`` source slug."""
        super().__init__(source="historic_register", verbose_name="Historic registers")

    def candidates(self, location: Location) -> list[str | None]:
        """The containing listings' names - see :func:`register_listing_names`."""
        return list(register_listing_names(location))
