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

    def get_state_boundary(self, state_abbr: str) -> dict[str, Any] | None:
        """Return the raw Esri ring geometry of one US state's boundary.

        Unlike :meth:`get_geography`, this is an attribute query (matching on
        the state's USPS abbreviation), not a point-in-polygon lookup - it
        answers "what does this state's polygon look like", not "what state is
        this point in". Used by ``services.geo_boundary.state_boundary`` to
        build a point-containment gate for state-scoped plugins.

        Args:
            state_abbr: Two-letter USPS state abbreviation (e.g. ``"NY"``).

        Returns:
            The raw ``{"rings": [...]}`` Esri geometry dict, or None when the
            state isn't found or the request fails.

        Raises:
            ValueError: ``state_abbr`` isn't exactly two letters - guards the
                ``where`` clause below, which interpolates it directly.
        """
        if len(state_abbr) != 2 or not state_abbr.isalpha():
            raise ValueError(f"state_abbr must be a two-letter USPS abbreviation, got {state_abbr!r}")
        params: dict[str, str | int] = {
            "where": f"STUSPS='{state_abbr.upper()}'",
            "outFields": "STUSPS",
            "returnGeometry": "true",
            "outSR": 4326,
            "f": "json",
        }
        try:
            response = self.session.get(f"{self.base_url}/{_LAYER_STATE}/query", params=params, timeout=15)
            response.raise_for_status()
            body = response.json()
        except requests.exceptions.RequestException:
            logger.warning("TIGERweb state boundary query failed for %s", state_abbr, exc_info=True)
            return None
        features = body.get("features") or []
        if not features:
            return None
        return features[0].get("geometry")

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
        tribal_land = self._normalize(self._query_layer(_LAYER_FEDERAL_RESERVATION, latitude, longitude)) or self._normalize(self._query_layer(_LAYER_STATE_RESERVATION, latitude, longitude))
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
