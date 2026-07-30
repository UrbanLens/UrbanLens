"""Gateway for REData's CID -> coordinate resolution endpoint.

See ``docs/redata-cid-resolution.md`` for why this exists (Google Maps CIDs
decoded via the free literal-S2-cell heuristic are wrong ~31% of the time).
Contract implemented here matches REData's own docs: ``../REData/docs/api-reference.md``,
"Google Maps CID resolution" section - if the two ever disagree, REData's docs
(and its code) win.

This is the same REData account/deployment already used for property records
(``services.apis.property_records.redata_gateway.RedataGateway`` - same
``UL_REDATA_API_URL``/``UL_REDATA_API_KEY`` settings, same bearer-token
convention) hitting a different endpoint. Kept as a separate class/service key
rather than a method on ``RedataGateway`` since it's a distinct capability
(place geocoding, not property records) with its own call volume to track -
the API key used must carry REData's ``places:read`` scope.

Do not call this directly - go through
``services.apis.locations.cid_resolution.resolve_cids``, which decides whether
REData or the Google Places fallback should handle a given batch.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import logging
from typing import ClassVar

from urbanlens.dashboard.services.gateway import Gateway, GatewayRequestError
from urbanlens.UrbanLens.settings.app import settings

logger = logging.getLogger(__name__)


class RedataPermissionError(GatewayRequestError):
    """Raised when REData rejects the API key itself (401/403), not a transient failure.

    An expired/invalid key or a key missing the ``places:read`` scope will
    never succeed no matter how many times the request is retried - distinct
    from ``GatewayRequestError`` so callers can stop retrying and surface this
    to the user instead of looping forever.
    """


_REQUEST_TIMEOUT = 60

#: REData caps a single call's batch at 10,000 (400 too_many_cids above that) -
#: see api-reference.md. Chunked transparently so callers never have to think
#: about this limit.
_MAX_CIDS_PER_REQUEST = 10_000


@dataclass(frozen=True, slots=True)
class RedataCidBatchResult:
    """One (possibly chunked) ``resolve_cids`` call's outcome.

    Every requested cid ends up in exactly one bucket - mirrors REData's own
    ``results``/``pending`` split (see the module docstring).
    """

    resolved: dict[int, tuple[float, float]] = field(default_factory=dict)
    #: REData confirmed, after repeated attempts, no resolvable location exists.
    unresolvable: set[int] = field(default_factory=set)
    #: Just queued or already in flight server-side (Celery, on REData's end) -
    #: poll again later. Never overlaps with `resolved`/`unresolvable`.
    pending: set[int] = field(default_factory=set)


@dataclass(slots=True, kw_only=True)
class RedataCidGateway(Gateway):
    """REST client for REData's ``POST /places/resolve-cids/`` endpoint."""

    service_key: ClassVar[str] = "redata_cid_lookup"
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
        return {"Authorization": f"Bearer {self.api_key}", "Accept": "application/json", "Content-Type": "application/json"}

    def resolve_cids(self, cids: list[int]) -> RedataCidBatchResult:
        """Resolve a batch of Google Maps CIDs to coordinates via REData.

        Deliberately asynchronous on REData's end - a cid REData hasn't
        resolved yet comes back in ``pending``, not as an error; call again
        later to see if it's settled.

        Args:
            cids: CIDs to resolve (the decimal value after the ``:0x`` in a
                Google Maps place URL's data segment). Transparently chunked
                into REData's 10,000-per-request cap.

        Returns:
            A :class:`RedataCidBatchResult` partitioning every input cid.

        Raises:
            RedataPermissionError: REData rejected the API key itself (401/403) -
                not transient, callers should stop retrying.
            GatewayRequestError: A chunk's request to REData failed outright
                (network error, non-200, unparseable/malformed body). Callers
                should treat the whole batch as transiently unresolved and
                retry later - REData not being reachable says nothing about
                whether any individual cid is resolvable.
        """
        result = RedataCidBatchResult()
        if not cids:
            return result

        for i in range(0, len(cids), _MAX_CIDS_PER_REQUEST):
            self._resolve_chunk(cids[i : i + _MAX_CIDS_PER_REQUEST], result)
        return result

    def _resolve_chunk(self, cids: list[int], result: RedataCidBatchResult) -> None:
        body = self._post_resolve_cids(cids)

        raw_results = body.get("results")
        if not isinstance(raw_results, dict):
            raise GatewayRequestError("REData response is missing a 'results' object.")

        for cid_str, entry in raw_results.items():
            try:
                cid = int(cid_str)
            except (TypeError, ValueError):
                logger.warning("REData returned a non-integer cid key: %r", cid_str)
                continue
            if entry is None:
                result.unresolvable.add(cid)
                continue
            try:
                result.resolved[cid] = (float(entry["lat"]), float(entry["lng"]))
            except (KeyError, TypeError, ValueError):
                logger.warning("REData returned a malformed entry for cid %d: %r", cid, entry)
                result.pending.add(cid)

        for cid_str in body.get("pending") or []:
            try:
                result.pending.add(int(cid_str))
            except (TypeError, ValueError):
                logger.warning("REData returned a non-integer cid in 'pending': %r", cid_str)

        # Defensive: a cid REData didn't mention in either bucket at all -
        # treat as pending (retry later) rather than silently dropping it.
        accounted = result.resolved.keys() | result.unresolvable | result.pending
        for cid in cids:
            if cid not in accounted:
                result.pending.add(cid)

    def _post_resolve_cids(self, cids: list[int]) -> dict:
        base_url = self.base_url
        if base_url is None:
            # __post_init__ already validates this for the normal construction
            # path; this only narrows the type for mypy.
            raise GatewayRequestError("UL_REDATA_API_URL is not configured.")

        try:
            response = self.session.post(
                f"{base_url.rstrip('/')}/api/v1/places/resolve-cids/",
                json={"cids": cids},
                headers=self._headers,
                timeout=_REQUEST_TIMEOUT,
            )
        except OSError as exc:
            raise GatewayRequestError(f"Could not reach REData: {exc}") from exc

        if response.status_code != 200:
            logger.warning("REData CID resolution failed (%s): %s", response.status_code, response.text[:500])
            if response.status_code in (401, 403):
                raise RedataPermissionError(f"REData rejected the request with status {response.status_code} - check UL_REDATA_API_KEY's scopes.")
            raise GatewayRequestError(f"REData request failed with status {response.status_code}.")

        try:
            return dict(response.json())
        except ValueError as exc:
            raise GatewayRequestError("REData returned an unparseable response.") from exc
