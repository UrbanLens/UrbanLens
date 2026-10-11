"""Gateway for REData, the standalone property-records service."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
import hashlib
import json
import logging
import math
import time
from typing import TYPE_CHECKING, Any, ClassVar

from django.core.cache import DEFAULT_CACHE_ALIAS

from urbanlens.dashboard.services.core.bounded_cache import get_or_none, set_or_skip
from urbanlens.dashboard.services.core.coalesce import coalesced
from urbanlens.dashboard.services.core.gateway import UPSTREAM_BUSY_MAX_SECONDS, Gateway, GatewayRequestError, UpstreamBusyError, read_capped, upstream_retry_after
from urbanlens.dashboard.services.core.upstream_breaker import RedataBreaker
from urbanlens.UrbanLens.settings.app import settings

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping

    import requests

    from urbanlens.dashboard.services.apis.locations.redata_context_gateway import LocationContextEnvelope

logger = logging.getLogger(__name__)

_REQUEST_TIMEOUT = 30
#: Every panel that needs the parcel asks REData the same question as the page opens.
_PARCEL_LOOKUP_SHARE_SECONDS = 3600
#: How long a parcel lookup REData answered as partial (``complete: false``) is shared. REData holds such a record for
#: at least five minutes before it asks the tier it lacks again, so a shorter share would only repeat the same partial
#: answer, and an hour would keep the partial one from the retry that completes it.
_PARTIAL_PARCEL_LOOKUP_SHARE_SECONDS = 300
#: The ``geography_level`` of a demographics answer given for the county where the tract was asked about.
_COUNTY_LEVEL = "county"
#: The key REData's own ``record_payload`` lists its unanswered tiers under, read only when a body carries no
#: top-level ``complete``/``sources``. It happens to equal :data:`~urbanlens.dashboard.models.cache.location_cache.UNANSWERED_SOURCES_KEY`.
_RECORD_UNANSWERED_KEY = "unanswered_sources"
#: Every building on a campus matches the same CRIS record, and extracting one of its documents is OCR on REData's side.
_CULTURAL_RESOURCE_SHARE_SECONDS = 3600
#: How long a refusal to extract a document is believed. REData's "found nothing" also covers an unreachable AI
#: provider, so this is a backoff, not a verdict.
_EXTRACTION_REFUSAL_SECONDS = 7 * 24 * 60 * 60
#: How long a parcel lookup REData answered "retry later" for, without being down, is left before it is asked again.
#: Every ask re-runs REData's whole tier pipeline, and REData stores none of these answers.
_UNSETTLED_PARCEL_RETRY_SECONDS = 12 * 60 * 60
#: Rows per page of a paginated parcel list: REData's ``max_page_size``, so most parcels take one request.
_PARCEL_ROW_PAGE_SIZE = 500
#: Pages of one paginated parcel list read before stopping. A card summarises these rows, so a parcel with more is
#: shown from its first ``MAX_PARCEL_ROW_PAGES * _PARCEL_ROW_PAGE_SIZE`` rather than costing a request per hundred.
MAX_PARCEL_ROW_PAGES = 4

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

#: How long a resolved and an unresolved building-ref answer are kept (see ``RedataGateway.resolve_building_ref``).
_RESOLVED_REF_SECONDS = 24 * 3600
_UNRESOLVED_REF_SECONDS = 3600
#: REData's own outbound pacing refused the call before it reached the county
#: source - distinct from ``REASON_SOURCE_ERROR`` (the source itself failed),
#: and just as transient.
REASON_SOURCE_RATE_LIMITED = "source_rate_limited"
#: The generic per-endpoint form of the same thing, used by REData's
#: single-source endpoints (demographics, the places family, cultural-resource
#: detail) rather than the tiered parcel pipeline.
REASON_RATE_LIMITED = "rate_limited"
#: What refused the call was the share of REData's budget the requesting key's environment may spend, not REData's
#: whole budget. REData answers it where it would otherwise answer ``REASON_SOURCE_RATE_LIMITED`` (the parcel lookup)
#: or ``REASON_RATE_LIMITED`` (the single-source endpoints): nothing was asked of the source, so it is just as
#: transient and never an answer about the place.
REASON_KEY_BUDGET_EXHAUSTED = "key_budget_exhausted"
#: REData refused the key for the endpoint (a scope it lacks). Says nothing about the place asked about.
REASON_FORBIDDEN = "forbidden"
#: No source behind a near-point endpoint answered (cultural resources' 503).
REASON_ALL_PROVIDERS_UNAVAILABLE = "all_providers_unavailable"
#: An attachment REData cannot extract: not a document, a provider with no extraction, or a file it does not hold yet.
REASON_NOT_EXTRACTABLE = "not_extractable"
#: REData read a document and found neither fields nor photos, or could not reach its OCR or AI provider to look.
REASON_EXTRACTION_UNAVAILABLE = "extraction_unavailable"
#: CRIS no longer lists the attachment.
REASON_ATTACHMENT_UNAVAILABLE = "attachment_unavailable"
#: REData's CRIS detail fetch answered 200 ``unresolved``: the source could not resolve the resource to a record, so REData holds
#: it until ``retry_after``. Not a claim that the record does not exist, and not an outage of REData.
REASON_DETAIL_UNRESOLVED = "detail_unresolved"
#: The ``detail_status`` of that answer.
_DETAIL_UNRESOLVED = "unresolved"

#: REData is computing a cold parcel's unfiltered boundaries, buildings and related buildings in the background (its
#: P62, 0.3.7): ask again after the ``retry_after`` its 503 body names. The wait is the parcel's, so it carries no
#: ``Retry-After`` header, which would hold off every parcel's calls.
REASON_REFRESH_QUEUED = "refresh_queued"
#: The same, when the computation outran the request and could not be queued, or failed in the background lately.
REASON_COMPUTE_TIMEOUT = "compute_timeout"
#: The 503 ``error`` codes that mean REData will have the parcel's answer after the body's ``retry_after``, as REData
#: 0.3.7 to 0.3.9 send them. Later releases send ``error: source_error`` and the code in ``pending``.
COMPUTING_REASONS: frozenset[str] = frozenset({REASON_REFRESH_QUEUED, REASON_COMPUTE_TIMEOUT})
#: The wait for a computing answer whose body names none.
_COMPUTING_DEFAULT_SECONDS = 60

#: Reasons that mean "we could not ask", never "there is nothing here".
#: The existence of a ``LocationCache`` row is what marks a source as fetched, so a caller that
#: stores a payload for one of these turns a passing outage into a blank card for the whole
#: ``external_data_cache_days`` window.
TRANSIENT_REASONS: frozenset[str] = frozenset({REASON_SOURCE_ERROR, REASON_SOURCE_RATE_LIMITED, REASON_RATE_LIMITED, REASON_KEY_BUDGET_EXHAUSTED, REASON_ALL_PROVIDERS_UNAVAILABLE, REASON_DETAIL_UNRESOLVED})
_SETTLED_EXTRACTION_REFUSALS: frozenset[str] = frozenset({REASON_NOT_EXTRACTABLE, REASON_EXTRACTION_UNAVAILABLE, REASON_ATTACHMENT_UNAVAILABLE})


class PropertyRecordsUnavailableError(GatewayRequestError):
    """Raised when REData reports no record is available, or the request to it failed.

    Attributes:
        reason: REData's ``REASON_*`` string when it responded with a structured error (e.g. ``"manual_only"``, ``"no_data_found"``); ``REASON_SOURCE_ERROR`` for anything REData didn't cleanly report itself (a network failure, a malformed response, or a...
        links: Manual-lookup reference URLs (assessor/treasurer/recorder), when REData supplied them (only for the manual-lookup reasons).
        retry_later: REData said to ask again later (a 503), whatever its reason.
        status_code: REData's HTTP status, when it answered with one this error records (a 404 or 503)."""

    def __init__(self, reason: str, message: str, *, links: dict[str, str] | None = None, retry_later: bool = False, status_code: int | None = None) -> None:
        self.reason = reason
        self.links = links or {}
        self.retry_later = retry_later
        self.status_code = status_code
        super().__init__(message)

    @property
    def is_outage(self) -> bool:
        """A retry-later answer or a reason in :data:`TRANSIENT_REASONS` learned nothing; anything else is REData's settled answer."""
        return self.retry_later or self.reason in TRANSIENT_REASONS


class PropertyRecordsBusyError(PropertyRecordsUnavailableError, UpstreamBusyError):
    """REData throttled this key, or its source is down for now; a caller may retry after ``retry_after`` seconds."""

    def __init__(self, reason: str, message: str, *, retry_after: int, links: dict[str, str] | None = None) -> None:
        super().__init__(reason, message, links=links, retry_later=True)
        self.retry_after = retry_after


class PropertyRecordsComputingError(PropertyRecordsBusyError):
    """REData is computing this parcel's answer in the background and will have it after ``retry_after`` seconds.

    Not an outage of REData or a source: a caller neither caches the silence nor settles for a fallback's answer, and
    asks again once the wait is over.
    """

    answer_pending: ClassVar[bool] = True


def _computing_reason(reason: str, body: Mapping[str, Any]) -> str | None:
    """Why a 503 says REData is still computing the answer, or None when it does not say so.

    Args:
        reason: The body's ``error``.
        body: REData's 503 body.

    Returns:
        The body's ``pending`` when it names one, any code included: it means "ask again after ``retry_after``" whatever
        the code. Otherwise ``reason`` when it is one of :data:`COMPUTING_REASONS`.
    """
    pending = body.get("pending")
    if isinstance(pending, str) and pending:
        return pending
    return reason if reason in COMPUTING_REASONS else None


def _computing_wait(body: Mapping[str, Any]) -> int:
    """The wait a computing 503's body names, in seconds.

    Args:
        body: REData's 503 body.

    Returns:
        Seconds, at least 1 and at most :data:`UPSTREAM_BUSY_MAX_SECONDS`; :data:`_COMPUTING_DEFAULT_SECONDS` when the
        body names none.
    """
    seconds = _seconds_from_now(body.get("retry_after"))
    if seconds is None or not math.isfinite(seconds):
        return _COMPUTING_DEFAULT_SECONDS
    return max(1, min(math.ceil(seconds), UPSTREAM_BUSY_MAX_SECONDS))


def _refused(response: requests.Response) -> PropertyRecordsBusyError:
    """The error for an endpoint REData refused this key, held off for as long as the breaker holds the endpoint.

    Args:
        response: REData's 401 or 403.

    Returns:
        A busy error, so a caller neither caches it as an answer nor asks again soon.
    """
    return PropertyRecordsBusyError(REASON_FORBIDDEN, f"REData refused this key ({response.status_code}); it may lack the endpoint's scope.", retry_after=RedataBreaker.REFUSED_SECONDS)


def _download_failure(response: requests.Response) -> PropertyRecordsUnavailableError:
    """The error for a request REData answered with a status its caller does not handle itself.

    Args:
        response: REData's response.

    Returns:
        A :class:`PropertyRecordsBusyError` for a refusal, a throttle or a source outage, which a caller can retry, else the plain error.
    """
    if response.status_code in RedataBreaker.REFUSED_STATUSES:
        return _refused(response)
    message = f"REData request failed with status {response.status_code}."
    wait = upstream_retry_after(response)
    if wait is None:
        return PropertyRecordsUnavailableError(REASON_SOURCE_ERROR, message)
    return PropertyRecordsBusyError(REASON_RATE_LIMITED if response.status_code == 429 else REASON_SOURCE_ERROR, message, retry_after=wait)


def _hold_wait(retry_after: object) -> int:
    """How long to leave a resource REData is holding, from the ``retry_after`` of its ``unresolved`` answer.

    Args:
        retry_after: REData's ``retry_after``: the time the hold ends, as an ISO 8601 timestamp, or a number of seconds. Anything else is unread.

    Returns:
        Seconds, at least 1 and at most :data:`UPSTREAM_BUSY_MAX_SECONDS`, which is also the wait for an answer that names none: REData holds a resource for an hour at least.
    """
    seconds = _seconds_from_now(retry_after)
    if seconds is None or not math.isfinite(seconds):
        return UPSTREAM_BUSY_MAX_SECONDS
    return max(1, min(math.ceil(seconds), UPSTREAM_BUSY_MAX_SECONDS))


def _seconds_from_now(value: object) -> float | None:
    """A wait REData named, as seconds from now: a number of seconds, or the ISO 8601 time it ends; None when it is neither."""
    if isinstance(value, bool) or not isinstance(value, int | float | str):
        return None
    if not isinstance(value, str):
        return float(value)
    try:
        return float(value)
    except ValueError:
        pass
    try:
        ends = datetime.fromisoformat(value.strip())
    except ValueError:
        return None
    return (ends if ends.tzinfo else ends.replace(tzinfo=UTC)).timestamp() - time.time()


def _named_wait(response: requests.Response) -> int | None:
    """The wait a 503 named in its ``Retry-After`` header, or None.

    Args:
        response: REData's 503.

    Returns:
        Seconds, bounded as every upstream wait is.
    """
    return upstream_retry_after(response) if str(response.headers.get("Retry-After", "")).strip() else None


def parcel_unanswered_sources(body: Mapping[str, Any]) -> list[Any] | None:
    """The tiers REData could not hear from for a parcel, as its lookup body says.

    The top-level ``complete`` and ``sources`` (each entry ``{tier, status, host, http_status, message}``, as a failed
    source in the lookup's 503) say it when present; ``record_payload.unanswered_sources`` is the fallback, for a REData
    that does not answer them yet.

    Args:
        body: REData's parcel body, from ``/api/v1/parcels/lookup/``.

    Returns:
        The unanswered sources; ``["unknown"]`` for an incomplete body that names none; ``[]`` for a complete one; None
        when the body says nothing either way (an older REData, or a malformed field).
    """
    record = body.get("record_payload")
    own = record.get(_RECORD_UNANSWERED_KEY) if isinstance(record, dict) else None
    fallback = list(own) if isinstance(own, list) else None
    complete, sources = body.get("complete"), body.get("sources")
    if not isinstance(complete, bool) and not isinstance(sources, list):
        return fallback
    if named := [source for source in sources if source] if isinstance(sources, list) else []:
        return named
    return (fallback or ["unknown"]) if complete is False else []


def unanswered_tiers(payload: Mapping[str, Any]) -> frozenset[int]:
    """The REData tier numbers (1 is the county GIS parcel layer) a parcel payload lacks.

    Args:
        payload: A :meth:`RedataGateway.lookup_parcel` payload.

    Returns:
        The tiers named under the payload's unanswered sources; empty for a complete answer, and for one that names
        its missing sources without a tier.
    """
    from urbanlens.dashboard.models.cache.location_cache import UNANSWERED_SOURCES_KEY

    named = payload.get(UNANSWERED_SOURCES_KEY)
    tiers: set[int] = set()
    for source in named if isinstance(named, list) else []:
        if not isinstance(source, dict):
            continue
        try:
            tiers.add(int(source["tier"]))
        except (KeyError, TypeError, ValueError):
            continue
    return frozenset(tiers)


def _parcel_lookup_share_seconds(body: Mapping[str, Any]) -> int:
    """How long a parcel lookup's answer is shared: briefly when REData says it is partial, else :data:`_PARCEL_LOOKUP_SHARE_SECONDS`."""
    return _PARTIAL_PARCEL_LOOKUP_SHARE_SECONDS if parcel_unanswered_sources(body) else _PARCEL_LOOKUP_SHARE_SECONDS


def _demographics_share_seconds(body: Any) -> int:
    """How long a parcel's demographics answer is shared.

    Briefly when REData says the answer is partial or degraded, or answered the tract question at county level, which it
    does when it cannot resolve the tract (as before its tract layer has synced). An older REData sends no level.

    Args:
        body: REData's ``/parcels/{uuid}/demographics/`` body.

    Returns:
        Seconds.
    """
    if not isinstance(body, dict):
        return _PARCEL_LOOKUP_SHARE_SECONDS
    demographics = body.get("demographics")
    coarser = isinstance(demographics, dict) and demographics.get("geography_level") == _COUNTY_LEVEL
    return _PARTIAL_PARCEL_LOOKUP_SHARE_SECONDS if coarser or body.get("complete") is False or body.get("degraded") else _PARCEL_LOOKUP_SHARE_SECONDS


def _failure_memo(exc: PropertyRecordsUnavailableError) -> dict[str, Any]:
    """What :func:`_remembered_failure` needs to raise ``exc`` again, as plain data the cache can hold."""
    return {
        "reason": exc.reason,
        "message": str(exc),
        "links": dict(exc.links),
        "retry_later": exc.retry_later,
        "status_code": exc.status_code,
        "retry_after": exc.retry_after if isinstance(exc, PropertyRecordsBusyError) else None,
    }


def _remembered_failure(memo: Mapping[str, Any]) -> PropertyRecordsUnavailableError:
    """The error a :func:`_failure_memo` was taken from, as a caller classifies it.

    Args:
        memo: The remembered failure.

    Returns:
        A busy error when REData named a wait, else the plain error with the same reason, status and retry flag.
    """
    reason, message = str(memo.get("reason") or REASON_SOURCE_ERROR), str(memo.get("message") or "")
    links = memo.get("links") if isinstance(memo.get("links"), dict) else None
    retry_after = memo.get("retry_after")
    if isinstance(retry_after, int):
        return PropertyRecordsBusyError(reason, message, retry_after=retry_after, links=links)
    status_code = memo.get("status_code")
    return PropertyRecordsUnavailableError(reason, message, links=links, retry_later=bool(memo.get("retry_later")), status_code=status_code if isinstance(status_code, int) else None)


#: Names the sources REData asked and could not hear from, on an answer that is therefore partial.
UNANSWERED_SOURCES_HEADER = "X-REData-Unanswered-Sources"


def _provider_results(body: Any) -> LocationContextEnvelope:
    """A near-a-parcel answer in REData's provider envelope, keeping whether every provider answered.

    Args:
        body: REData's ``{count, complete, results, providers}`` body.

    Returns:
        The rows and per-provider outcomes. A body without ``complete`` (an older REData) is taken as complete.
    """
    from urbanlens.dashboard.services.apis.locations.redata_context_gateway import LocationContextEnvelope

    if not isinstance(body, dict):
        return LocationContextEnvelope(count=0, complete=True)
    results = body.get("results")
    rows = [row for row in results if isinstance(row, dict)] if isinstance(results, list) else []
    outcomes = body.get("providers")
    providers = [entry for entry in outcomes if isinstance(entry, dict)] if isinstance(outcomes, list) else []
    return LocationContextEnvelope(count=len(rows), complete=body.get("complete") is not False, results=rows, providers=providers)


@dataclass(slots=True, frozen=True)
class ParcelBuildings:
    """A parcel's buildings as REData answered them.

    Attributes:
        buildings: One dict per physical building.
        unanswered_sources: Sources REData asked and could not hear from; when any are named the list is a floor.
    """

    buildings: list[dict[str, Any]]
    unanswered_sources: tuple[str, ...] = ()


@dataclass(slots=True, frozen=True)
class ParcelBoundaries:
    """A parcel's boundary candidates as REData answered them.

    Attributes:
        candidates: The scored candidates, as :meth:`RedataGateway.lookup_boundaries` describes them.
        unanswered_sources: Sources REData asked and could not hear from; when any are named, a candidate a missing
            source would have outranked may be the one suggested.
    """

    candidates: list[dict[str, Any]]
    unanswered_sources: tuple[str, ...] = ()


def _unanswered_header(headers: Mapping[str, Any]) -> tuple[str, ...]:
    """The sources a parcel answer's ``X-REData-Unanswered-Sources`` header names.

    Args:
        headers: The response headers.

    Returns:
        The named sources; empty when the header is absent, as it is on a complete answer and from an older REData.
    """
    return tuple(name.strip() for name in str(headers.get(UNANSWERED_SOURCES_HEADER, "")).split(",") if name.strip())


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
            PropertyRecordsBusyError: The key is throttled, or REData refused it this endpoint, and the wait has not
                passed, so no request was made; ``reason`` says which, as the refusal itself did.
            PropertyRecordsUnavailableError: REData could not be reached.
        """
        try:
            return send(url, **kwargs)
        except UpstreamBusyError as exc:
            reason = REASON_FORBIDDEN if RedataBreaker().refuses(url) else REASON_RATE_LIMITED
            raise PropertyRecordsBusyError(reason, str(exc), retry_after=exc.retry_after) from exc
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
        return self._get_json_and_headers(path, params=params)[0]

    def _get_json_and_headers(self, path: str, *, params: dict[str, Any] | None = None) -> tuple[Any, Mapping[str, str]]:
        """GET one REData endpoint and return its decoded JSON body with the response headers.

        Args:
            path: Path relative to ``base_url`` (leading slash optional).
            params: Query-string parameters, if any.

        Returns:
            The raw decoded JSON body, and the headers it came with.

        Raises:
            PropertyRecordsUnavailableError: As :meth:`_get_json`.
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
                return response.json(), response.headers
            except ValueError as exc:
                raise PropertyRecordsUnavailableError(REASON_SOURCE_ERROR, "REData returned an unparseable response.") from exc

        if response.status_code in (404, 503):
            try:
                body = response.json()
            except ValueError:
                body = {}
            if not isinstance(body, dict):
                body = {}
            reason = body.get("error") or REASON_SOURCE_ERROR
            message = body.get("message", "")
            links = body.get("links") if isinstance(body.get("links"), dict) else None
            # REData answers 404 for a permanent reason and 503 for one worth asking about again.
            if response.status_code == 404:
                raise PropertyRecordsUnavailableError(reason, message, links=links, status_code=404)
            if (computing := _computing_reason(reason, body)) is not None:
                raise PropertyRecordsComputingError(computing, message, retry_after=_computing_wait(body), links=links)
            if (wait := _named_wait(response)) is not None:
                raise PropertyRecordsBusyError(reason, message, retry_after=wait, links=links)
            raise PropertyRecordsUnavailableError(reason, message, links=links, retry_later=True)

        if response.status_code in RedataBreaker.REFUSED_STATUSES:
            logger.info("REData refused %s (%s)", path, response.status_code)
            raise _refused(response)
        logger.warning("REData request to %s failed (%s): %s", path, response.status_code, response.text[:500])
        raise _download_failure(response)

    def _get_pages(self, path: str) -> list[dict[str, Any]]:
        """Every row of a page-number-paginated REData list, up to :data:`MAX_PARCEL_ROW_PAGES` pages.

        Pages are asked for by number on this gateway's own host: ``next`` names whatever host REData saw the request
        on, and following it would hand the API key to that host.

        Args:
            path: Path relative to ``base_url``.

        Returns:
            The rows, in REData's order.

        Raises:
            PropertyRecordsUnavailableError: Any page could not be read; part of a list is not offered as all of it.
        """
        rows: list[dict[str, Any]] = []
        for page in range(1, MAX_PARCEL_ROW_PAGES + 1):
            body = self._get_json(path, params={"page": page, "page_size": _PARCEL_ROW_PAGE_SIZE})
            if not isinstance(body, dict):
                break
            rows.extend(row for row in body.get("results") or [] if isinstance(row, dict))
            if not body.get("next"):
                break
        return rows

    def _lookup_parcel_body(self, latitude: float, longitude: float, *, situs_address: str = "", apn: str = "") -> dict[str, Any]:
        """Shared implementation for :meth:`lookup_parcel` and :meth:`lookup_parcel_uuid`."""
        params: dict[str, Any] = {"lat": latitude, "lng": longitude}
        if situs_address:
            params["situs_address"] = situs_address
        if apn:
            params["apn"] = apn
        question = hashlib.sha256(json.dumps({**params, "lat": round(latitude, 6), "lng": round(longitude, 6)}, sort_keys=True).encode()).hexdigest()
        deferred_key = f"redata:parcels-lookup-deferred:{question}"
        deferred = get_or_none(deferred_key, label="parcel lookup deferral", alias=DEFAULT_CACHE_ALIAS)
        if isinstance(deferred, dict):
            raise PropertyRecordsBusyError(str(deferred.get("reason") or REASON_SOURCE_ERROR), str(deferred.get("message") or ""), retry_after=_UNSETTLED_PARCEL_RETRY_SECONDS)
        try:
            return coalesced(f"redata:parcels-lookup:{question}", lambda: dict(self._get_json("/api/v1/parcels/lookup/", params=params) or {}), ttl=_parcel_lookup_share_seconds)
        except PropertyRecordsUnavailableError as exc:
            # A busy error already says when to ask again (Retry-After, a throttle, a refused key the breaker holds).
            if not exc.retry_later or exc.reason in TRANSIENT_REASONS or isinstance(exc, PropertyRecordsBusyError):
                raise
            # Nothing was learned and nothing is down: no county source found the parcel, or none could be searched.
            set_or_skip(deferred_key, {"reason": exc.reason, "message": str(exc)}, _UNSETTLED_PARCEL_RETRY_SECONDS, label="parcel lookup deferral", alias=DEFAULT_CACHE_ALIAS)
            raise PropertyRecordsBusyError(exc.reason, str(exc), retry_after=_UNSETTLED_PARCEL_RETRY_SECONDS, links=exc.links) from exc

    def lookup_parcel(self, latitude: float, longitude: float, *, situs_address: str = "", apn: str = "") -> dict[str, Any]:
        """Look up (retrieving/refreshing as needed) the parcel record at a coordinate.

        Args:
            latitude: WGS-84 latitude.
            longitude: WGS-84 longitude.
            situs_address: Already-known street address, passed through to
                REData as an additional search key.
            apn: Already-known parcel/APN, passed through the same way.

        Returns:
            The record payload dict - REData's own ``PropertyRecord.to_dict()`` shape (owner/tax/sale/assessment fields, ``source``, ``confidence``, ``field_sources``/``field_mismatches``, ...). A partial record names the tiers REData could not hear from under ``unanswered_sources`` (:data:`UNANSWERED_SOURCES_KEY`), so the cache keeps it only briefly and a boundary can tell the line is not yet drawn.

        Raises:
            PropertyRecordsUnavailableError: No record is available (see the exception's own docstring for how to distinguish a permanent "nothing here" from a transient outage via ``reason``).
        """
        from urbanlens.dashboard.models.cache.location_cache import UNANSWERED_SOURCES_KEY

        body = self._lookup_parcel_body(latitude, longitude, situs_address=situs_address, apn=apn)
        payload = dict(body.get("record_payload") or {})
        # Taken from the top-level ``complete``/``sources`` when REData answers them, so this does not depend on the key
        # REData happens to use inside its own record_payload; a body with neither is left as it came.
        unanswered = parcel_unanswered_sources(body)
        if unanswered is not None and (unanswered or UNANSWERED_SOURCES_KEY in payload):
            payload[UNANSWERED_SOURCES_KEY] = unanswered
        # The Parcel publishes parcel_geometry as GeoJSON beside record_payload, whose own copy is the tier's
        # Esri-ring snapshot. A county building footprint reaches here only inside record_payload, still in Esri
        # rings - read it with services.apis.locations.base.polygon_from_wire, which takes either shape.
        if "parcel_geometry" in body:
            payload["parcel_geometry"] = body["parcel_geometry"]
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
        """Return neighbourhood demographics for the census tract containing a parcel, shared by every location on it.

        An answer is shared as long as a parcel lookup is (briefly when partial or coarser, see
        :func:`_demographics_share_seconds`); a failure is remembered briefly, or for as long as REData asked, and raised
        again alike.

        Args:
            parcel_uuid: The parcel's REData uuid (see :meth:`lookup_parcel_uuid`).

        Returns:
            The demographics dict, or None when the parcel has no known coordinate or its coordinate is outside the USA.

        Raises:
            PropertyRecordsUnavailableError: The request to REData failed, or REData 503s the whole endpoint (e.g. ``RD_US_CENSUS_API_KEY`` not configured server-side, or the Census API is rate-limited).
        """
        unanswered_key = f"redata:parcel-demographics-unanswered:{parcel_uuid}"
        unanswered = get_or_none(unanswered_key, label="parcel demographics failure", alias=DEFAULT_CACHE_ALIAS)
        if isinstance(unanswered, dict):
            raise _remembered_failure(unanswered)
        try:
            body = coalesced(f"redata:parcel-demographics:{parcel_uuid}", lambda: self._get_json(f"/api/v1/parcels/{parcel_uuid}/demographics/") or {}, ttl=_demographics_share_seconds)
        except PropertyRecordsUnavailableError as exc:
            # Every location on a parcel asks for the parcel's demographics; the next one learns this answer from here.
            named = exc.retry_after if isinstance(exc, PropertyRecordsBusyError) else 0
            wait = max(_PARTIAL_PARCEL_LOOKUP_SHARE_SECONDS, min(named, UPSTREAM_BUSY_MAX_SECONDS))
            set_or_skip(unanswered_key, _failure_memo(exc), wait, label="parcel demographics failure", alias=DEFAULT_CACHE_ALIAS)
            raise
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

    def lookup_land_use_areas(self, parcel_uuid: str) -> dict[str, dict[str, Any]]:
        """Return the boundary of each Census Special Land Use Area a parcel's coordinate falls inside.

        Args:
            parcel_uuid: The parcel's REData uuid (see :meth:`lookup_parcel_uuid`).

        Returns:
            ``{"<category>": {"name", "geoid", "geometry"}, ...}`` - only the categories the parcel is inside, usually
            none. ``geometry`` is GeoJSON, or None when REData has no boundary for the area.

        Raises:
            PropertyRecordsUnavailableError: The request to REData failed. A REData that predates the endpoint, or does
                not know the parcel, answers 404 (``status_code``); one whose share of the live lookup is spent answers a
                503 with ``Retry-After``.
        """
        body = self._get_json(f"/api/v1/parcels/{parcel_uuid}/land-use-areas/")
        if not isinstance(body, dict):
            return {}
        return {str(category): dict(area) for category, area in body.items() if isinstance(area, dict)}

    def lookup_assessments(self, parcel_uuid: str) -> LocationContextEnvelope:
        """Return annual assessor valuations near a parcel.

        Args:
            parcel_uuid: The parcel's REData uuid (see :meth:`lookup_parcel_uuid`).

        Returns:
            The raw assessment rows (none outside covered counties), and whether every provider covering the parcel
            answered: an incomplete answer's rows are a floor.

        Raises:
            PropertyRecordsUnavailableError: The request to REData failed, or no provider covering the parcel answered.
        """
        return _provider_results(self._get_json(f"/api/v1/parcels/{parcel_uuid}/assessments/"))

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
        return self._get_pages(f"/api/v1/parcels/{parcel_uuid}/liens/")

    def lookup_owners(self, parcel_uuid: str) -> list[dict[str, Any]]:
        """Return every owner REData has linked to a parcel, former ones included.

        Args:
            parcel_uuid: The parcel's REData uuid (see :meth:`lookup_parcel_uuid`).

        Returns:
            The raw owner rows. ``current`` says whether the parcel's latest record still names
            the owner, and ``parcels`` lists every parcel the owner is linked to, this one included.

        Raises:
            PropertyRecordsUnavailableError: The request to REData failed.
        """
        return self._get_pages(f"/api/v1/parcels/{parcel_uuid}/owners/")

    def lookup_sales(self, parcel_uuid: str) -> list[dict[str, Any]]:
        """Return the sales REData has recorded against a parcel across every retrieval, newest first.

        Args:
            parcel_uuid: The parcel's REData uuid (see :meth:`lookup_parcel_uuid`).

        Returns:
            The raw sale rows.

        Raises:
            PropertyRecordsUnavailableError: The request to REData failed.
        """
        return self._get_pages(f"/api/v1/parcels/{parcel_uuid}/sales/")

    def lookup_tax_payments(self, parcel_uuid: str) -> list[dict[str, Any]]:
        """``delinquent`` is the publisher's own determination rather than something derived from ``paid`` - a row can be unpaid but not yet delinquent, since bills are unpaid before their due date.

        Args:
            parcel_uuid: The parcel's REData uuid (see :meth:`lookup_parcel_uuid`).

        Returns:
            The raw payment rows, newest tax year first; empty outside covered counties.

        Raises:
            PropertyRecordsUnavailableError: The request to REData failed.
        """
        return self._get_pages(f"/api/v1/parcels/{parcel_uuid}/tax-payments/")

    def lookup_sale_records(self, parcel_uuid: str) -> LocationContextEnvelope:
        """Return supplementary recorded sales near a parcel.
        Rows are **near-parcel** - ``parcel`` is null and nothing links a row to a specific parcel - so callers must match by address (or a raw PIN in ``attributes``) before attributing a sale to a property.

        Args:
            parcel_uuid: The parcel's REData uuid (see :meth:`lookup_parcel_uuid`).

        Returns:
            The raw sale rows (none outside covered areas), and whether every provider covering the parcel answered:
            an incomplete answer's rows are a floor.

        Raises:
            PropertyRecordsUnavailableError: The request to REData failed, or no provider covering the parcel answered.
        """
        return _provider_results(self._get_json(f"/api/v1/parcels/{parcel_uuid}/sale-records/"))

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
        return self.lookup_parcel_buildings(parcel_uuid).buildings

    def lookup_parcel_buildings(self, parcel_uuid: str) -> ParcelBuildings:
        """Every building REData can find for a parcel, and which of its sources did not answer.

        Args:
            parcel_uuid: The parcel's REData uuid.

        Returns:
            The buildings, as :meth:`lookup_buildings` returns them, with the sources REData named as unanswered.

        Raises:
            PropertyRecordsUnavailableError: The request to REData failed.
        """
        body, headers = self._get_json_and_headers(f"/api/v1/parcels/{parcel_uuid}/buildings/")
        return ParcelBuildings(list(body) if isinstance(body, list) else [], _unanswered_header(headers))

    def resolve_building_ref(self, ref: str) -> dict[str, Any]:
        """Map a building ref REData once served to its stable ``overture:<gers_id>``.

        Transitional, as REData's endpoint is (its P98): used only to move places and floorplans off Overture's
        legacy content-hash refs, and removed with them. A resolved answer is kept for a day, since a served hash
        names one footprint for good; any other answer for an hour.

        Args:
            ref: The ref to resolve.

        Returns:
            REData's answer: ``ref``, ``status`` (``resolved``, ``ambiguous`` or ``unknown``), ``stable_ref`` and ``candidates``.

        Raises:
            PropertyRecordsUnavailableError: The request to REData failed, or it does not serve the endpoint yet.
        """
        key = f"redata:building-ref:{hashlib.sha256(ref.encode()).hexdigest()}"
        kept = get_or_none(key, label="building ref resolution", alias=DEFAULT_CACHE_ALIAS)
        if isinstance(kept, dict):
            return kept
        body = self._get_json("/api/v1/buildings/resolve/", params={"ref": ref})
        if not isinstance(body, dict) or not isinstance(body.get("status"), str):
            raise PropertyRecordsUnavailableError(REASON_SOURCE_ERROR, "REData returned an unreadable building ref resolution.")
        set_or_skip(key, body, _RESOLVED_REF_SECONDS if body["status"] == "resolved" else _UNRESOLVED_REF_SECONDS, label="building ref resolution", alias=DEFAULT_CACHE_ALIAS)
        return body

    def lookup_boundaries(self, parcel_uuid: str) -> ParcelBoundaries:
        """Return every boundary candidate REData can find for a parcel, scored, and which of its sources did not answer.

        Args:
            parcel_uuid: The parcel's REData uuid.

        Returns:
            Candidate dicts (possibly empty), each with ``geometry`` as standard GeoJSON plus ``kind`` (``"parcel"`` for the parcel's own cadastral line, ``"area"`` for something merely related to it), ``confidence``, ``is_suggested`` and ``confidence_breakdown``; and the sources REData named as unanswered (0.3.7 names them on this endpoint too).

        Raises:
            PropertyRecordsUnavailableError: The request to REData failed.
        """
        body, headers = self._get_json_and_headers(f"/api/v1/parcels/{parcel_uuid}/boundaries/")
        return ParcelBoundaries(list(body) if isinstance(body, list) else [], _unanswered_header(headers))

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
            PropertyRecordsUnavailableError: The request to REData failed, or no register covering the point answered.
        """
        params: dict[str, Any] = {"lat": latitude, "lng": longitude, "radius_meters": radius_meters}
        if provider:
            params["provider"] = provider
        body = self._get_json("/api/v1/cultural-resources/lookup/", params=params)
        if isinstance(body, list):
            return list(body)
        if not isinstance(body, dict):
            return []
        results = body.get("results")
        rows = list(results) if isinstance(results, list) else []
        if not rows and body.get("complete") is False:
            raise PropertyRecordsUnavailableError(REASON_ALL_PROVIDERS_UNAVAILABLE, "A register covering the point did not answer, and none of the rest found anything.", retry_later=True)
        return rows

    def fetch_cultural_resource_detail(self, resource_uuid: str) -> dict[str, Any]:
        """Fetch a CRIS resource's full detail record and attachments, shared by every panel asking about it.

        Every building on a campus matches the campus's own CRIS record, so each building's panel asks for it.

        Args:
            resource_uuid: The resource's REData uuid (from :meth:`lookup_cultural_resources`).

        Returns:
            See :meth:`_fetch_cultural_resource_detail_now`.

        Raises:
            PropertyRecordsUnavailableError: See :meth:`_fetch_cultural_resource_detail_now`.
        """
        return coalesced(f"redata:cris-detail:{resource_uuid}", lambda: self._fetch_cultural_resource_detail_now(resource_uuid), ttl=_CULTURAL_RESOURCE_SHARE_SECONDS)

    def _fetch_cultural_resource_detail_now(self, resource_uuid: str) -> dict[str, Any]:
        """Fetch (and cache onto the resource) a CRIS resource's full detail record and attachments.
        REData answers with an envelope - ``{"detail_status": ..., "resource": {...}}`` - because "the source was asked and genuinely publishes nothing deeper" and "detail was retrieved" both leave a resource whose ``detail_retrieved_at`` is set.

        Args:
            resource_uuid: The resource's REData uuid (from :meth:`lookup_cultural_resources`).

        A resource its source cannot resolve is held by REData, which answers 200 with ``detail_status`` ``unresolved`` and the resource without its detail. That is raised, not returned: returned, it reads as a fetched record, and :meth:`fetch_cultural_resource_detail` would share it.

        Returns:
            The resource dict, now with ``detail_payload``/``detail_retrieved_at`` and ``attachments`` populated.

        Raises:
            PropertyRecordsBusyError: REData holds the resource (``REASON_DETAIL_UNRESOLVED``, a transient reason), carrying the wait REData named, bounded as any busy wait is.
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
            if body.get("detail_status") == _DETAIL_UNRESOLVED:
                raise PropertyRecordsBusyError(
                    REASON_DETAIL_UNRESOLVED,
                    str(body.get("message") or "REData could not resolve this resource to a record yet."),
                    retry_after=_hold_wait(body.get("retry_after")),
                )
            resource = body.get("resource")
            return dict(resource) if isinstance(resource, dict) else body
        if response.status_code == 400:
            try:
                body = response.json()
            except ValueError:
                body = {}
            raise PropertyRecordsUnavailableError(body.get("error") or REASON_SOURCE_ERROR, body.get("message", ""))
        logger.warning("REData cultural-resource detail fetch failed (%s): %s", response.status_code, response.text[:500])
        raise _download_failure(response)

    def queue_cultural_resource_details(self, latitude: float, longitude: float, *, radius_meters: float) -> dict[str, Any]:
        """Ask REData to fetch the detail record of every resource near a coordinate, in the background.
        REData paces the fetches against each provider's own rate budget, so a site with 100+ resources is warmed without one caller spending that budget inline; later lookups then carry each resource's ``attachments``.

        Args:
            latitude: WGS-84 latitude.
            longitude: WGS-84 longitude.
            radius_meters: Search radius around the coordinate, as for :meth:`lookup_cultural_resources`.

        Returns:
            REData's counts - ``queued``/``already_fetched``/``unsupported``/``held``/``considered``.

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
        if response.status_code in RedataBreaker.REFUSED_STATUSES:
            raise _refused(response)
        logger.warning("REData bulk cultural-resource detail queue failed (%s): %s", response.status_code, response.text[:500])
        raise _download_failure(response)

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
        """OCR/AI-extract a document attachment, shared by every panel asking about it.

        Args:
            resource_uuid: The resource's REData uuid.
            attachment_id: The attachment's id.
            timeout: Seconds to wait for REData's answer to one extraction. A caller finding the extraction in flight
                waits out two of them and a download, which is what an undownloaded document costs.

        Returns:
            See :meth:`_extract_cultural_resource_attachment_now`.

        Raises:
            PropertyRecordsUnavailableError: See :meth:`_extract_cultural_resource_attachment_now`.
        """
        refused_key = f"redata:cris-extract-refused:{resource_uuid}:{attachment_id}"
        refused = get_or_none(refused_key, label="CRIS extraction refusal", alias=DEFAULT_CACHE_ALIAS)
        if isinstance(refused, dict):
            raise PropertyRecordsUnavailableError(str(refused.get("reason") or REASON_SOURCE_ERROR), str(refused.get("message") or ""))
        try:
            return coalesced(
                f"redata:cris-extract:{resource_uuid}:{attachment_id}",
                lambda: self._extract_cultural_resource_attachment_now(resource_uuid, attachment_id, timeout=timeout),
                ttl=_CULTURAL_RESOURCE_SHARE_SECONDS,
                wait_seconds=2 * timeout + _REQUEST_TIMEOUT,
            )
        except PropertyRecordsUnavailableError as exc:
            # Every ask runs OCR and an AI model again; dev asked one empty form twelve times.
            if exc.reason in _SETTLED_EXTRACTION_REFUSALS:
                set_or_skip(refused_key, {"reason": exc.reason, "message": str(exc)}, _EXTRACTION_REFUSAL_SECONDS, label="CRIS extraction refusal", alias=DEFAULT_CACHE_ALIAS)
            raise

    def _extract_cultural_resource_attachment_now(self, resource_uuid: str, attachment_id: int, *, timeout: float = _REQUEST_TIMEOUT) -> dict[str, Any]:
        """OCR/AI-extract a ``document``-kind attachment's fields and any embedded photos.

        REData extracts only a document whose file it holds, and fetches the file from CRIS only when it is downloaded,
        so a refusal is answered by downloading it and asking once more.

        Args:
            resource_uuid: The resource's REData uuid.
            attachment_id: The attachment's id within that resource.
            timeout: Seconds to wait; REData extracts synchronously, which can take longer than a lookup.

        Returns:
            The attachment dict with ``extracted_data``/``extracted_at``/ ``extracted_images`` populated - see REData's own ``../REData/docs/api-reference.md`` for the shape.

        Raises:
            PropertyRecordsUnavailableError: The attachment isn't an extractable document (``"not_extractable"``), its file
                could not be downloaded, neither the text nor image extraction found anything at all
                (``"extraction_unavailable"``), or the request to REData failed outright.
        """
        try:
            return self._post_extraction(resource_uuid, attachment_id, timeout=timeout)
        except PropertyRecordsUnavailableError as refused:
            if refused.reason != REASON_NOT_EXTRACTABLE:
                raise
        self.download_cultural_resource_attachment(resource_uuid, attachment_id)
        return self._post_extraction(resource_uuid, attachment_id, timeout=timeout)

    def _post_extraction(self, resource_uuid: str, attachment_id: int, *, timeout: float) -> dict[str, Any]:
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
        raise _download_failure(response)

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
