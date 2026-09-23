"""Gateway for REData, the standalone property-records service."""

from __future__ import annotations

from dataclasses import dataclass, field
import hashlib
import json
import logging
from typing import TYPE_CHECKING, Any, ClassVar

from urbanlens.dashboard.services.core.coalesce import coalesced
from urbanlens.dashboard.services.core.gateway import Gateway, GatewayRequestError, UpstreamBusyError, read_capped, upstream_retry_after
from urbanlens.UrbanLens.settings.app import settings

if TYPE_CHECKING:
    from collections.abc import Callable

    import requests

logger = logging.getLogger(__name__)

_REQUEST_TIMEOUT = 30
#: Every panel that needs the parcel asks REData the same question as the page opens.
_PARCEL_LOOKUP_SHARE_SECONDS = 3600

#: Mirrors REData's own ``REASON_*`` string constants - a stable contract across the API boundary
#: (REData's values, returned verbatim in its error responses' ``"error"`` field), not Python
#: objects importable across separate repos/deployments.
REASON_MANUAL_ONLY = "manual_only"
REASON_BLOCKED = "blocked"
#: The one reason that must never be cached as a durable "no data" fact - see
#: ``PropertyRecordsUnavailableError``'s docstring.
#: Used both for REData's own ``source_error`` reason and for failures that never reached REData at
#: all (network errors, malformed responses, unexpected status codes) - all of those are equally
REASON_SOURCE_ERROR = "source_error"
#: REData's own outbound pacing refused the call before it reached the county
#: source - distinct from ``REASON_SOURCE_ERROR`` (the source itself failed),
#: and just as transient.
REASON_SOURCE_RATE_LIMITED = "source_rate_limited"
#: The generic per-endpoint form of the same thing, used by REData's
#: single-source endpoints (demographics, the places family, cultural-resource
#: detail) rather than the tiered parcel pipeline.
REASON_RATE_LIMITED = "rate_limited"
#: The key lacks the scope an endpoint needs - settled until the key changes, so not transient.
REASON_FORBIDDEN = "forbidden"

#: Reasons that mean "we could not ask", never "there is nothing here".
#: The existence of a ``LocationCache`` row is what marks a source as fetched, so a caller that
#: stores a payload for one of these turns a passing outage into a blank card for the whole
#: ``external_data_cache_days`` window.
TRANSIENT_REASONS: frozenset[str] = frozenset({REASON_SOURCE_ERROR, REASON_SOURCE_RATE_LIMITED, REASON_RATE_LIMITED})


class PropertyRecordsUnavailableError(GatewayRequestError):
    """Raised when REData reports no record is available, or the request to it failed.

    Attributes:
        reason: REData's ``REASON_*`` string when it responded with a structured error (e.g. ``"manual_only"``, ``"no_data_found"``); ``REASON_SOURCE_ERROR`` for anything REData didn't cleanly report itself (a network failure, a malformed response, or a...
        links: Manual-lookup reference URLs (assessor/treasurer/recorder), when REData supplied them (only for the manual-lookup reasons)."""

    def __init__(self, reason: str, message: str, *, links: dict[str, str] | None = None) -> None:
        self.reason = reason
        self.links = links or {}
        super().__init__(message)


class PropertyRecordsBusyError(PropertyRecordsUnavailableError, UpstreamBusyError):
    """REData throttled this key, or its source is down for now; a caller may retry after ``retry_after`` seconds."""

    def __init__(self, reason: str, message: str, *, retry_after: int) -> None:
        super().__init__(reason, message)
        self.retry_after = retry_after


def _download_failure(response: requests.Response) -> PropertyRecordsUnavailableError:
    """The error for a file download REData answered with neither 200 nor 404.

    Args:
        response: REData's response.

    Returns:
        A :class:`PropertyRecordsBusyError` for a throttle or a source outage, which a caller can retry, else the plain error.
    """
    message = f"REData request failed with status {response.status_code}."
    wait = upstream_retry_after(response)
    if wait is None:
        return PropertyRecordsUnavailableError(REASON_SOURCE_ERROR, message)
    return PropertyRecordsBusyError(REASON_RATE_LIMITED if response.status_code == 429 else REASON_SOURCE_ERROR, message, retry_after=wait)


@dataclass(slots=True, kw_only=True)
class RedataGateway(Gateway):
    """REST client for REData's external property-records API."""

    service_key: ClassVar[str] = "redata_api"
    paid_service: ClassVar[bool] = False

    # default_factory so settings changes apply per instance; a bare default freezes at import.
    base_url: str | None = field(default_factory=lambda: settings.redata_api_url)
    api_key: str | None = field(default_factory=lambda: settings.redata_api_key)

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

    @staticmethod
    def _send(send: Callable[..., requests.Response], url: str, **kwargs: Any) -> requests.Response:
        """Make one request, turning a failure to make it into this gateway's own error.

        Args:
            send: The session method to call.
            url: The absolute URL.
            **kwargs: Passed to the session.

        Returns:
            REData's response, whatever its status.

        Raises:
            PropertyRecordsBusyError: The key is throttled and its wait has not passed, so no request was made.
            PropertyRecordsUnavailableError: REData could not be reached.
        """
        try:
            return send(url, **kwargs)
        except UpstreamBusyError as exc:
            raise PropertyRecordsBusyError(REASON_RATE_LIMITED, str(exc), retry_after=exc.retry_after) from exc
        except OSError as exc:
            raise PropertyRecordsUnavailableError(REASON_SOURCE_ERROR, f"Could not reach REData: {exc}") from exc

    def _get_json(self, path: str, *, params: dict[str, Any] | None = None) -> Any:
        """GET one REData endpoint and return its decoded JSON body.

        Args:
            path: Path relative to ``base_url`` (leading slash optional).
            params: Query-string parameters, if any.

        Returns:
            The raw decoded JSON body.

        Raises:
            PropertyRecordsUnavailableError: Network failure, a non-2xx response REData didn't shape as one of its own structured errors, or an unparseable body.
        """
        base_url = self.base_url
        if base_url is None:
            # __post_init__ already validates this for the normal construction path;
            # this only guards a hypothetical bypass (e.g. object.__new__) and narrows
            # the type for mypy without resorting to assert (banned outside tests).
            raise PropertyRecordsUnavailableError(REASON_SOURCE_ERROR, "UL_REDATA_API_URL is not configured.")
        response = self._send(self.session.get, f"{base_url.rstrip('/')}/{path.lstrip('/')}", params=params, headers=self._headers, timeout=_REQUEST_TIMEOUT)

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
        raise _download_failure(response)

    def _lookup_parcel_body(self, latitude: float, longitude: float, *, situs_address: str = "", apn: str = "") -> dict[str, Any]:
        """Shared implementation for :meth:`lookup_parcel` and :meth:`lookup_parcel_uuid`."""
        params: dict[str, Any] = {"lat": latitude, "lng": longitude}
        if situs_address:
            params["situs_address"] = situs_address
        if apn:
            params["apn"] = apn
        question = hashlib.sha256(json.dumps({**params, "lat": round(latitude, 6), "lng": round(longitude, 6)}, sort_keys=True).encode()).hexdigest()
        return coalesced(f"redata:parcels-lookup:{question}", lambda: dict(self._get_json("/api/v1/parcels/lookup/", params=params) or {}), ttl=_PARCEL_LOOKUP_SHARE_SECONDS)

    def lookup_parcel(self, latitude: float, longitude: float, *, situs_address: str = "", apn: str = "") -> dict[str, Any]:
        """Look up (retrieving/refreshing as needed) the parcel record at a coordinate.

        Args:
            latitude: WGS-84 latitude.
            longitude: WGS-84 longitude.
            situs_address: Already-known street address, passed through to
                REData as an additional search key.
            apn: Already-known parcel/APN, passed through the same way.

        Returns:
            The record payload dict - REData's own ``PropertyRecord.to_dict()`` shape (owner/tax/sale/assessment fields, ``source``, ``confidence``, ``field_sources``/``field_mismatches``, ...).

        Raises:
            PropertyRecordsUnavailableError: No record is available (see the exception's own docstring for how to distinguish a permanent "nothing here" from a transient outage via ``reason``).
        """
        body = self._lookup_parcel_body(latitude, longitude, situs_address=situs_address, apn=apn)
        payload = dict(body.get("record_payload") or {})
        # parcel_geometry/building_geometry are also top-level fields on the Parcel response
        # (alongside record_payload), already converted to standard GeoJSON server-side (REData's
        # own core.services.geojson.esri_rings_to_geojson) - prefer these over record_payload's own
        # copies, which are just whichever tier's raw, still-Esri-ring-shaped PropertyRecord
        for key in ("parcel_geometry", "building_geometry"):
            if key in body:
                payload[key] = body[key]
        # Also a top-level field (see lookup_parcel_uuid) - surfaced here too so
        # callers who already called lookup_parcel don't need a second,
        # identically-parametered request just to get the uuid.
        if "uuid" in body:
            payload["uuid"] = body["uuid"]
        return payload

    def lookup_parcel_uuid(self, latitude: float, longitude: float, *, situs_address: str = "", apn: str = "") -> str | None:
        """Resolve the REData parcel uuid at a coordinate, for uuid-keyed endpoints.

        Args:
            latitude: WGS-84 latitude.
            longitude: WGS-84 longitude.
            situs_address: Already-known street address, passed through to
                REData as an additional search key.
            apn: Already-known parcel/APN, passed through the same way.

        Returns:
            The parcel's uuid, or None if REData's response didn't include one.

        Raises:
            PropertyRecordsUnavailableError: No parcel is available at this coordinate, or the request to REData failed.
        """
        body = self._lookup_parcel_body(latitude, longitude, situs_address=situs_address, apn=apn)
        return body.get("uuid") or None

    def lookup_coverage(self, parcel_uuid: str) -> dict[str, dict[str, Any]]:
        """Return which of REData's supplementary endpoints are worth calling for a parcel.

        Args:
            parcel_uuid: The parcel's REData uuid (see :meth:`lookup_parcel_uuid`).

        Returns:
            ``{"<domain>": {"available": bool, "reason": "..."}, ...}`` - keys are always present regardless of value.

        Raises:
            PropertyRecordsUnavailableError: The request to REData failed.
        """
        body = self._get_json(f"/api/v1/parcels/{parcel_uuid}/coverage/")
        return dict(body) if isinstance(body, dict) else {}

    def lookup_demographics(self, parcel_uuid: str) -> dict[str, Any] | None:
        """Return neighbourhood demographics for the census tract containing a parcel.

        Args:
            parcel_uuid: The parcel's REData uuid (see :meth:`lookup_parcel_uuid`).

        Returns:
            The demographics dict, or None when the parcel has no known coordinate or its coordinate is outside the USA.

        Raises:
            PropertyRecordsUnavailableError: The request to REData failed, or REData 503s the whole endpoint (e.g. ``RD_US_CENSUS_API_KEY`` not configured server-side, or the Census API is rate-limited).
        """
        body = self._get_json(f"/api/v1/parcels/{parcel_uuid}/demographics/") or {}
        demographics = body.get("demographics") if isinstance(body, dict) else None
        return dict(demographics) if isinstance(demographics, dict) else None

    def lookup_national_parks(self, parcel_uuid: str) -> dict[str, Any]:
        """Return the NPS park unit containing a parcel (if any), plus nearby ones.

        Args:
            parcel_uuid: The parcel's REData uuid (see :meth:`lookup_parcel_uuid`).

        Returns:
            ``{"containing_park": <National Park Unit dict, or None>, "nearby_parks": [...]}`` - both null/empty when the parcel has no known coordinate.

        Raises:
            PropertyRecordsUnavailableError: The request to REData failed, or REData reported a transient failure on the containing-park check (a cache hit never reaches this - see the endpoint's own docs).
        """
        body = self._get_json(f"/api/v1/parcels/{parcel_uuid}/national-parks/")
        return dict(body) if isinstance(body, dict) else {}

    def lookup_assessments(self, parcel_uuid: str) -> list[dict[str, Any]]:
        """Return annual assessor valuations near a parcel.

        Args:
            parcel_uuid: The parcel's REData uuid (see :meth:`lookup_parcel_uuid`).

        Returns:
            The raw assessment rows; empty outside covered counties.

        Raises:
            PropertyRecordsUnavailableError: The request to REData failed.
        """
        body = self._get_json(f"/api/v1/parcels/{parcel_uuid}/assessments/") or {}
        return list(body.get("results") or [])

    def lookup_liens(self, parcel_uuid: str) -> list[dict[str, Any]]:
        """Return recorded liens and fines against a parcel.
        ``status`` is free text - publishers spell it inconsistently and REData does not normalise it - so treat it as a label to show, not a value to branch on.

        Args:
            parcel_uuid: The parcel's REData uuid (see :meth:`lookup_parcel_uuid`).

        Returns:
            The raw lien rows, newest filing first; empty outside covered counties.

        Raises:
            PropertyRecordsUnavailableError: The request to REData failed.
        """
        body = self._get_json(f"/api/v1/parcels/{parcel_uuid}/liens/") or {}
        return list(body.get("results") or [])

    def lookup_tax_payments(self, parcel_uuid: str) -> list[dict[str, Any]]:
        """``delinquent`` is the publisher's own determination rather than something derived from ``paid`` - a row can be unpaid but not yet delinquent, since bills are unpaid before their due date.

        Args:
            parcel_uuid: The parcel's REData uuid (see :meth:`lookup_parcel_uuid`).

        Returns:
            The raw payment rows, newest tax year first; empty outside covered counties.

        Raises:
            PropertyRecordsUnavailableError: The request to REData failed.
        """
        body = self._get_json(f"/api/v1/parcels/{parcel_uuid}/tax-payments/") or {}
        return list(body.get("results") or [])

    def lookup_sale_records(self, parcel_uuid: str) -> list[dict[str, Any]]:
        """Return supplementary recorded sales near a parcel.
        Rows are **near-parcel** - ``parcel`` is null and nothing links a row to a specific parcel - so callers must match by address (or a raw PIN in ``attributes``) before attributing a sale to a property.

        Args:
            parcel_uuid: The parcel's REData uuid (see :meth:`lookup_parcel_uuid`).

        Returns:
            The raw sale rows; empty outside covered areas.

        Raises:
            PropertyRecordsUnavailableError: The request to REData failed.
        """
        body = self._get_json(f"/api/v1/parcels/{parcel_uuid}/sale-records/") or {}
        return list(body.get("results") or [])

    def lookup_listings(self, parcel_uuid: str) -> dict[str, Any]:
        """Return cached LoopNet commercial listings for a parcel.

        Args:
            parcel_uuid: The parcel's REData uuid (see :meth:`lookup_parcel_uuid`).

        Returns:
            ``{"results": [...], "refresh_queued": bool}`` - see the module's docs for each listing's fields, including its ``photos`` metadata list (never the file bytes - see :meth:`download_listing_photo`).

        Raises:
            PropertyRecordsUnavailableError: The parcel has no known ``situs_address`` for LoopNet to search by, or the request to REData failed.
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
            PropertyRecordsUnavailableError: The photo was discovered but its download failed (REData never retries this inline), or the request to REData failed outright.
        """
        base_url = self.base_url
        if base_url is None:
            raise PropertyRecordsUnavailableError(REASON_SOURCE_ERROR, "UL_REDATA_API_URL is not configured.")
        response = self._send(self.session.get, f"{base_url.rstrip('/')}/api/v1/listings/{listing_uuid}/photos/{photo_id}/download/", headers=self._headers, timeout=_REQUEST_TIMEOUT, stream=True)
        if response.status_code == 200:
            return read_capped(response, what="LoopNet listing photo"), response.headers.get("Content-Type", "image/jpeg")
        if response.status_code == 404:
            try:
                body = response.json()
            except ValueError:
                body = {}
            raise PropertyRecordsUnavailableError(body.get("error") or REASON_SOURCE_ERROR, body.get("message", ""))
        logger.warning("REData listing photo download failed (%s): %s", response.status_code, response.text[:500])
        raise _download_failure(response)

    def lookup_buildings(self, parcel_uuid: str) -> list[dict[str, Any]]:
        """Return every building REData can find for a parcel, reconciled across sources.
        Never fetches/caches a *new* parcel - this only reads buildings for a parcel REData already resolved (see :meth:`lookup_parcel_uuid`).

        Args:
            parcel_uuid: The parcel's REData uuid.

        Returns:
            One dict per *physical building* (possibly empty), not one per source observation - REData reconciles them (its ``../REData/docs/archive/buildings-dedup-spec.md``).

        Raises:
            PropertyRecordsUnavailableError: The request to REData failed.
        """
        body = self._get_json(f"/api/v1/parcels/{parcel_uuid}/buildings/")
        return list(body) if isinstance(body, list) else []

    def lookup_boundaries(self, parcel_uuid: str) -> list[dict[str, Any]]:
        """Return every boundary candidate REData can find for a parcel, scored.

        Args:
            parcel_uuid: The parcel's REData uuid.

        Returns:
            Candidate dicts (possibly empty), each with ``geometry`` as standard GeoJSON plus ``kind`` (``"parcel"`` for the parcel's own cadastral line, ``"area"`` for something merely related to it), ``confidence``, ``is_suggested`` and ``confidence_breakdown``.

        Raises:
            PropertyRecordsUnavailableError: The request to REData failed.
        """
        body = self._get_json(f"/api/v1/parcels/{parcel_uuid}/boundaries/")
        return list(body) if isinstance(body, list) else []

    def lookup_floorplans(self, parcel_uuid: str, *, building_ref: str = "", on_date: str | None = None) -> list[dict[str, Any]]:
        """List a parcel's floorplan version summaries, resolved by date.
        No floorplan provider exists in REData yet, so an empty list is the expected answer for a long time - absence is quiet.

        Args:
            parcel_uuid: The parcel's REData uuid.
            building_ref: Restrict to one building's plans (the reconciled
                building ``ref``).
            on_date: ISO date to resolve as of; None for current.

        Returns:
            Summary dicts (uuid, building_ref, valid_from, counts), possibly empty.

        Raises:
            PropertyRecordsUnavailableError: The request to REData failed.
        """
        params: dict[str, Any] = {}
        if building_ref:
            params["building_ref"] = building_ref
        if on_date:
            params["date"] = on_date
        body = self._get_json(f"/api/v1/parcels/{parcel_uuid}/floorplans/", params=params or None)
        results = body.get("results") if isinstance(body, dict) else None
        return list(results) if isinstance(results, list) else []

    def lookup_floorplan_document(self, floorplan_uuid: str) -> dict[str, Any] | None:
        """Fetch one floorplan version's full nested document.

        Args:
            floorplan_uuid: The plan version's uuid, from a summary row.

        Returns:
            The document dict, or None when it does not exist.
        """
        try:
            body = self._get_json(f"/api/v1/floorplans/{floorplan_uuid}/")
        except PropertyRecordsUnavailableError:
            return None
        return body if isinstance(body, dict) else None

    def lookup_cultural_resources(self, latitude: float, longitude: float, *, radius_meters: float = 200, provider: str | None = None) -> list[dict[str, Any]]:
        """Find (fetching/caching as needed) cultural/historic resources near a coordinate.
        Only the fast, unauthenticated layer-query tier runs here - a resource's full detail record (including its attachments) is a separate, un-eager step, see :meth:`fetch_cultural_resource_detail`.

        Args:
            latitude: WGS-84 latitude.
            longitude: WGS-84 longitude.
            radius_meters: Search radius around the coordinate.
            provider: Restrict the search to one of REData's registered
                providers. This endpoint answers from a **registry** of state
                and municipal inventories plus the nationwide National
                Register, so an unrestricted call over (say) New York returns
                CRIS *and* NRHP rows in one list. A caller that renders one
                inventory's own fields must name it, or it will sometimes pick
                a row from a different source that has none of them - and pay
                for the other providers' queries besides.

        Returns:
            A list of resource dicts, each tagged with the ``provider`` that answered - see the module docs for each resource's fields.

        Raises:
            PropertyRecordsUnavailableError: The request to REData failed.
        """
        params: dict[str, Any] = {"lat": latitude, "lng": longitude, "radius_meters": radius_meters}
        if provider:
            params["provider"] = provider
        body = self._get_json("/api/v1/cultural-resources/lookup/", params=params)
        if isinstance(body, list):
            return list(body)
        if isinstance(body, dict):
            results = body.get("results")
            if isinstance(results, list):
                return list(results)
        return []

    def fetch_cultural_resource_detail(self, resource_uuid: str) -> dict[str, Any]:
        """Fetch (and cache onto the resource) a CRIS resource's full detail record and attachments.
        REData answers with an envelope - ``{"detail_status": ..., "resource": {...}}`` - because "the source was asked and genuinely publishes nothing deeper" and "detail was retrieved" both leave a resource whose ``detail_retrieved_at`` is set.

        Args:
            resource_uuid: The resource's REData uuid (from :meth:`lookup_cultural_resources`).

        Returns:
            The resource dict, now with ``detail_payload``/``detail_retrieved_at`` and ``attachments`` populated.

        Raises:
            PropertyRecordsUnavailableError: This resource type has no detail-fetch path (e.g. ``archaeological_buffer_area``), or the request to REData failed.
        """
        base_url = self.base_url
        if base_url is None:
            raise PropertyRecordsUnavailableError(REASON_SOURCE_ERROR, "UL_REDATA_API_URL is not configured.")
        response = self._send(self.session.post, f"{base_url.rstrip('/')}/api/v1/cultural-resources/{resource_uuid}/fetch-detail/", headers=self._headers, timeout=_REQUEST_TIMEOUT)
        if response.status_code == 200:
            try:
                body = dict(response.json())
            except ValueError as exc:
                raise PropertyRecordsUnavailableError(REASON_SOURCE_ERROR, "REData returned an unparseable response.") from exc
            resource = body.get("resource")
            return dict(resource) if isinstance(resource, dict) else body
        if response.status_code == 400:
            try:
                body = response.json()
            except ValueError:
                body = {}
            raise PropertyRecordsUnavailableError(body.get("error") or REASON_SOURCE_ERROR, body.get("message", ""))
        logger.warning("REData cultural-resource detail fetch failed (%s): %s", response.status_code, response.text[:500])
        raise PropertyRecordsUnavailableError(REASON_SOURCE_ERROR, f"REData request failed with status {response.status_code}.")

    def queue_cultural_resource_details(self, latitude: float, longitude: float, *, radius_meters: float) -> dict[str, Any]:
        """Ask REData to fetch the detail record of every resource near a coordinate, in the background.
        REData paces the fetches against each provider's own rate budget, so a site with 100+ resources is warmed without one caller spending that budget inline; later lookups then carry each resource's ``attachments``.

        Args:
            latitude: WGS-84 latitude.
            longitude: WGS-84 longitude.
            radius_meters: Search radius around the coordinate, as for :meth:`lookup_cultural_resources`.

        Returns:
            REData's counts - ``queued``/``already_fetched``/``unsupported``/``considered``.

        Raises:
            PropertyRecordsUnavailableError: The key lacks ``cultural_resources:write`` (403), or the request to REData failed.
        """
        base_url = self.base_url
        if base_url is None:
            raise PropertyRecordsUnavailableError(REASON_SOURCE_ERROR, "UL_REDATA_API_URL is not configured.")
        params = {"lat": latitude, "lng": longitude, "radius_meters": radius_meters}
        response = self._send(self.session.post, f"{base_url.rstrip('/')}/api/v1/cultural-resources/fetch-details/", params=params, headers=self._headers, timeout=_REQUEST_TIMEOUT)
        if response.status_code in (200, 202):
            try:
                body = response.json()
            except ValueError:
                body = {}
            return dict(body) if isinstance(body, dict) else {}
        if response.status_code == 403:
            raise PropertyRecordsUnavailableError(REASON_FORBIDDEN, "The REData key lacks cultural_resources:write.")
        logger.warning("REData bulk cultural-resource detail queue failed (%s): %s", response.status_code, response.text[:500])
        raise PropertyRecordsUnavailableError(REASON_SOURCE_ERROR, f"REData request failed with status {response.status_code}.")

    def download_cultural_resource_attachment(self, resource_uuid: str, attachment_id: int) -> tuple[bytes, str]:
        """Download one CRIS attachment/photo's actual file bytes.

        Args:
            resource_uuid: The resource's REData uuid.
            attachment_id: The attachment's id within that resource.

        Returns:
            Tuple of (file bytes, content-type).

        Raises:
            PropertyRecordsUnavailableError: CRIS no longer lists this attachment, or the request to REData failed outright.
        """
        base_url = self.base_url
        if base_url is None:
            raise PropertyRecordsUnavailableError(REASON_SOURCE_ERROR, "UL_REDATA_API_URL is not configured.")
        response = self._send(self.session.get, f"{base_url.rstrip('/')}/api/v1/cultural-resources/{resource_uuid}/attachments/{attachment_id}/download/", headers=self._headers, timeout=_REQUEST_TIMEOUT, stream=True)
        if response.status_code == 200:
            return read_capped(response, what="CRIS attachment"), response.headers.get("Content-Type", "application/octet-stream")
        if response.status_code == 404:
            try:
                body = response.json()
            except ValueError:
                body = {}
            raise PropertyRecordsUnavailableError(body.get("error") or REASON_SOURCE_ERROR, body.get("message", ""))
        logger.warning("REData cultural-resource attachment download failed (%s): %s", response.status_code, response.text[:500])
        raise _download_failure(response)

    def extract_cultural_resource_attachment(self, resource_uuid: str, attachment_id: int, *, timeout: float = _REQUEST_TIMEOUT) -> dict[str, Any]:
        """OCR/AI-extract a downloaded document attachment's fields and any embedded photos.
        Only meaningful for a ``document``-kind attachment (typically a scanned Building-Structure Inventory Form) that's already been downloaded at least once (see :meth:`download_cultural_resource_attachment`).

        Args:
            resource_uuid: The resource's REData uuid.
            attachment_id: The attachment's id within that resource.
            timeout: Seconds to wait; REData extracts synchronously, which can take longer than a lookup.

        Returns:
            The attachment dict with ``extracted_data``/``extracted_at``/ ``extracted_images`` populated - see REData's own ``../REData/docs/api-reference.md`` for the shape.

        Raises:
            PropertyRecordsUnavailableError: The attachment isn't a downloaded document yet (``"not_extractable"``), neither the text nor image extraction found anything at all (``"extraction_unavailable"``), or the request to REData failed outright.
        """
        base_url = self.base_url
        if base_url is None:
            raise PropertyRecordsUnavailableError(REASON_SOURCE_ERROR, "UL_REDATA_API_URL is not configured.")
        response = self._send(
            self.session.post,
            f"{base_url.rstrip('/')}/api/v1/cultural-resources/{resource_uuid}/attachments/{attachment_id}/extract/",
            headers=self._headers,
            timeout=timeout,
        )
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
        response = self._send(
            self.session.get,
            f"{base_url.rstrip('/')}/api/v1/cultural-resources/{resource_uuid}/attachments/{attachment_id}/extracted-images/{image_id}/download/",
            headers=self._headers,
            timeout=_REQUEST_TIMEOUT,
            stream=True,
        )
        if response.status_code == 200:
            return read_capped(response, what="CRIS extracted image"), response.headers.get("Content-Type", "image/jpeg")
        if response.status_code == 404:
            try:
                body = response.json()
            except ValueError:
                body = {}
            raise PropertyRecordsUnavailableError(body.get("error") or REASON_SOURCE_ERROR, body.get("message", ""))
        logger.warning("REData extracted-image download failed (%s): %s", response.status_code, response.text[:500])
        raise _download_failure(response)
