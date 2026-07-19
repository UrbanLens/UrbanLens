"""US Census Bureau TIGERweb gateway - free, keyless geography lookups by coordinate.

https://tigerweb.geo.census.gov/ - an ArcGIS REST MapServer over the same
TIGER/Line geographies used elsewhere in the government open-data space, with
no API key and no rate-limit registration required. US coverage only.
"""

from __future__ import annotations

from dataclasses import dataclass
import logging
from typing import Any, ClassVar

import requests

from urbanlens.dashboard.services.gateway import Gateway
from urbanlens.dashboard.services.redact import redact_coordinate

logger = logging.getLogger(__name__)

_BASE_URL = "https://tigerweb.geo.census.gov/arcgis/rest/services/TIGERweb/tigerWMS_Current/MapServer"

# TIGERweb MapServer layer ids for the geographies this gateway surfaces -
# see the MapServer's own `?f=json` layer listing for the full catalog.
_LAYER_STATE = 80
_LAYER_COUNTY = 82
_LAYER_PLACE = 28
_LAYER_TRACT = 8
_LAYER_ZCTA = 2
_LAYER_URBAN_AREA = 88
_LAYER_CBSA = 93
_LAYER_FEDERAL_RESERVATION = 36
_LAYER_STATE_RESERVATION = 40


@dataclass(slots=True, kw_only=True)
class CensusTigerwebGateway(Gateway):
    """Gateway for the US Census Bureau's TIGERweb ArcGIS REST service.

    Free, keyless point-in-polygon lookups for US Census geography (state,
    county, incorporated place, census tract) covering any US coordinate.
    """

    service_key: ClassVar[str] = "census_tigerweb"
    paid_service: ClassVar[bool] = False

    base_url: str = _BASE_URL

    def _query_layer(self, layer_id: int, latitude: float, longitude: float) -> dict[str, Any] | None:
        """Return the first feature's attributes intersecting a point, for one layer."""
        params: dict[str, str | int] = {
            "geometry": f"{longitude},{latitude}",
            "geometryType": "esriGeometryPoint",
            "inSR": 4326,
            "spatialRel": "esriSpatialRelIntersects",
            "outFields": "*",
            "returnGeometry": "false",
            "f": "json",
        }
        try:
            response = self.session.get(f"{self.base_url}/{layer_id}/query", params=params, timeout=15)
            response.raise_for_status()
            body = response.json()
        except requests.exceptions.RequestException:
            logger.warning("TIGERweb layer %s query failed for %s, %s", layer_id, redact_coordinate(latitude), redact_coordinate(longitude), exc_info=True)
            return None
        features = body.get("features") or []
        return features[0].get("attributes") if features else None

    def get_geography(self, latitude: float, longitude: float) -> dict[str, Any]:
        """Return the US Census geography containing a coordinate.

        Args:
            latitude: WGS-84 latitude.
            longitude: WGS-84 longitude.

        Returns:
            Dict with ``state``, ``county``, ``place``, ``tract``, ``zcta``,
            ``urban_area``, ``cbsa``, ``tribal_land`` sub-dicts (each
            ``{"name": ..., "geoid": ...}``, or None when the point isn't in
            that geography type, e.g. an unincorporated area with no
            enclosing place, or a rural point outside any urban area/reservation);
            an empty dict outside the US entirely.
        """
        state = self._normalize(self._query_layer(_LAYER_STATE, latitude, longitude))
        if not state:
            return {}
        tribal_land = self._normalize(self._query_layer(_LAYER_FEDERAL_RESERVATION, latitude, longitude)) or self._normalize(
            self._query_layer(_LAYER_STATE_RESERVATION, latitude, longitude)
        )
        return {
            "state": state,
            "county": self._normalize(self._query_layer(_LAYER_COUNTY, latitude, longitude)),
            "place": self._normalize(self._query_layer(_LAYER_PLACE, latitude, longitude)),
            "tract": self._normalize(self._query_layer(_LAYER_TRACT, latitude, longitude)),
            "zcta": self._normalize(self._query_layer(_LAYER_ZCTA, latitude, longitude)),
            "urban_area": self._normalize(self._query_layer(_LAYER_URBAN_AREA, latitude, longitude)),
            "cbsa": self._normalize(self._query_layer(_LAYER_CBSA, latitude, longitude)),
            "tribal_land": tribal_land,
        }

    @staticmethod
    def _normalize(attributes: dict[str, Any] | None) -> dict[str, Any] | None:
        """Reduce a raw TIGERweb attribute row to its display name and GEOID."""
        if not attributes:
            return None
        return {"name": attributes.get("NAME"), "geoid": attributes.get("GEOID")}

    def get_county_extent(self, fips: str, out_wkid: int = 4326) -> tuple[float, float, float, float] | None:
        """Return a county's bounding-box extent by its 5-digit GEOID, reprojected to ``out_wkid``.

        Used by property-record discovery as a jurisdiction-identity ground
        truth that doesn't depend on a candidate's own (sometimes
        uninformative or actively misleading, e.g. an unrelated county's
        service literally named for its own county) title text - see
        ``property_records.discovery``'s extent-overlap check.

        ``out_wkid`` should normally be the *candidate* layer's own spatial
        reference (from its ``extent.spatialReference``), not a fixed
        WGS-84/Web Mercator assumption: real ArcGIS Online-hosted layers turn
        up in all sorts of projections (a live one used NAD83 Missouri state
        plane, wkid 26854) that this module has no business trying to invert
        itself. Letting the ArcGIS server on the other end do the
        reprojection - which every standard ``/query`` endpoint supports
        natively for any registered wkid - means both boxes end up in the
        exact same coordinate system, and a bare rectangle-overlap test
        (:func:`~.relevance.extent_overlaps_county`) is valid regardless of
        what that system's units actually are.

        Args:
            fips: 5-digit Census county GEOID (2-digit state + 3-digit county).
            out_wkid: The spatial-reference WKID to reproject the extent into.

        Returns:
            ``(xmin, ymin, xmax, ymax)`` in ``out_wkid``'s units, or None when
            the GEOID doesn't resolve to a county, ``out_wkid`` isn't a
            reference TIGERweb recognizes, or the request fails.
        """
        params: dict[str, str | int] = {
            "where": f"GEOID='{fips}'",
            "returnExtentOnly": "true",
            "outSR": out_wkid,
            "f": "json",
        }
        try:
            response = self.session.get(f"{self.base_url}/{_LAYER_COUNTY}/query", params=params, timeout=15)
            response.raise_for_status()
            body = response.json()
        except (requests.exceptions.RequestException, ValueError):
            logger.warning("TIGERweb county-extent query failed for FIPS %s", fips, exc_info=True)
            return None
        extent = body.get("extent")
        if not isinstance(extent, dict):
            return None
        try:
            return float(extent["xmin"]), float(extent["ymin"]), float(extent["xmax"]), float(extent["ymax"])
        except (KeyError, TypeError, ValueError):
            return None
