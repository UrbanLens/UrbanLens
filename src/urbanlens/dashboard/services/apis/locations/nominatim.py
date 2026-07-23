"""OpenStreetMap Nominatim service - free reverse geocoding with place metadata."""

from __future__ import annotations

from dataclasses import dataclass
import logging
import re
from typing import Any, ClassVar

from urbanlens.dashboard.services.gateway import Gateway
from urbanlens.dashboard.services.redact import redact_coordinate

logger = logging.getLogger(__name__)

_API_URL = "https://nominatim.openstreetmap.org"
_USER_AGENT = "UrbanLens/1.0 (https://github.com/urbanlens/urbanlens; hello@urbanlens.org) python-requests/2.x"

# Nominatim's UI convention prefixes a fallback name with its OSM element type
# (e.g. "Way: College Hill Golf Course"). Strip it defensively wherever a name
# is read, since the raw value is also used as a pin-naming candidate.
_OSM_TYPE_PREFIX_PATTERN = re.compile(r"^(node|way|relation)\s*:\s*", re.IGNORECASE)

# Curated extratags worth surfacing beyond the headline fields (website, phone,
# hours, operator) already broken out individually. Ordered by roughly how
# often they're useful across the amenity/tourism/historic/shop types this
# panel sees. Value is the human label; boolean-ish values are humanized
# separately by ``_humanize_osm_value``.
_EXTRA_DETAIL_FIELDS: tuple[tuple[str, str], ...] = (
    ("cuisine", "Cuisine"),
    ("religion", "Religion"),
    ("denomination", "Denomination"),
    ("sport", "Sport"),
    ("brand", "Brand"),
    ("network", "Network"),
    ("internet_access", "Internet Access"),
    ("wheelchair", "Wheelchair Access"),
    ("fee", "Fee"),
    ("smoking", "Smoking"),
    ("outdoor_seating", "Outdoor Seating"),
    ("delivery", "Delivery"),
    ("takeaway", "Takeaway"),
    ("dog", "Dogs Allowed"),
    ("air_conditioning", "Air Conditioning"),
    ("capacity", "Capacity"),
    ("level", "Level"),
    ("surface", "Surface"),
    ("access", "Access"),
    ("designation", "Designation"),
    ("heritage", "Heritage Status"),
    ("start_date", "Established"),
    ("population", "Population"),
    ("ele", "Elevation (m)"),
    ("email", "Email"),
    ("operator:type", "Operator Type"),
    ("gnis:feature_id", "GNIS Feature ID"),
)

_BOOLISH_VALUE_LABELS: dict[str, str] = {
    "yes": "Yes",
    "no": "No",
    "limited": "Limited",
    "designated": "Designated",
    "permissive": "Permissive",
    "private": "Private",
    "public": "Public",
    "customers": "Customers only",
    "only": "Only",
}


def _humanize_osm_value(value: str) -> str:
    """Turn a raw OSM tag value into a display-friendly string.

    Args:
        value: Raw tag value, e.g. ``"limited"`` or ``"fine_dining;regional"``.

    Returns:
        A humanized value: known boolean-ish tokens are mapped to friendly
        labels, and remaining underscore/semicolon-separated values are
        turned into a comma-separated, space-joined string.
    """
    cleaned = value.strip()
    mapped = _BOOLISH_VALUE_LABELS.get(cleaned.lower())
    if mapped:
        return mapped
    return ", ".join(part.strip().replace("_", " ") for part in cleaned.split(";") if part.strip())


def _humanize_osm_key(value: str) -> str:
    """Turn a raw OSM tag/type value (e.g. ``golf_course``) into a title-cased label."""
    return value.replace("_", " ").replace("-", " ").strip().title()


@dataclass(slots=True, kw_only=True)
class NominatimGateway(Gateway):
    """
    Reverse-geocodes coordinates via the Nominatim API and returns rich place metadata.

    Nominatim is free and requires no API key, but enforces a 1-request/second
    rate limit.  The LocationCache layer ensures we only query once per 7 days,
    so this is not a concern in practice.
    """

    service_key: ClassVar[str] = "nominatim"
    paid_service: ClassVar[bool] = False

    base_url: str = _API_URL

    def __post_init__(self) -> None:
        Gateway.__post_init__(self)
        self.session.headers.update({"User-Agent": _USER_AGENT})

    def search(self, query: str, *, limit: int = 5, **params: Any) -> list[dict[str, Any]]:
        """Search OpenStreetMap places by free-text query through Nominatim."""
        request_params: dict[str, Any] = {
            "q": query,
            "format": "json",
            "limit": max(1, min(int(limit), 50)),
            "extratags": 1,
            "namedetails": 1,
            "addressdetails": 1,
            **params,
        }
        try:
            resp = self.session.get(f"{self.base_url}/search", params=request_params, timeout=10)
            resp.raise_for_status()
            raw = resp.json()
        except Exception:
            logger.exception("Nominatim search failed for %r", query)
            return []
        if not isinstance(raw, list):
            return []
        return [self._normalise(item) for item in raw if isinstance(item, dict)]

    def lookup(self, osm_ids: list[str], **params: Any) -> list[dict[str, Any]]:
        """Lookup OSM objects by ids like ``N123``, ``W456``, or ``R789``."""
        if not osm_ids:
            return []
        request_params: dict[str, Any] = {
            "osm_ids": ",".join(osm_ids[:50]),
            "format": "json",
            "extratags": 1,
            "namedetails": 1,
            "addressdetails": 1,
            **params,
        }
        try:
            resp = self.session.get(f"{self.base_url}/lookup", params=request_params, timeout=10)
            resp.raise_for_status()
            raw = resp.json()
        except Exception:
            logger.exception("Nominatim lookup failed for %s", osm_ids)
            return []
        if not isinstance(raw, list):
            return []
        return [self._normalise(item) for item in raw if isinstance(item, dict)]

    def reverse_geocode(self, latitude: float, longitude: float) -> dict[str, Any] | None:
        """
        Reverse-geocode coordinates and return structured place metadata.

        Args:
            latitude: WGS-84 latitude.
            longitude: WGS-84 longitude.

        Returns:
            Dict with place metadata, or None if no result or an error occurred.
        """
        try:
            params: dict[str, str | int | float] = {
                "lat": latitude,
                "lon": longitude,
                "format": "json",
                "extratags": 1,
                "namedetails": 1,
                "addressdetails": 1,
            }
            resp = self.session.get(
                f"{self.base_url}/reverse",
                params=params,
                timeout=10,
            )
            resp.raise_for_status()
            raw = resp.json()
        except Exception:
            logger.exception("Nominatim reverse geocode failed for %s,%s", redact_coordinate(latitude), redact_coordinate(longitude))
            return None

        if "error" in raw:
            return None

        return self._normalise(raw)

    @staticmethod
    def _normalise(raw: dict) -> dict[str, Any]:
        """Extract useful fields from the raw Nominatim response."""
        extra = raw.get("extratags") or {}
        address = raw.get("address") or {}
        osm_type = raw.get("osm_type", "")
        osm_id = raw.get("osm_id", "")

        osm_url = f"https://www.openstreetmap.org/{osm_type}/{osm_id}" if osm_type and osm_id else ""

        name = (raw.get("namedetails") or {}).get("name") or raw.get("name") or address.get("amenity") or address.get("building") or ""
        name = _OSM_TYPE_PREFIX_PATTERN.sub("", name).strip()

        building = extra.get("building") or address.get("building") or ""
        amenity = address.get("amenity") or ""
        tourism = extra.get("tourism") or address.get("tourism") or ""
        historic = extra.get("historic") or address.get("historic") or ""

        # A single human label for the place's kind, preferring the specific
        # OSM primary tag (e.g. "golf_course") over the broader class/category
        # ("leisure") and the coarser amenity/tourism/historic/building
        # fallbacks already parsed above.
        raw_type = raw.get("type") or ""
        kind_source = raw_type if raw_type and raw_type != "yes" else amenity or tourism or historic or building or raw.get("category") or raw.get("class") or ""
        kind_label = _humanize_osm_key(kind_source) if kind_source else ""

        wikidata = extra.get("wikidata") or ""
        image = extra.get("image") or extra.get("wikimedia_commons") or ""
        if image and image.startswith("File:"):
            # A bare Commons filename, not a URL - link to the description page
            # rather than trying to hotlink an unresolved image.
            image = f"https://commons.wikimedia.org/wiki/{image}"

        # Address-breakdown facts (from addressdetails=1, not extratags) - these
        # exist for nearly every reverse-geocode result, even a bare point with
        # no OSM tags of its own beyond geometry, so surfacing them keeps the
        # panel from looking sparse/empty for the common "no extra tags" case.
        address_details = [
            {"key": key, "label": label, "value": address[key]}
            for key, label in (("neighbourhood", "Neighbourhood"), ("suburb", "Suburb"), ("county", "County"), ("postcode", "Postcode"))
            if address.get(key) and not (key == "suburb" and address.get("neighbourhood") == address.get("suburb"))
        ]

        extra_details = address_details + [{"key": key, "label": label, "value": _humanize_osm_value(str(extra[key]))} for key, label in _EXTRA_DETAIL_FIELDS if extra.get(key)]

        return {
            "name": name,
            "display_name": raw.get("display_name", ""),
            "osm_url": osm_url,
            "kind_label": kind_label,
            "website": extra.get("website") or extra.get("url") or "",
            "phone": extra.get("phone") or extra.get("contact:phone") or "",
            "email": extra.get("email") or extra.get("contact:email") or "",
            "opening_hours": extra.get("opening_hours") or "",
            "operator": extra.get("operator") or "",
            # Semicolon-separated former names (e.g. "Old Name A;Old Name B") -
            # see plugins.builtin.nominatim for how this is split into
            # separate alias candidates rather than kept as one garbled string.
            "old_name": extra.get("old_name") or "",
            "building": building,
            "amenity": amenity,
            "tourism": tourism,
            "historic": historic,
            "wikipedia": extra.get("wikipedia") or "",
            "wikidata": wikidata,
            "image": image,
            "extra_details": extra_details,
            "category": raw.get("category", ""),
            "type": raw.get("type", ""),
            "importance": raw.get("importance"),
            "lat": raw.get("lat"),
            "lon": raw.get("lon"),
            "boundingbox": raw.get("boundingbox") or [],
            # Only present when the request passed polygon_geojson=1 (see
            # RegionBoundarySearchView) - a GeoJSON Polygon/MultiPolygon for
            # area-like results, or absent/None otherwise.
            "geojson": raw.get("geojson"),
        }
