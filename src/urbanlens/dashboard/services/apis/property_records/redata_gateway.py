"""Gateway for REData, the standalone property-records service.

REData (``../REData``, a separate repo/deployment) owns the full tiered
county-property-record retrieval pipeline - jurisdiction resolution, ArcGIS/
Socrata, vendor-platform scraping, bespoke per-county recipes - that used to
live directly in this package. See ``docs/property-records-plan.md`` (the
original design) and ``docs/redata.md`` (the extraction/parity audit) for the
history. This gateway is now the only thing in UrbanLens that talks to a
property-record source: it calls REData's REST API
(``GET /api/v1/parcels/lookup/``) and returns the same payload shape
(effectively REData's own ``PropertyRecord.to_dict()``) that
``plugins.builtin.property_records`` already expected from the old local
orchestrator, so nothing downstream of ``_fetch_payload`` needed to change.
"""

from __future__ import annotations

from dataclasses import dataclass
import logging
from typing import Any, ClassVar

from urbanlens.dashboard.services.gateway import Gateway, GatewayRequestError
from urbanlens.UrbanLens.settings.app import settings

logger = logging.getLogger(__name__)

_REQUEST_TIMEOUT = 30

#: Mirrors REData's own ``parcels.services.property_records.orchestrator.REASON_*``
#: string constants - a stable contract across the API boundary (REData's
#: values, returned verbatim in its error responses' ``"error"`` field), not
#: Python objects importable across separate repos/deployments.
REASON_MANUAL_ONLY = "manual_only"
REASON_BLOCKED = "blocked"
#: The one reason that must never be cached as a durable "no data" fact - see
#: ``PropertyRecordsUnavailableError``'s docstring. Used both for REData's own
#: ``source_error`` reason and for failures that never reached REData at all
#: (network errors, malformed responses, unexpected status codes) - all of
#: those are equally transient from a caller's point of view.
REASON_SOURCE_ERROR = "source_error"


class PropertyRecordsUnavailableError(GatewayRequestError):
    """Raised when REData reports no record is available, or the request to it failed.

    Attributes:
        reason: REData's ``REASON_*`` string when it responded with a
            structured error (e.g. ``"manual_only"``, ``"no_data_found"``);
            ``REASON_SOURCE_ERROR`` for anything REData didn't cleanly report
            itself (a network failure, a malformed response, or a REData-side
            outage/5xx not shaped like its own error responses).
        links: Manual-lookup reference URLs (assessor/treasurer/recorder),
            when REData supplied them (only for the manual-lookup reasons).
    """

    def __init__(self, reason: str, message: str, *, links: dict[str, str] | None = None) -> None:
        self.reason = reason
        self.links = links or {}
        super().__init__(message)


@dataclass(slots=True, kw_only=True)
class RedataGateway(Gateway):
    """REST client for REData's external property-records API."""

    service_key: ClassVar[str] = "redata_api"
    paid_service: ClassVar[bool] = False

    base_url: str | None = settings.redata_api_url
    api_key: str | None = settings.redata_api_key

    def __post_init__(self) -> None:
        Gateway.__post_init__(self)
        if not self.base_url:
            raise ValueError("REDATA_API_URL must be configured.")
        if not self.api_key:
            raise ValueError("REDATA_API_KEY must be configured.")

    @property
    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.api_key}", "Accept": "application/json"}

    def lookup_parcel(self, latitude: float, longitude: float, *, situs_address: str = "", apn: str = "") -> dict[str, Any]:
        """Look up (retrieving/refreshing as needed) the parcel record at a coordinate.

        Args:
            latitude: WGS-84 latitude.
            longitude: WGS-84 longitude.
            situs_address: Already-known street address, passed through to
                REData as a Tier 2/3 search key.
            apn: Already-known parcel/APN, passed through the same way.

        Returns:
            The record payload dict - REData's own ``PropertyRecord.to_dict()``
            shape (owner/tax/sale/assessment fields, ``source``, ``confidence``,
            ``field_sources``/``field_mismatches``, ...).

        Raises:
            PropertyRecordsUnavailableError: No record is available (see the
                exception's own docstring for how to distinguish a permanent
                "nothing here" from a transient outage via ``reason``).
        """
        base_url = self.base_url
        if base_url is None:
            # __post_init__ already validates this for the normal construction path;
            # this only guards a hypothetical bypass (e.g. object.__new__) and narrows
            # the type for mypy without resorting to assert (banned outside tests).
            raise PropertyRecordsUnavailableError(REASON_SOURCE_ERROR, "REDATA_API_URL is not configured.")

        params: dict[str, Any] = {"lat": latitude, "lng": longitude}
        if situs_address:
            params["situs_address"] = situs_address
        if apn:
            params["apn"] = apn

        try:
            response = self.session.get(f"{base_url.rstrip('/')}/api/v1/parcels/lookup/", params=params, headers=self._headers, timeout=_REQUEST_TIMEOUT)
        except OSError as exc:
            raise PropertyRecordsUnavailableError(REASON_SOURCE_ERROR, f"Could not reach REData: {exc}") from exc

        if response.status_code == 200:
            try:
                body = response.json()
            except ValueError as exc:
                raise PropertyRecordsUnavailableError(REASON_SOURCE_ERROR, "REData returned an unparseable response.") from exc
            payload = dict(body.get("record_payload") or {})
            # parcel_geometry/building_geometry are also top-level fields on the
            # Parcel response (alongside record_payload), already converted to
            # standard GeoJSON server-side (REData's own
            # core.services.geojson.esri_rings_to_geojson) - prefer these over
            # record_payload's own copies, which are just whichever tier's raw,
            # still-Esri-ring-shaped PropertyRecord snapshot was last written.
            for key in ("parcel_geometry", "building_geometry"):
                if key in body:
                    payload[key] = body[key]
            return payload

        if response.status_code in (404, 503):
            try:
                body = response.json()
            except ValueError:
                body = {}
            reason = body.get("error") or REASON_SOURCE_ERROR
            raise PropertyRecordsUnavailableError(reason, body.get("message", ""), links=body.get("links"))

        logger.warning("REData lookup failed (%s): %s", response.status_code, response.text[:500])
        raise PropertyRecordsUnavailableError(REASON_SOURCE_ERROR, f"REData request failed with status {response.status_code}.")
