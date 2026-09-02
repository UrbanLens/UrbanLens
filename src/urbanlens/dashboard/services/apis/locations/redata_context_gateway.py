"""Shared REST client for REData's "near-a-coordinate" location-context endpoints.

REData's geocode, elevation, weather, imagery, natural-hazards,
nature-observations, reference-documents, points-of-interest, and media
families - plus ``/parks/nearby/`` - all share one request/response contract
(``../REData/docs/api-reference.md``, "Near-a-coordinate endpoints"): the same
``lat``/``lng``/``radius_meters``/``provider``/``force_refresh`` query
parameters, and the same ``{"count", "complete", "results", "providers"}``
response envelope. This module implements that shared contract exactly once;
a domain-specific gateway (``RedataElevationGateway``, ``RedataWeatherGateway``,
...) subclasses it and adds only its own path, params, and typed accessors.

Mirrors ``services.apis.property_records.redata_gateway.RedataGateway``'s
auth/error-handling shape for the rest of REData's API surface, which predates
this shared envelope and so implements its own per-endpoint parsing instead.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import logging
from typing import TYPE_CHECKING, Any, NoReturn

from urbanlens.dashboard.services.core.gateway import Gateway, GatewayRequestError
from urbanlens.UrbanLens.settings.app import settings

if TYPE_CHECKING:
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


class LocationContextUnavailableError(GatewayRequestError):
    """Raised when a REData location-context request fails or answers with a blackout.

    Attributes:
        reason: One of the module's ``REASON_*`` constants, or REData's own
            ``error`` code verbatim for a ``400`` (e.g. ``"invalid_coordinates"``,
            ``"unknown_provider"``) - REData's fixed reason taxonomy for these
            endpoints (see the module docstring).
    """

    def __init__(self, reason: str, message: str) -> None:
        self.reason = reason
        super().__init__(message)


def redata_configured() -> bool:
    """Whether REData is configured for this install (``UL_REDATA_API_URL``/``UL_REDATA_API_KEY``).

    Shared by every location-context resolution module (weather, routing,
    geocode, imagery, ...) so "is REData available" is decided identically
    everywhere, matching ``places_resolution.py``/``cid_resolution.py``'s
    existing precedent for the same check.

    Always ``False`` under :attr:`~services.sandbox.guard.ProcessRole.AI`: the
    assistant's tool loop must never reach REData, regardless of whether
    ``ai-worker``'s environment happens to carry the credentials (it
    shouldn't, per the compose anchors). This is defense in depth, not the
    boundary - the real boundary is the network: ``ai-worker`` sits on
    ``ai_network``/``inference_network``, neither of which routes to REData.
    """
    from urbanlens.dashboard.services.sandbox.guard import ProcessRole, current_role

    if current_role() is ProcessRole.AI:
        return False
    return bool(settings.redata_api_url and settings.redata_api_key)


@dataclass(slots=True, frozen=True)
class LocationContextEnvelope:
    """A parsed near-a-coordinate REData response.

    Attributes:
        count: Number of entries in ``results``.
        complete: False when any source covering the coordinate failed to
            answer (``unavailable``/``rate_limited`` in ``providers``) - the
            results are a floor, not a total.
        results: The provider-tagged result dicts - shape is endpoint-specific,
            see each domain gateway's own accessor.
        providers: Per-provider status entries (``provider``, ``status``,
            ``count``, ``message``, ``radius_meters``) - empty for the few
            endpoints with no provider registry behind them.
    """

    count: int
    complete: bool
    results: list[dict[str, Any]] = field(default_factory=list)
    providers: list[dict[str, Any]] = field(default_factory=list)


@dataclass(slots=True, kw_only=True)
class RedataLocationContextGateway(Gateway):
    """Base REST client for REData's near-a-coordinate location-context endpoints.

    Concrete per-domain subclasses set their own ``service_key`` (for
    UrbanLens's own rate-limit/cost tracking - REData pools its own outbound
    budget across these endpoints server-side regardless, see REData's
    ``docs/api-reference.md`` "Rate limiting") and add typed accessor methods
    that call :meth:`near_point` with their own path.
    """

    # default_factory, not a bare default: a dataclass field's bare default is evaluated
    # once at class-definition/import time, so a later settings change never reaches
    # subsequent instantiations - default_factory re-reads it fresh each time.
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
            limit: Bounded positive integer (REData caps at 200 for most of
                these endpoints).
            extra_params: Any endpoint-specific query params beyond the shared
                set above (e.g. hazards' ``min_magnitude``/``years``).

        Returns:
            The parsed :class:`LocationContextEnvelope`.

        Raises:
            LocationContextUnavailableError: A total blackout (every source
                covering the coordinate failed), a REData-side validation
                error, or the request itself failed outright.
        """
        params: dict[str, Any] = {"lat": latitude, "lng": longitude}
        if radius_meters is not None:
            params["radius_meters"] = radius_meters
        if provider is not None:
            params["provider"] = provider
        if force_refresh:
            params["force_refresh"] = "true"
        if limit is not None:
            params["limit"] = limit
        if extra_params:
            params.update(extra_params)
        return self._get_envelope(path, params)

    def get_json(self, path: str, params: dict[str, Any] | None = None) -> Any:
        """GET any REData endpoint outside the near-a-coordinate envelope and return its raw JSON body.

        For endpoints that share this gateway's auth/error handling but not
        its ``{"count","complete","results","providers"}`` shape - e.g. a
        by-name search (``/reference-documents/search/``) or a plain
        ``{"count","results"}`` envelope with no ``providers`` block
        (``/search/web/``, ``/search/news/``). Use :meth:`near_point` instead
        for anything shaped like REData's near-a-coordinate contract.

        Args:
            path: Path relative to ``base_url`` (leading slash optional).
            params: Query-string parameters, if any.

        Returns:
            The raw decoded JSON body (whatever type - object or array - the
            endpoint actually returns).

        Raises:
            LocationContextUnavailableError: A REData-side validation error,
                or the request itself failed outright.
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

        For write-shaped/non-cacheable endpoints (e.g. ``POST /routes/``) that
        share this gateway's auth/error handling but take a body rather than
        query params.

        Args:
            path: Path relative to ``base_url`` (leading slash optional).
            json_body: The JSON-serializable request body.

        Returns:
            The raw decoded JSON body.

        Raises:
            LocationContextUnavailableError: A REData-side validation error,
                or the request itself failed outright.
        """
        base_url = self.base_url
        if base_url is None:
            raise LocationContextUnavailableError(REASON_SOURCE_ERROR, "UL_REDATA_API_URL is not configured.")
        try:
            response = self.session.post(f"{base_url.rstrip('/')}/{path.lstrip('/')}", json=json_body, headers=self._headers, timeout=_REQUEST_TIMEOUT)
        except OSError as exc:
            # str(exc) on a requests/urllib3 connection error routinely embeds the full
            # request URL, including the lat/lng (or address) query params callers pass
            # in - which would undo every redact_coordinate()/redact_text() call a
            # caller wraps its *own* logging in. The exception type is enough for an
            # operator to diagnose a network failure without re-leaking the coordinate.
            raise LocationContextUnavailableError(REASON_SOURCE_ERROR, f"Could not reach REData: {type(exc).__name__}") from exc

        if response.status_code == 200:
            try:
                return response.json()
            except ValueError as exc:
                raise LocationContextUnavailableError(REASON_SOURCE_ERROR, "REData returned an unparseable response.") from exc
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
            return LocationContextEnvelope(
                count=int(body.get("count") or 0),
                complete=bool(body.get("complete", True)),
                results=list(body.get("results") or []),
                providers=list(body.get("providers") or []),
            )
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
        except OSError as exc:
            # str(exc) on a requests/urllib3 connection error routinely embeds the full
            # request URL, including the lat/lng (or address) query params callers pass
            # in - which would undo every redact_coordinate()/redact_text() call a
            # caller wraps its *own* logging in. The exception type is enough for an
            # operator to diagnose a network failure without re-leaking the coordinate.
            raise LocationContextUnavailableError(REASON_SOURCE_ERROR, f"Could not reach REData: {type(exc).__name__}") from exc

    def _raise_for_error_status(self, response: requests.Response, path: str) -> NoReturn:
        """Translate a non-200 REData response into a :class:`LocationContextUnavailableError`.

        Always raises - callers reach this only once ``response.status_code != 200``.
        """
        if response.status_code in (400, 503):
            try:
                body = response.json()
            except ValueError:
                body = {}
            reason = body.get("error") or REASON_SOURCE_ERROR
            raise LocationContextUnavailableError(reason, body.get("message", ""))

        logger.warning("REData request to %s failed (%s): %s", path, response.status_code, response.text[:500])
        raise LocationContextUnavailableError(REASON_SOURCE_ERROR, f"REData request failed with status {response.status_code}.")
