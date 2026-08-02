"""Viewport-scoped rail and waterway features for the main map.

The overlay is built from OpenStreetMap data through UrbanLens' existing
Overpass gateway.  In addition to active railways and waterways, the query
includes OSM lifecycle tags used for rail trails, abandoned rights-of-way,
and disused or derelict canals.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from django.core.cache import cache

from urbanlens.dashboard.services.apis.locations.boundaries.overpass import OverpassGateway

_CACHE_SECONDS = 5 * 60
_CACHE_VERSION = 1
_MAX_FEATURES = 4_000
_MAX_BBOX_SPAN = 1.5
_MAX_BBOX_AREA = 1.0

_RAILWAY_VALUES = (
    "rail|light_rail|subway|tram|narrow_gauge|monorail|preserved|"
    "disused|abandoned|razed|dismantled|historic"
)
_WATERWAY_VALUES = "river|stream|canal|tidal_channel|derelict_canal"
_HISTORIC_RAIL_VALUES = {"abandoned", "disused", "dismantled", "razed", "historic"}


@dataclass(frozen=True, slots=True)
class InfrastructureBounds:
    """Validated WGS84 viewport bounds."""

    west: float
    south: float
    east: float
    north: float

    @property
    def overpass_bbox(self) -> str:
        """Return Overpass' south,west,north,east bbox order."""
        return f"{self.south:.5f},{self.west:.5f},{self.north:.5f},{self.east:.5f}"

    @property
    def cache_key(self) -> str:
        rounded = (self.west, self.south, self.east, self.north)
        coordinates = ":".join(f"{value:.3f}" for value in rounded)
        return f"map-infrastructure:v{_CACHE_VERSION}:{coordinates}"


def parse_infrastructure_bbox(raw_bbox: str | None) -> InfrastructureBounds:
    """Parse Leaflet's ``west,south,east,north`` bbox string.

    The layer intentionally serves local viewports only.  This keeps a user
    from accidentally asking Overpass for a country-sized geometry response.
    """
    try:
        values = [float(value) for value in (raw_bbox or "").split(",")]
    except ValueError as exc:
        raise ValueError("bbox must contain four numeric coordinates") from exc
    if len(values) != 4:
        raise ValueError("bbox must contain west,south,east,north")

    west, south, east, north = values
    if not (-180 <= west < east <= 180 and -90 <= south < north <= 90):
        raise ValueError("bbox coordinates are out of range or reversed")

    width = east - west
    height = north - south
    if width > _MAX_BBOX_SPAN or height > _MAX_BBOX_SPAN or width * height > _MAX_BBOX_AREA:
        raise ValueError("bbox is too large; zoom in before loading infrastructure")
    return InfrastructureBounds(west=west, south=south, east=east, north=north)


def build_infrastructure_query(bounds: InfrastructureBounds) -> str:
    """Build the bounded Overpass QL program used by the overlay."""
    bbox = bounds.overpass_bbox
    return f"""[out:json][timeout:25][maxsize:67108864];
(
  way["railway"~"^({_RAILWAY_VALUES})$"]({bbox});
  way["disused:railway"]({bbox});
  way["abandoned:railway"]({bbox});
  way["demolished:railway"]({bbox});
  way["railtrail"="yes"]({bbox});
  way["waterway"~"^({_WATERWAY_VALUES})$"]({bbox});
  way["disused:waterway"]({bbox});
  way["abandoned:waterway"]({bbox});
  way["demolished:waterway"]({bbox});
  way["historic"~"^(railway|canal)$"]({bbox});
);
out tags geom qt;"""


def _is_rail(tags: dict[str, Any]) -> bool:
    return bool(
        tags.get("railway")
        or tags.get("disused:railway")
        or tags.get("abandoned:railway")
        or tags.get("demolished:railway")
        or tags.get("railtrail") == "yes"
        or tags.get("historic") == "railway"
    )


def _is_historic(tags: dict[str, Any], kind: str) -> bool:
    if kind == "rail":
        return bool(
            tags.get("railway") in _HISTORIC_RAIL_VALUES
            or tags.get("disused:railway")
            or tags.get("abandoned:railway")
            or tags.get("demolished:railway")
            or tags.get("railtrail") == "yes"
            or tags.get("historic") == "railway"
            or tags.get("disused") == "yes"
            or tags.get("abandoned") == "yes"
        )
    return bool(
        tags.get("waterway") == "derelict_canal"
        or tags.get("historic") == "canal"
        or tags.get("disused:waterway")
        or tags.get("abandoned:waterway")
        or tags.get("demolished:waterway")
        or tags.get("disused") == "yes"
        or tags.get("abandoned") == "yes"
    )


def _feature_label(tags: dict[str, Any], kind: str, historic: bool) -> tuple[str, str]:
    name = str(tags.get("name") or tags.get("official_name") or "").strip()
    if kind == "rail":
        raw_type = str(
            tags.get("railway")
            or tags.get("disused:railway")
            or tags.get("abandoned:railway")
            or tags.get("demolished:railway")
            or ("rail trail" if tags.get("railtrail") == "yes" else "railway")
        )
        type_label = raw_type.replace("_", " ").title()
        fallback = f"{'Historic ' if historic else ''}{type_label}"
    else:
        raw_type = str(
            tags.get("waterway")
            or tags.get("disused:waterway")
            or tags.get("abandoned:waterway")
            or tags.get("demolished:waterway")
            or "waterway"
        )
        type_label = raw_type.replace("_", " ").title()
        fallback = f"{'Historic ' if historic else ''}{type_label}"
    return name or fallback, type_label


def _element_to_feature(element: dict[str, Any]) -> dict[str, Any] | None:
    tags = element.get("tags")
    geometry = element.get("geometry")
    if not isinstance(tags, dict) or not isinstance(geometry, list):
        return None

    coordinates: list[list[float]] = []
    for point in geometry:
        if not isinstance(point, dict):
            continue
        lat = point.get("lat")
        lon = point.get("lon")
        if isinstance(lat, (int, float)) and isinstance(lon, (int, float)):
            coordinates.append([float(lon), float(lat)])
    if len(coordinates) < 2:
        return None

    kind = "rail" if _is_rail(tags) else "water"
    historic = _is_historic(tags, kind)
    name, type_label = _feature_label(tags, kind, historic)
    osm_id = element.get("id")
    osm_url = f"https://www.openstreetmap.org/way/{osm_id}" if isinstance(osm_id, int) else ""
    return {
        "type": "Feature",
        "id": f"osm-way-{osm_id}",
        "geometry": {"type": "LineString", "coordinates": coordinates},
        "properties": {
            "name": name,
            "kind": kind,
            "type": type_label,
            "historic": historic,
            "status": "Historic / inactive" if historic else "Active",
            "osm_url": osm_url,
        },
    }


def infrastructure_feature_collection(bounds: InfrastructureBounds) -> dict[str, Any]:
    """Return a cached GeoJSON FeatureCollection for ``bounds``."""
    cached = cache.get(bounds.cache_key)
    if isinstance(cached, dict):
        return cached

    elements = OverpassGateway().elements_for_query(build_infrastructure_query(bounds), timeout=30)
    features: list[dict[str, Any]] = []
    for element in elements:
        feature = _element_to_feature(element)
        if feature is not None:
            features.append(feature)
        if len(features) >= _MAX_FEATURES:
            break

    collection = {
        "type": "FeatureCollection",
        "features": features,
        "attribution": "© OpenStreetMap contributors",
        "truncated": len(features) >= _MAX_FEATURES,
    }
    cache.set(bounds.cache_key, collection, _CACHE_SECONDS)
    return collection
