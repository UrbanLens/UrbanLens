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
``pacing.pace_host``.

Error discipline: "the server couldn't answer" (transport failure, 5xx,
retries exhausted on 429/503) raises :class:`~meta.SourceUnreachableError`,
while "the server answered and this parcel isn't there" returns an empty
list - the orchestrator records the two differently (transient
``REASON_SOURCE_ERROR`` vs cacheable ``REASON_NO_DATA_FOUND``) so a county
outage is never mistaken for a permanent no-data fact about a parcel.
"""

from __future__ import annotations

from dataclasses import dataclass
import logging
from typing import TYPE_CHECKING, Any, ClassVar
from urllib.parse import urlsplit

import requests

from urbanlens.dashboard.services.apis.property_records.meta import SourceUnreachableError
from urbanlens.dashboard.services.apis.property_records.pacing import request_with_backoff
from urbanlens.dashboard.services.gateway import Gateway, GatewayRequestError

if TYPE_CHECKING:
    from urbanlens.dashboard.models.property_jurisdiction.model import PropertyJurisdiction

logger = logging.getLogger(__name__)

_DEFAULT_TIMEOUT = 20


@dataclass(slots=True, kw_only=True)
class ArcGisSocrataGateway(Gateway):
    """Generic Tier 1 client for ArcGIS REST and Socrata SODA parcel layers."""

    service_key: ClassVar[str] = "property_records_gis"  # pyright: ignore[reportIncompatibleVariableOverride]
    paid_service: ClassVar[bool] = False

    def _get(self, url: str, params: dict[str, Any]) -> requests.Response | None:
        """GET with per-host pacing and backoff, classifying failures.

        Args:
            url: Full request URL (a county's ArcGIS/Socrata endpoint).
            params: Query parameters.

        Returns:
            The response for any answered, non-server-error status (including
            4xx - callers treat those as "no data", since a client error
            against a configured endpoint is a configuration fact, not an
            outage), or None for a non-ok status worth treating as no data.

        Raises:
            SourceUnreachableError: Transport failure, 5xx, or 429/503 after
                retries - transient conditions the caller must not record as
                "this parcel has no data".
        """
        host = urlsplit(url).netloc
        try:
            response = request_with_backoff(self.session, "GET", url, params=params, timeout=_DEFAULT_TIMEOUT)
        except requests.exceptions.RequestException as exc:
            logger.warning("Property-records GIS request failed for host %s", host, exc_info=True)
            raise SourceUnreachableError(f"County GIS host {host} could not be reached.") from exc

        if response.status_code == 429 or response.status_code >= 500:
            raise SourceUnreachableError(f"County GIS host {host} answered {response.status_code} after retries.")
        if not response.ok:
            logger.debug("Property-records GIS host %s returned %s; treating as no data", host, response.status_code)
            return None
        return response

    def query_arcgis_by_point(self, service_url: str, latitude: float, longitude: float) -> list[dict[str, Any]]:
        """Query an ArcGIS MapServer/FeatureServer layer for features intersecting a point.

        Args:
            service_url: The layer's service URL (e.g. ``.../MapServer/2``).
            latitude: WGS-84 latitude.
            longitude: WGS-84 longitude.

        Returns:
            List of raw ``attributes`` dicts (may be empty).

        Raises:
            SourceUnreachableError: When the county server can't be reached.
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
        response = self._get(f"{service_url.rstrip('/')}/query", params)
        if response is None:
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

        Raises:
            GatewayRequestError: When ``geo_field`` is blank (caller bug /
                registry misconfiguration - ``query_by_point`` pre-checks
                this so the orchestrator never trips it).
            SourceUnreachableError: When the county server can't be reached.
        """
        if not geo_field:
            raise GatewayRequestError("Socrata point query requires PropertyJurisdiction.gis_geo_field to be configured.")
        # Deliberately no $order/distance_in_meters clause: confirmed live
        # against a real Socrata dataset (New Orleans' parcels resource)
        # that distance_in_meters isn't a supported SoQL function on every
        # backend - it fails the *entire* query with a 400, not just the
        # ordering. within_circle's radius is already small enough that a
        # point query returns at most a small handful of rows regardless of
        # order, so dropping it trades "nearest first" for "the query
        # actually runs" - the only reasonable choice.
        params = {
            "$where": f"within_circle({geo_field}, {latitude}, {longitude}, {radius_meters})",
            "$limit": 5,
        }
        response = self._get(resource_url, params)
        if response is None:
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
            when the jurisdiction has no usable endpoint configured yet - a
            misconfigured row degrades to "no data" with a warning rather
            than breaking the whole pipeline for this coordinate).

        Raises:
            SourceUnreachableError: When the county server can't be reached.
        """
        from urbanlens.dashboard.models.property_jurisdiction.meta import AdapterType

        if not jurisdiction.gis_rest_url:
            return []
        if jurisdiction.adapter_type == AdapterType.ARCGIS_REST:
            return self.query_arcgis_by_point(jurisdiction.gis_rest_url, latitude, longitude)
        if jurisdiction.adapter_type == AdapterType.SOCRATA:
            if not jurisdiction.gis_geo_field:
                logger.warning("Jurisdiction %s is marked Socrata but has no gis_geo_field configured; skipping its Tier 1 query", jurisdiction.fips)
                return []
            return self.query_socrata_by_point(jurisdiction.gis_rest_url, jurisdiction.gis_geo_field, latitude, longitude)
        return []
