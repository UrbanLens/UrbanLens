"""Tier 1 gateway: generic ArcGIS REST + Socrata SODA parcel queries.

Per ``docs/property-records-plan.md`` section 2 (Tier 1): many counties
expose their parcel layer through an Esri ArcGIS ``MapServer``/``FeatureServer``
``query`` endpoint, or a Socrata/CKAN open-data portal - both are queryable
over plain HTTP with no scraping and no API key. The query *pattern* is
standard across every county running either platform, so one client (this
module) serves all of them; only the specific endpoint URL and field names
differ, which is exactly what ``PropertyJurisdiction`` rows exist to hold.

Unlike every other ``Gateway`` in this codebase, the ~3,000 US counties this
gateway can be pointed at are ~3,000 *different* domains sharing one
``service_key`` (so total call volume is still tracked/rate-limited/logged
the normal way - see ``ApiCallLog``), but each individual county server also
needs its own, much gentler politeness pacing so one pin's fetch never
hammers a specific small-county server that happens to be slow - see
``_pace_host`` and the plan's compliance section ("rate-limit aggressively
per-domain").
"""

from __future__ import annotations

from dataclasses import dataclass
import logging
import time
from typing import TYPE_CHECKING, Any, ClassVar
from urllib.parse import urlsplit

from django.core.cache import cache
import requests

from urbanlens.dashboard.services.gateway import Gateway, GatewayRequestError

if TYPE_CHECKING:
    from urbanlens.dashboard.models.property_jurisdiction.model import PropertyJurisdiction

logger = logging.getLogger(__name__)

#: Minimum seconds between two requests to the same county server host -
#: "1 req/2-3 sec" per the plan's compliance section; kept at the low end
#: since this also has to survive the central service-level rate limit.
_MIN_HOST_INTERVAL_SECONDS = 2.0
_HOST_PACE_CACHE_PREFIX = "proprec:hostpace:"
_HOST_PACE_TTL_SECONDS = 60

#: Exponential backoff on 429/503 - a handful of small county ArcGIS/Socrata
#: instances are genuinely under-provisioned and return these under any load.
_MAX_RETRIES = 3
_BACKOFF_BASE_SECONDS = 2.0

_DEFAULT_TIMEOUT = 20


def _pace_host(url: str) -> None:
    """Sleep just long enough to keep requests to this URL's host politely spaced.

    Args:
        url: The URL about to be requested.
    """
    host = urlsplit(url).netloc
    if not host:
        return
    key = f"{_HOST_PACE_CACHE_PREFIX}{host}"
    last = cache.get(key)
    now = time.monotonic()
    if last is not None:
        wait = _MIN_HOST_INTERVAL_SECONDS - (now - last)
        if wait > 0:
            time.sleep(wait)
    cache.set(key, time.monotonic(), _HOST_PACE_TTL_SECONDS)


@dataclass(slots=True, kw_only=True)
class ArcGisSocrataGateway(Gateway):
    """Generic Tier 1 client for ArcGIS REST and Socrata SODA parcel layers."""

    service_key: ClassVar[str] = "property_records_gis"
    paid_service: ClassVar[bool] = False

    def _get_with_backoff(self, url: str, params: dict[str, Any]) -> requests.Response | None:
        """GET with per-host pacing and exponential backoff on 429/503.

        Args:
            url: Full request URL (a county's ArcGIS/Socrata endpoint).
            params: Query parameters.

        Returns:
            The response, or None after retries are exhausted or the request
            failed outright - callers treat both as "no data available",
            never as a hard error (a single bad county server must not break
            the pipeline for every other jurisdiction).
        """
        for attempt in range(_MAX_RETRIES):
            _pace_host(url)
            try:
                response = self.session.get(url, params=params, timeout=_DEFAULT_TIMEOUT)
            except requests.exceptions.RequestException:
                logger.warning("Property-records GIS request failed for host %s", urlsplit(url).netloc, exc_info=True)
                return None

            if response.status_code in (429, 503):
                backoff = _BACKOFF_BASE_SECONDS * (2**attempt)
                logger.debug("Property-records GIS host %s returned %s, backing off %.1fs", urlsplit(url).netloc, response.status_code, backoff)
                time.sleep(backoff)
                continue

            return response

        return None

    def query_arcgis_by_point(self, service_url: str, latitude: float, longitude: float) -> list[dict[str, Any]]:
        """Query an ArcGIS MapServer/FeatureServer layer for features intersecting a point.

        Args:
            service_url: The layer's service URL (e.g. ``.../MapServer/2``).
            latitude: WGS-84 latitude.
            longitude: WGS-84 longitude.

        Returns:
            List of raw ``attributes`` dicts (may be empty).
        """
        params = {
            "geometry": f"{longitude},{latitude}",
            "geometryType": "esriGeometryPoint",
            "inSR": 4326,
            "spatialRel": "esriSpatialRelIntersects",
            "outFields": "*",
            "returnGeometry": "false",
            "f": "json",
        }
        response = self._get_with_backoff(f"{service_url.rstrip('/')}/query", params)
        if response is None or not response.ok:
            return []
        try:
            body = response.json()
        except ValueError:
            logger.warning("Property-records ArcGIS response wasn't JSON for host %s", urlsplit(service_url).netloc)
            return []
        if body.get("error"):
            logger.debug("Property-records ArcGIS query error from %s: %s", urlsplit(service_url).netloc, body["error"])
            return []
        return [feature["attributes"] for feature in body.get("features") or [] if "attributes" in feature]

    def query_socrata_by_point(self, resource_url: str, geo_field: str, latitude: float, longitude: float, *, radius_meters: float = 60) -> list[dict[str, Any]]:
        """Query a Socrata SODA resource for rows near a point via ``within_circle``.

        Args:
            resource_url: The dataset's ``.json`` SODA endpoint.
            geo_field: Name of the dataset's point/location column.
            latitude: WGS-84 latitude.
            longitude: WGS-84 longitude.
            radius_meters: Search radius - kept small; parcels are queried by
                point, not by area, so this only needs to absorb minor
                geometry/centroid imprecision, not find "nearby" parcels.

        Returns:
            List of raw row dicts (may be empty).
        """
        if not geo_field:
            raise GatewayRequestError("Socrata point query requires PropertyJurisdiction.gis_geo_field to be configured.")
        params = {
            "$where": f"within_circle({geo_field}, {latitude}, {longitude}, {radius_meters})",
            "$order": f"distance_in_meters({geo_field}, {latitude}, {longitude})",
            "$limit": 5,
        }
        response = self._get_with_backoff(resource_url, params)
        if response is None or not response.ok:
            return []
        try:
            rows = response.json()
        except ValueError:
            logger.warning("Property-records Socrata response wasn't JSON for host %s", urlsplit(resource_url).netloc)
            return []
        return rows if isinstance(rows, list) else []

    def query_by_point(self, jurisdiction: PropertyJurisdiction, latitude: float, longitude: float) -> list[dict[str, Any]]:
        """Dispatch a point query to the jurisdiction's configured Tier 1 adapter.

        Args:
            jurisdiction: A registry row with ``adapter_type`` of ``arcgis_rest`` or ``socrata``.
            latitude: WGS-84 latitude.
            longitude: WGS-84 longitude.

        Returns:
            List of raw attribute dicts from whichever adapter applies (empty
            when the jurisdiction has no usable endpoint configured yet).
        """
        from urbanlens.dashboard.models.property_jurisdiction.meta import AdapterType

        if not jurisdiction.gis_rest_url:
            return []
        if jurisdiction.adapter_type == AdapterType.ARCGIS_REST:
            return self.query_arcgis_by_point(jurisdiction.gis_rest_url, latitude, longitude)
        if jurisdiction.adapter_type == AdapterType.SOCRATA:
            return self.query_socrata_by_point(jurisdiction.gis_rest_url, jurisdiction.gis_geo_field, latitude, longitude)
        return []
