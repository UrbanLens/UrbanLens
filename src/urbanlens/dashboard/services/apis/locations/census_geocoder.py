"""US Census Bureau Geocoder gateway - free, keyless address geocoding with FIPS geography.

https://geocoding.geo.census.gov/geocoder/ - unlike ``census_tigerweb.py``
(point-in-polygon lookups for coordinates already in hand), this resolves a
free-text US street address straight to coordinates *and* the Census
geography (state/county FIPS) containing it in one call - the "geocode the
address" step of the property-records jurisdiction-resolution pipeline (see
``docs/property-records-plan.md`` section 1, and
``services.apis.property_records.jurisdiction.resolve_jurisdiction``). No API
key, no rate-limit registration, US coverage only. Kept as its own reusable
gateway (not folded into the property-records package) since any other
service needing "free-text US address -> coordinates + FIPS" can use it too.
"""

from __future__ import annotations

from dataclasses import dataclass
import logging
from typing import Any, ClassVar

import requests

from urbanlens.dashboard.services.gateway import Gateway

logger = logging.getLogger(__name__)

_BASE_URL = "https://geocoding.geo.census.gov/geocoder"
_BENCHMARK = "Public_AR_Current"
_VINTAGE = "Current_Current"


@dataclass(frozen=True, slots=True)
class CensusGeocodeResult:
    """One geocoded address match, with its containing Census geography.

    Attributes:
        matched_address: The Census Bureau's normalized form of the input address.
        latitude: WGS-84 latitude of the matched address point.
        longitude: WGS-84 longitude of the matched address point.
        state_fips: 2-digit state FIPS code.
        county_fips: 3-digit county FIPS code.
        county_name: County display name (e.g. ``"Albany County"``).
        state_name: State display name.
    """

    matched_address: str
    latitude: float
    longitude: float
    state_fips: str
    county_fips: str
    county_name: str
    state_name: str

    @property
    def fips(self) -> str:
        """5-digit combined state+county FIPS code."""
        return f"{self.state_fips}{self.county_fips}"


@dataclass(slots=True, kw_only=True)
class CensusGeocoderGateway(Gateway):
    """Gateway for the US Census Bureau's free geocoding + geography REST API."""

    service_key: ClassVar[str] = "census_geocoder"
    paid_service: ClassVar[bool] = False

    base_url: str = _BASE_URL

    def geocode_address(self, address: str) -> CensusGeocodeResult | None:
        """Geocode a free-text US address to coordinates and its containing county FIPS.

        Args:
            address: A one-line US street address (e.g. ``"123 Main St, Albany, NY"``).

        Returns:
            The best match, or None when the address didn't match anything, the
            match had no county geography attached, or the request failed.
        """
        params = {
            "address": address,
            "benchmark": _BENCHMARK,
            "vintage": _VINTAGE,
            "format": "json",
        }
        try:
            response = self.session.get(f"{self.base_url}/geographies/onelineaddress", params=params, timeout=15)
            response.raise_for_status()
            body = response.json()
        except requests.exceptions.RequestException:
            logger.warning("Census geocoder address lookup failed", exc_info=True)
            return None

        matches = (body.get("result") or {}).get("addressMatches") or []
        if not matches:
            return None
        return self._parse_match(matches[0])

    def geography_for_coordinates(self, latitude: float, longitude: float) -> CensusGeocodeResult | None:
        """Return the Census geography (state/county FIPS) containing a coordinate.

        Args:
            latitude: WGS-84 latitude.
            longitude: WGS-84 longitude.

        Returns:
            A result with ``matched_address`` empty (no address is being
            matched), or None outside US coverage / on request failure.
        """
        params: dict[str, str | float] = {
            "x": longitude,
            "y": latitude,
            "benchmark": _BENCHMARK,
            "vintage": _VINTAGE,
            "format": "json",
        }
        try:
            response = self.session.get(f"{self.base_url}/geographies/coordinates", params=params, timeout=15)
            response.raise_for_status()
            body = response.json()
        except requests.exceptions.RequestException:
            logger.warning("Census geocoder coordinate lookup failed", exc_info=True)
            return None

        geographies = (body.get("result") or {}).get("geographies") or {}
        counties = geographies.get("Counties") or []
        if not counties:
            return None
        county = counties[0]
        return CensusGeocodeResult(
            matched_address="",
            latitude=latitude,
            longitude=longitude,
            state_fips=str(county.get("STATE") or ""),
            county_fips=str(county.get("COUNTY") or ""),
            county_name=str(county.get("BASENAME") or county.get("NAME") or ""),
            state_name=str(county.get("STATE_NAME") or ""),
        )

    @staticmethod
    def _parse_match(match: dict[str, Any]) -> CensusGeocodeResult | None:
        coords = match.get("coordinates") or {}
        latitude, longitude = coords.get("y"), coords.get("x")
        if latitude is None or longitude is None:
            return None

        counties = (match.get("geographies") or {}).get("Counties") or []
        if not counties:
            return None
        county = counties[0]

        return CensusGeocodeResult(
            matched_address=str(match.get("matchedAddress") or ""),
            latitude=float(latitude),
            longitude=float(longitude),
            state_fips=str(county.get("STATE") or ""),
            county_fips=str(county.get("COUNTY") or ""),
            county_name=str(county.get("BASENAME") or county.get("NAME") or ""),
            state_name=str(county.get("STATE_NAME") or ""),
        )
