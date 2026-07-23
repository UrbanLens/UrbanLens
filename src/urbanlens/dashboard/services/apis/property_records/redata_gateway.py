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
            raise ValueError("UL_REDATA_API_URL must be configured.")
        if not self.base_url.startswith(("http://", "https://")):
            self.base_url = f"https://{self.base_url}"
        if not self.api_key:
            raise ValueError("UL_REDATA_API_KEY must be configured.")

    @property
    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.api_key}", "Accept": "application/json"}

    def _get_json(self, path: str, *, params: dict[str, Any] | None = None) -> Any:
        """GET one REData endpoint and return its decoded JSON body.

        Shared low-level helper for every read endpoint on this gateway -
        callers translate REData's ``404``/``503`` error shape and any
        network/parse failure into :class:`PropertyRecordsUnavailableError`
        themselves, since what counts as "nothing found" vs. "REData is
        having trouble" differs slightly per endpoint. Returns whatever JSON
        type the endpoint actually uses (most are an object, but e.g. the
        cultural-resources lookup returns a bare array) - callers know their
        own endpoint's shape.

        Args:
            path: Path relative to ``base_url`` (leading slash optional).
            params: Query-string parameters, if any.

        Returns:
            The raw decoded JSON body.

        Raises:
            PropertyRecordsUnavailableError: Network failure, a non-2xx
                response REData didn't shape as one of its own structured
                errors, or an unparseable body.
        """
        base_url = self.base_url
        if base_url is None:
            # __post_init__ already validates this for the normal construction path;
            # this only guards a hypothetical bypass (e.g. object.__new__) and narrows
            # the type for mypy without resorting to assert (banned outside tests).
            raise PropertyRecordsUnavailableError(REASON_SOURCE_ERROR, "UL_REDATA_API_URL is not configured.")
        try:
            response = self.session.get(f"{base_url.rstrip('/')}/{path.lstrip('/')}", params=params, headers=self._headers, timeout=_REQUEST_TIMEOUT)
        except OSError as exc:
            raise PropertyRecordsUnavailableError(REASON_SOURCE_ERROR, f"Could not reach REData: {exc}") from exc

        if response.status_code == 200:
            try:
                return response.json()
            except ValueError as exc:
                raise PropertyRecordsUnavailableError(REASON_SOURCE_ERROR, "REData returned an unparseable response.") from exc

        if response.status_code in (404, 503):
            try:
                body = response.json()
            except ValueError:
                body = {}
            reason = body.get("error") or REASON_SOURCE_ERROR
            raise PropertyRecordsUnavailableError(reason, body.get("message", ""), links=body.get("links"))

        logger.warning("REData request to %s failed (%s): %s", path, response.status_code, response.text[:500])
        raise PropertyRecordsUnavailableError(REASON_SOURCE_ERROR, f"REData request failed with status {response.status_code}.")

    def _lookup_parcel_body(self, latitude: float, longitude: float, *, situs_address: str = "", apn: str = "") -> dict[str, Any]:
        """Shared implementation for :meth:`lookup_parcel` and :meth:`lookup_parcel_uuid`."""
        params: dict[str, Any] = {"lat": latitude, "lng": longitude}
        if situs_address:
            params["situs_address"] = situs_address
        if apn:
            params["apn"] = apn
        return dict(self._get_json("/api/v1/parcels/lookup/", params=params) or {})

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
        body = self._lookup_parcel_body(latitude, longitude, situs_address=situs_address, apn=apn)
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

    def lookup_parcel_uuid(self, latitude: float, longitude: float, *, situs_address: str = "", apn: str = "") -> str | None:
        """Resolve the REData parcel uuid at a coordinate, for uuid-keyed endpoints.

        Endpoints outside the tiered property-records pipeline itself (e.g.
        commercial listings) are keyed by REData's own parcel uuid rather than
        a coordinate - this performs the same lookup as :meth:`lookup_parcel`
        (REData resolves/caches it identically either way) but returns just
        the uuid, without assuming anything about ``record_payload``'s shape.

        Args:
            latitude: WGS-84 latitude.
            longitude: WGS-84 longitude.
            situs_address: Already-known street address, passed through to
                REData as a Tier 2/3 search key.
            apn: Already-known parcel/APN, passed through the same way.

        Returns:
            The parcel's uuid, or None if REData's response didn't include one.

        Raises:
            PropertyRecordsUnavailableError: No parcel is available at this
                coordinate, or the request to REData failed.
        """
        body = self._lookup_parcel_body(latitude, longitude, situs_address=situs_address, apn=apn)
        return body.get("uuid") or None

    def lookup_listings(self, parcel_uuid: str) -> dict[str, Any]:
        """Return cached LoopNet commercial listings for a parcel.

        Never fetches from LoopNet inline with the request, even on a cache
        miss - see the endpoint's own documentation in REData's
        ``docs/api-reference.md`` for why (LoopNet's bot-detection, REData's
        strict outbound budget for it). ``refresh_queued`` in the response
        signals whether this call also queued a background LoopNet fetch.

        Args:
            parcel_uuid: The parcel's REData uuid (see :meth:`lookup_parcel_uuid`).

        Returns:
            ``{"results": [...], "refresh_queued": bool}`` - see the module's
            docs for each listing's fields, including its ``photos`` metadata
            list (never the file bytes - see :meth:`download_listing_photo`).

        Raises:
            PropertyRecordsUnavailableError: The parcel has no known
                ``situs_address`` for LoopNet to search by, or the request
                to REData failed.
        """
        return dict(self._get_json(f"/api/v1/parcels/{parcel_uuid}/listings/") or {})

    def download_listing_photo(self, listing_uuid: str, photo_id: int) -> tuple[bytes, str]:
        """Download one LoopNet listing photo's actual file bytes.

        Args:
            listing_uuid: The listing's REData uuid (from :meth:`lookup_listings`).
            photo_id: The photo's id within that listing.

        Returns:
            Tuple of (file bytes, content-type).

        Raises:
            PropertyRecordsUnavailableError: The photo was discovered but its
                download failed (REData never retries this inline), or the
                request to REData failed outright.
        """
        base_url = self.base_url
        if base_url is None:
            raise PropertyRecordsUnavailableError(REASON_SOURCE_ERROR, "UL_REDATA_API_URL is not configured.")
        try:
            response = self.session.get(f"{base_url.rstrip('/')}/api/v1/listings/{listing_uuid}/photos/{photo_id}/download/", headers=self._headers, timeout=_REQUEST_TIMEOUT)
        except OSError as exc:
            raise PropertyRecordsUnavailableError(REASON_SOURCE_ERROR, f"Could not reach REData: {exc}") from exc
        if response.status_code == 200:
            return response.content, response.headers.get("Content-Type", "image/jpeg")
        if response.status_code == 404:
            try:
                body = response.json()
            except ValueError:
                body = {}
            raise PropertyRecordsUnavailableError(body.get("error") or REASON_SOURCE_ERROR, body.get("message", ""))
        logger.warning("REData listing photo download failed (%s): %s", response.status_code, response.text[:500])
        raise PropertyRecordsUnavailableError(REASON_SOURCE_ERROR, f"REData request failed with status {response.status_code}.")

    def lookup_buildings(self, parcel_uuid: str) -> list[dict[str, Any]]:
        """Return every building REData can find for a parcel, combined across sources.

        Never fetches/caches a *new* parcel - this only reads buildings for a
        parcel REData already resolved (see :meth:`lookup_parcel_uuid`).

        Args:
            parcel_uuid: The parcel's REData uuid.

        Returns:
            A list of ``BuildingRecord`` dicts (possibly empty) - each carries
            at least a coordinate; ``geometry`` is standard GeoJSON (a
            ``Point`` when no boundary is available). ``building_number``/
            ``year_built`` are only ever populated for a ``"cris"``-sourced
            entry today - see REData's own ``docs/api-reference.md``.

        Raises:
            PropertyRecordsUnavailableError: The request to REData failed.
        """
        body = self._get_json(f"/api/v1/parcels/{parcel_uuid}/buildings/")
        return list(body) if isinstance(body, list) else []

    def lookup_cultural_resources(self, latitude: float, longitude: float, *, radius_meters: float = 200) -> list[dict[str, Any]]:
        """Find (fetching/caching as needed) CRIS cultural/historic resources near a coordinate.

        Only the fast, unauthenticated layer-query tier runs here - a
        resource's full detail record (including its attachments) is a
        separate, un-eager step, see :meth:`fetch_cultural_resource_detail`.

        Args:
            latitude: WGS-84 latitude.
            longitude: WGS-84 longitude.
            radius_meters: Search radius around the coordinate.

        Returns:
            A list of resource dicts (empty outside NY, CRIS's only current
            coverage) - see the module docs for each resource's fields.

        Raises:
            PropertyRecordsUnavailableError: The request to REData failed.
        """
        body = self._get_json("/api/v1/cultural-resources/lookup/", params={"lat": latitude, "lng": longitude, "radius_meters": radius_meters})
        if isinstance(body, list):
            return list(body)
        if isinstance(body, dict):
            results = body.get("results")
            if isinstance(results, list):
                return list(results)
        return []

    def fetch_cultural_resource_detail(self, resource_uuid: str) -> dict[str, Any]:
        """Fetch (and cache onto the resource) a CRIS resource's full detail record and attachments.

        Args:
            resource_uuid: The resource's REData uuid (from :meth:`lookup_cultural_resources`).

        Returns:
            The resource dict, now with ``detail_payload``/``detail_retrieved_at``
            and ``attachments`` populated.

        Raises:
            PropertyRecordsUnavailableError: This resource type has no
                detail-fetch path (e.g. ``archaeological_buffer_area``), or
                the request to REData failed.
        """
        base_url = self.base_url
        if base_url is None:
            raise PropertyRecordsUnavailableError(REASON_SOURCE_ERROR, "UL_REDATA_API_URL is not configured.")
        try:
            response = self.session.post(f"{base_url.rstrip('/')}/api/v1/cultural-resources/{resource_uuid}/fetch-detail/", headers=self._headers, timeout=_REQUEST_TIMEOUT)
        except OSError as exc:
            raise PropertyRecordsUnavailableError(REASON_SOURCE_ERROR, f"Could not reach REData: {exc}") from exc
        if response.status_code == 200:
            try:
                return dict(response.json())
            except ValueError as exc:
                raise PropertyRecordsUnavailableError(REASON_SOURCE_ERROR, "REData returned an unparseable response.") from exc
        if response.status_code == 400:
            try:
                body = response.json()
            except ValueError:
                body = {}
            raise PropertyRecordsUnavailableError(body.get("error") or REASON_SOURCE_ERROR, body.get("message", ""))
        logger.warning("REData cultural-resource detail fetch failed (%s): %s", response.status_code, response.text[:500])
        raise PropertyRecordsUnavailableError(REASON_SOURCE_ERROR, f"REData request failed with status {response.status_code}.")

    def download_cultural_resource_attachment(self, resource_uuid: str, attachment_id: int) -> tuple[bytes, str]:
        """Download one CRIS attachment/photo's actual file bytes.

        Unlike listing photos above, this fetches from CRIS on first request
        if not already cached.

        Args:
            resource_uuid: The resource's REData uuid.
            attachment_id: The attachment's id within that resource.

        Returns:
            Tuple of (file bytes, content-type).

        Raises:
            PropertyRecordsUnavailableError: CRIS no longer lists this
                attachment, or the request to REData failed outright.
        """
        base_url = self.base_url
        if base_url is None:
            raise PropertyRecordsUnavailableError(REASON_SOURCE_ERROR, "UL_REDATA_API_URL is not configured.")
        try:
            response = self.session.get(f"{base_url.rstrip('/')}/api/v1/cultural-resources/{resource_uuid}/attachments/{attachment_id}/download/", headers=self._headers, timeout=_REQUEST_TIMEOUT)
        except OSError as exc:
            raise PropertyRecordsUnavailableError(REASON_SOURCE_ERROR, f"Could not reach REData: {exc}") from exc
        if response.status_code == 200:
            return response.content, response.headers.get("Content-Type", "application/octet-stream")
        if response.status_code == 404:
            try:
                body = response.json()
            except ValueError:
                body = {}
            raise PropertyRecordsUnavailableError(body.get("error") or REASON_SOURCE_ERROR, body.get("message", ""))
        logger.warning("REData cultural-resource attachment download failed (%s): %s", response.status_code, response.text[:500])
        raise PropertyRecordsUnavailableError(REASON_SOURCE_ERROR, f"REData request failed with status {response.status_code}.")

    def extract_cultural_resource_attachment(self, resource_uuid: str, attachment_id: int) -> dict[str, Any]:
        """OCR/AI-extract a downloaded document attachment's fields and any embedded photos.

        Only meaningful for a ``document``-kind attachment (typically a
        scanned Building-Structure Inventory Form) that's already been
        downloaded at least once (see :meth:`download_cultural_resource_attachment`).

        Args:
            resource_uuid: The resource's REData uuid.
            attachment_id: The attachment's id within that resource.

        Returns:
            The attachment dict with ``extracted_data``/``extracted_at``/
            ``extracted_images`` populated - see REData's own
            ``docs/api-reference.md`` for the shape.

        Raises:
            PropertyRecordsUnavailableError: The attachment isn't a
                downloaded document yet (``"not_extractable"``), neither the
                text nor image extraction found anything at all
                (``"extraction_unavailable"``), or the request to REData
                failed outright.
        """
        base_url = self.base_url
        if base_url is None:
            raise PropertyRecordsUnavailableError(REASON_SOURCE_ERROR, "UL_REDATA_API_URL is not configured.")
        try:
            response = self.session.post(
                f"{base_url.rstrip('/')}/api/v1/cultural-resources/{resource_uuid}/attachments/{attachment_id}/extract/",
                headers=self._headers,
                timeout=_REQUEST_TIMEOUT,
            )
        except OSError as exc:
            raise PropertyRecordsUnavailableError(REASON_SOURCE_ERROR, f"Could not reach REData: {exc}") from exc
        if response.status_code == 200:
            try:
                return dict(response.json())
            except ValueError as exc:
                raise PropertyRecordsUnavailableError(REASON_SOURCE_ERROR, "REData returned an unparseable response.") from exc
        if response.status_code in (400, 503):
            try:
                body = response.json()
            except ValueError:
                body = {}
            raise PropertyRecordsUnavailableError(body.get("error") or REASON_SOURCE_ERROR, body.get("message", ""))
        logger.warning("REData cultural-resource attachment extraction failed (%s): %s", response.status_code, response.text[:500])
        raise PropertyRecordsUnavailableError(REASON_SOURCE_ERROR, f"REData request failed with status {response.status_code}.")

    def download_extracted_image(self, resource_uuid: str, attachment_id: int, image_id: int) -> tuple[bytes, str]:
        """Download one image extracted from a document attachment's actual file bytes.

        Every row here already has its file saved at extraction time (see
        :meth:`extract_cultural_resource_attachment`) - no lazy-fetch
        fallback, unlike :meth:`download_cultural_resource_attachment`.

        Args:
            resource_uuid: The resource's REData uuid.
            attachment_id: The attachment's id within that resource.
            image_id: The extracted image's id within that attachment.

        Returns:
            Tuple of (file bytes, content-type).

        Raises:
            PropertyRecordsUnavailableError: The request to REData failed.
        """
        base_url = self.base_url
        if base_url is None:
            raise PropertyRecordsUnavailableError(REASON_SOURCE_ERROR, "UL_REDATA_API_URL is not configured.")
        try:
            response = self.session.get(
                f"{base_url.rstrip('/')}/api/v1/cultural-resources/{resource_uuid}/attachments/{attachment_id}/extracted-images/{image_id}/download/",
                headers=self._headers,
                timeout=_REQUEST_TIMEOUT,
            )
        except OSError as exc:
            raise PropertyRecordsUnavailableError(REASON_SOURCE_ERROR, f"Could not reach REData: {exc}") from exc
        if response.status_code == 200:
            return response.content, response.headers.get("Content-Type", "image/jpeg")
        if response.status_code == 404:
            try:
                body = response.json()
            except ValueError:
                body = {}
            raise PropertyRecordsUnavailableError(body.get("error") or REASON_SOURCE_ERROR, body.get("message", ""))
        logger.warning("REData extracted-image download failed (%s): %s", response.status_code, response.text[:500])
        raise PropertyRecordsUnavailableError(REASON_SOURCE_ERROR, f"REData request failed with status {response.status_code}.")
