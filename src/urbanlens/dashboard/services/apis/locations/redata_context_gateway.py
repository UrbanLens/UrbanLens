"""Shared REST client for REData's "near-a-coordinate" location-context endpoints."""

from __future__ import annotations

from collections import Counter
from contextlib import contextmanager
from dataclasses import dataclass, field
import logging
from typing import TYPE_CHECKING, Any, ClassVar, NoReturn

from urbanlens.dashboard.services.core.gateway import Gateway, GatewayRequestError, UpstreamBusyError, read_capped, upstream_retry_after
from urbanlens.dashboard.services.core.input_validation import ImpossibleInputError, require_coordinates, require_in_range
from urbanlens.dashboard.services.core.upstream_breaker import RedataBreaker
from urbanlens.UrbanLens.settings.app import settings

if TYPE_CHECKING:
    from collections.abc import Iterator

    import requests

logger = logging.getLogger(__name__)

_REQUEST_TIMEOUT = 30

#: Every source covering the coordinate failed to answer - back off, retrying
#: now will fail identically.
REASON_RATE_LIMITED = "rate_limited"
#: A mix of outages (or a single one) among the sources covering the
#: coordinate - unlike ``REASON_RATE_LIMITED``, waiting isn't guaranteed to help.
REASON_ALL_PROVIDERS_UNAVAILABLE = "all_providers_unavailable"
#: Anything REData didn't shape as one of its own structured errors (a
#: network failure, a malformed response, an unexpected status code, or a
#: REData-side outage not shaped like its own error responses).
REASON_SOURCE_ERROR = "source_error"
#: REData refused the key for the endpoint (401/403): a scope it lacks, not anything about the place asked about.
REASON_FORBIDDEN = "forbidden"
#: A provider status in REData's envelope meaning the provider was not heard from, so the answer is a floor.
_UNANSWERED_STATUSES = frozenset({"unavailable", "rate_limited", "not_cached"})
#: The largest near-point ``limit`` REData accepts; it answers 400 for more.
MAX_NEAR_POINT_LIMIT = 200


class LocationContextUnavailableError(GatewayRequestError):
    """Raised when a REData location-context request fails or answers with a blackout.

    Attributes:
        reason: One of the module's ``REASON_*`` constants, or REData's own ``error`` code verbatim for a ``400`` (e.g. ``"invalid_coordinates"``, ``"unknown_provider"``) - REData's fixed reason taxonomy for these endpoints (see the module docstring).
        rejected: REData refused the request itself (a ``400``), which asking again will not change."""

    def __init__(self, reason: str, message: str, *, rejected: bool = False) -> None:
        self.reason = reason
        self.rejected = rejected
        super().__init__(message)

    @property
    def is_outage(self) -> bool:
        """Only a rejected request is an answer; every other failure left the question unasked."""
        return not self.rejected


class LocationContextRefusedError(ImpossibleInputError, LocationContextUnavailableError):
    """An input REData could only answer 400 for, refused before asking: the same settled answer as that 400.

    Both an :class:`ImpossibleInputError`, for callers that know refusals, and a rejected
    :class:`LocationContextUnavailableError`, for the callers that already handle REData's own 400.
    """

    def __init__(self, refusal: ImpossibleInputError) -> None:
        ImpossibleInputError.__init__(self, refusal.service, refusal.reason, refusal.detail)
        self.rejected = True


@contextmanager
def refused_as_rejection() -> Iterator[None]:
    """Re-raise an :class:`ImpossibleInputError` from the checks inside as a :class:`LocationContextRefusedError`.

    Raises:
        LocationContextRefusedError: A check inside refused its input.
    """
    try:
        yield
    except LocationContextRefusedError:
        raise
    except ImpossibleInputError as exc:
        raise LocationContextRefusedError(exc) from None


class LocationContextBusyError(LocationContextUnavailableError, UpstreamBusyError):
    """REData throttled or refused this key, so the request was refused or not made; a caller may retry after ``retry_after`` seconds."""

    def __init__(self, message: str, *, retry_after: int, reason: str = REASON_RATE_LIMITED) -> None:
        super().__init__(reason, message)
        self.retry_after = retry_after


def cap_per_provider(rows: list[dict[str, Any]], limit: int) -> list[dict[str, Any]]:
    """The first ``limit`` rows of each provider, in their original order, as REData applies ``limit``.

    Args:
        rows: Near-point results, each tagged with its ``provider`` (rows without one share a group).
        limit: Most rows to keep per provider.

    Returns:
        The kept rows.
    """
    seen: Counter[Any] = Counter()
    kept = []
    for row in rows:
        provider = row.get("provider")
        if seen[provider] < limit:
            seen[provider] += 1
            kept.append(row)
    return kept


def redata_configured() -> bool:
    """Whether REData is configured for this install (``UL_REDATA_API_URL``/``UL_REDATA_API_KEY``).
    Always ``False`` under :attr:`~services.sandbox.guard.ProcessRole.AI`: the assistant's tool loop must never reach REData, regardless of whether ``ai-worker``'s environment happens to carry the credentials (it shouldn't, per the compose anchors)."""
    from urbanlens.dashboard.services.sandbox.guard import ProcessRole, current_role

    if current_role() is ProcessRole.AI:
        return False
    return bool(settings.redata_api_url and settings.redata_api_key)


@dataclass(slots=True, frozen=True)
class LocationContextEnvelope:
    """A parsed near-a-coordinate REData response.

    Attributes:
        count: Number of entries in ``results``.
        complete: False when any source covering the coordinate failed to answer (``unavailable``/``rate_limited`` in ``providers``) - the results are a floor, not a total.
        results: The provider-tagged result dicts - shape is endpoint-specific, see each domain gateway's own accessor.
        providers: Per-provider status entries (``provider``, ``status``, ``count``, ``message``, ``radius_meters``) - empty for the few endpoints with no provider registry behind them."""

    count: int
    complete: bool
    results: list[dict[str, Any]] = field(default_factory=list)
    providers: list[dict[str, Any]] = field(default_factory=list)

    @property
    def unanswered_sources(self) -> list[str]:
        """The providers that did not answer, when the answer is incomplete; ``[]`` for a complete one.

        Returns:
            Provider names, or ``["unknown"]`` for an incomplete answer that names none.
        """
        if self.complete:
            return []
        named = [str(entry.get("provider")) for entry in self.providers if entry.get("status") in _UNANSWERED_STATUSES and entry.get("provider")]
        return named or ["unknown"]

    def marked(self, payload: dict[str, Any]) -> dict[str, Any]:
        """``payload``, marked partial when this answer is, so the cache keeps it only briefly.

        Args:
            payload: What a caller is about to cache from this answer.

        Returns:
            The payload, with ``unanswered_sources`` added for an incomplete answer.
        """
        from urbanlens.dashboard.models.cache.location_cache import UNANSWERED_SOURCES_KEY

        unanswered = self.unanswered_sources
        return {**payload, UNANSWERED_SOURCES_KEY: unanswered} if unanswered else payload


@dataclass(slots=True, kw_only=True)
class RedataLocationContextGateway(Gateway):
    """Base REST client for REData's near-a-coordinate location-context endpoints."""

    #: Whether this endpoint's sources hold anything at exactly ``(0, 0)``: a weather or elevation grid does; a
    #: register of buildings does not, so a missing coordinate that became zeros is refused before asking.
    answers_at_null_island: ClassVar[bool] = False

    # default_factory so settings changes apply per instance; a bare default freezes at import.
    base_url: str | None = field(default_factory=lambda: settings.redata_api_url)
    api_key: str | None = field(default_factory=lambda: settings.redata_api_key)

    def __post_init__(self) -> None:
        Gateway.__post_init__(self)
        if not self.base_url:
            raise ValueError("UL_REDATA_API_URL must be configured.")
        if not self.base_url.startswith(("http://", "https://")):
            object.__setattr__(self, "base_url", f"https://{self.base_url}")
        if not self.api_key:
            raise ValueError("UL_REDATA_API_KEY must be configured.")

    @property
    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.api_key}", "Accept": "application/json"}

    def near_point(
        self,
        path: str,
        latitude: float,
        longitude: float,
        *,
        radius_meters: float | None = None,
        provider: str | list[str] | None = None,
        force_refresh: bool = False,
        limit: int | None = None,
        extra_params: dict[str, Any] | None = None,
    ) -> LocationContextEnvelope:
        """GET a near-a-coordinate REData endpoint and parse its envelope.

        Args:
            path: Path relative to ``base_url`` (leading slash optional), e.g.
                ``"/api/v1/hazards/"``.
            latitude: WGS-84 latitude.
            longitude: WGS-84 longitude.
            radius_meters: Search radius in meters. Omit to let REData's
                source(s) use their own natural default.
            provider: Restrict which source(s) actually run - a single tag or
                a repeatable list, per REData's ``?provider=`` semantics.
            force_refresh: Bypass REData's cache and re-query live.
            limit: Most rows to keep, at most :data:`MAX_NEAR_POINT_LIMIT`.
                Sent, and applied here as well, so a REData that ignores it
                cannot fill a cache row with every row in the radius.
            extra_params: Any endpoint-specific query params beyond the shared
                set above (e.g. hazards' ``min_magnitude``/``years``).

        Returns:
            The parsed :class:`LocationContextEnvelope`.

        Raises:
            LocationContextUnavailableError: A total blackout (every source covering the coordinate failed), an empty answer with a source that did not answer, a REData-side validation error, or the request itself failed outright.
            LocationContextRefusedError: The point, radius or limit could never be answered, so nothing was sent.
        """
        service = type(self).service_key or "redata"
        with refused_as_rejection():
            require_coordinates(service, latitude, longitude, allow_null_island=type(self).answers_at_null_island)
            if radius_meters is not None:
                require_in_range(service, "radius_meters", radius_meters, minimum=0, exclusive_minimum=True)
            if limit is not None:
                require_in_range(service, "limit", limit, minimum=1)
        params: dict[str, Any] = {"lat": latitude, "lng": longitude}
        if radius_meters is not None:
            params["radius_meters"] = radius_meters
        if provider is not None:
            params["provider"] = provider
        if force_refresh:
            params["force_refresh"] = "true"
        if limit is not None:
            limit = min(limit, MAX_NEAR_POINT_LIMIT)
            params["limit"] = limit
        if extra_params:
            params.update(extra_params)
        envelope = self._get_envelope(path, params)
        if limit is None or len(envelope.results) <= limit:
            return envelope
        kept = cap_per_provider(envelope.results, limit)
        return LocationContextEnvelope(count=len(kept), complete=envelope.complete, results=kept, providers=envelope.providers)

    def get_json(self, path: str, params: dict[str, Any] | None = None) -> Any:
        """GET any REData endpoint outside the near-a-coordinate envelope and return its raw JSON body.
        Use :meth:`near_point` instead for anything shaped like REData's near-a-coordinate contract.

        Args:
            path: Path relative to ``base_url`` (leading slash optional).
            params: Query-string parameters, if any.

        Returns:
            The raw decoded JSON body (whatever type - object or array - the endpoint actually returns).

        Raises:
            LocationContextUnavailableError: A REData-side validation error, or the request itself failed outright.
        """
        response = self._request(path, params or {})
        if response.status_code == 200:
            try:
                return response.json()
            except ValueError as exc:
                raise LocationContextUnavailableError(REASON_SOURCE_ERROR, "REData returned an unparseable response.") from exc
        return self._raise_for_error_status(response, path)

    def post_json(self, path: str, json_body: dict[str, Any]) -> Any:
        """POST a JSON body to a REData endpoint and return its raw JSON response.
        ``POST /routes/``) that share this gateway's auth/error handling but take a body rather than query params.

        Args:
            path: Path relative to ``base_url`` (leading slash optional).
            json_body: The JSON-serializable request body.

        Returns:
            The raw decoded JSON body.

        Raises:
            LocationContextUnavailableError: A REData-side validation error, or the request itself failed outright.
        """
        base_url = self.base_url
        if base_url is None:
            raise LocationContextUnavailableError(REASON_SOURCE_ERROR, "UL_REDATA_API_URL is not configured.")
        try:
            response = self.session.post(f"{base_url.rstrip('/')}/{path.lstrip('/')}", json=json_body, headers=self._headers, timeout=_REQUEST_TIMEOUT)
        except UpstreamBusyError as exc:
            raise LocationContextBusyError(str(exc), retry_after=exc.retry_after) from exc
        except OSError as exc:
            # str(exc) on a requests/urllib3 connection error routinely embeds the full request URL,
            # including the lat/lng (or address) query params callers pass in - which would undo
            # every redact_coordinate()/redact_text() call a caller wraps its *own* logging in.
            # The exception type is enough for an operator to diagnose a network failure without
            raise LocationContextUnavailableError(REASON_SOURCE_ERROR, f"Could not reach REData: {type(exc).__name__}") from exc

        if response.status_code == 200:
            try:
                return response.json()
            except ValueError as exc:
                raise LocationContextUnavailableError(REASON_SOURCE_ERROR, "REData returned an unparseable response.") from exc
        return self._raise_for_error_status(response, path)

    def get_bytes(self, path: str, *, what: str) -> tuple[bytes, str]:
        """GET one of REData's file endpoints (a mirrored image, an archived capture).

        Args:
            path: Path relative to ``base_url`` (leading slash optional).
            what: What is being downloaded, for the size-cap refusal.

        Returns:
            ``(bytes, content type)``.

        Raises:
            LocationContextUnavailableError: REData holds no such file (a ``404``, marked ``rejected``), the body was
                over the proxied-media cap, or the request failed.
        """
        base_url = self.base_url
        if base_url is None:
            raise LocationContextUnavailableError(REASON_SOURCE_ERROR, "UL_REDATA_API_URL is not configured.")
        try:
            response = self.session.get(f"{base_url.rstrip('/')}/{path.lstrip('/')}", headers=self._headers, timeout=_REQUEST_TIMEOUT, stream=True)
        except UpstreamBusyError as exc:
            raise LocationContextBusyError(str(exc), retry_after=exc.retry_after) from exc
        except OSError as exc:
            # See _request: the exception text would carry the URL.
            raise LocationContextUnavailableError(REASON_SOURCE_ERROR, f"Could not reach REData: {type(exc).__name__}") from exc
        if response.status_code == 200:
            try:
                content = read_capped(response, what=what)
            except GatewayRequestError as exc:
                raise LocationContextUnavailableError(REASON_SOURCE_ERROR, str(exc)) from exc
            return content, response.headers.get("Content-Type", "application/octet-stream")
        if response.status_code == 404:
            try:
                body = response.json()
            except ValueError:
                body = {}
            reason = body.get("error") if isinstance(body, dict) else None
            raise LocationContextUnavailableError(str(reason or "not_found"), str(body.get("message", "")) if isinstance(body, dict) else "", rejected=True)
        return self._raise_for_error_status(response, path)

    def _get_envelope(self, path: str, params: dict[str, Any]) -> LocationContextEnvelope:
        response = self._request(path, params)
        if response.status_code == 200:
            try:
                body = response.json()
            except ValueError as exc:
                raise LocationContextUnavailableError(REASON_SOURCE_ERROR, "REData returned an unparseable response.") from exc
            if not isinstance(body, dict):
                raise LocationContextUnavailableError(REASON_SOURCE_ERROR, "REData returned an unexpected response shape.")
            envelope = LocationContextEnvelope(
                count=int(body.get("count") or 0),
                complete=bool(body.get("complete", True)),
                results=list(body.get("results") or []),
                providers=list(body.get("providers") or []),
            )
            if not envelope.complete and not envelope.results:
                # Nothing found, and a source that might have found something did not answer: not an answer.
                raise LocationContextUnavailableError(REASON_ALL_PROVIDERS_UNAVAILABLE, "A source covering the request failed to answer, and none of the rest found anything.")
            return envelope
        return self._raise_for_error_status(response, path)

    def _request(self, path: str, params: dict[str, Any]) -> requests.Response:
        base_url = self.base_url
        if base_url is None:
            # __post_init__ already validates this for the normal construction path;
            # this only guards a hypothetical bypass (e.g. object.__new__) and narrows
            # the type for mypy without resorting to assert (banned outside tests).
            raise LocationContextUnavailableError(REASON_SOURCE_ERROR, "UL_REDATA_API_URL is not configured.")
        try:
            return self.session.get(f"{base_url.rstrip('/')}/{path.lstrip('/')}", params=params, headers=self._headers, timeout=_REQUEST_TIMEOUT)
        except UpstreamBusyError as exc:
            raise LocationContextBusyError(str(exc), retry_after=exc.retry_after) from exc
        except OSError as exc:
            # str(exc) on a requests/urllib3 connection error routinely embeds the full request URL,
            # including the lat/lng (or address) query params callers pass in - which would undo
            # every redact_coordinate()/redact_text() call a caller wraps its *own* logging in.
            # The exception type is enough for an operator to diagnose a network failure without
            raise LocationContextUnavailableError(REASON_SOURCE_ERROR, f"Could not reach REData: {type(exc).__name__}") from exc

    def _raise_for_error_status(self, response: requests.Response, path: str) -> NoReturn:
        """Translate a non-200 REData response into a :class:`LocationContextUnavailableError`.
        Always raises - callers reach this only once ``response.status_code != 200``."""
        if response.status_code in (400, 503):
            try:
                body = response.json()
            except ValueError:
                body = {}
            reason = body.get("error") or REASON_SOURCE_ERROR
            raise LocationContextUnavailableError(reason, body.get("message", ""), rejected=response.status_code == 400)

        if response.status_code in RedataBreaker.REFUSED_STATUSES:
            # Reported once by the breaker, which holds the endpoint off for as long as this asks the caller to wait.
            raise LocationContextBusyError(f"REData refused this key at {path} ({response.status_code}); it may lack the endpoint's scope.", retry_after=RedataBreaker.REFUSED_SECONDS, reason=REASON_FORBIDDEN)
        logger.warning("REData request to %s failed (%s): %s", path, response.status_code, response.text[:500])
        if response.status_code == 429:
            raise LocationContextBusyError("REData throttled this key.", retry_after=upstream_retry_after(response) or 1)
        raise LocationContextUnavailableError(REASON_SOURCE_ERROR, f"REData request failed with status {response.status_code}.")
