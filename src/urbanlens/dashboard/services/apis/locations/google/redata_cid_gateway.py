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

from collections.abc import Sequence
from dataclasses import dataclass, field
import logging
from typing import Any, ClassVar

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
class CidLookupEntry:
    """One ``resolve_cids`` request entry - a CID, optionally with its source Google Maps URL.

    REData's docs say passing the place's own ``url`` (or ``fid``/``data``)
    resolves "faster and more reliably" than a plain cid, since it lets REData
    hit the place directly instead of only having the bare id to go on -
    ``cid`` is never required alongside it (REData derives it automatically),
    but is included here too since callers already know it locally and it
    keeps ``RedataCidBatchResult`` keyed on the same value the caller expects.
    """

    cid: int
    url: str | None = None


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

    def resolve_cids(self, cids: Sequence[int | CidLookupEntry]) -> RedataCidBatchResult:
        """Resolve a batch of Google Maps CIDs to coordinates via REData.

        Deliberately asynchronous on REData's end - a cid REData hasn't
        resolved yet comes back in ``pending``, not as an error; call again
        later to see if it's settled.

        Args:
            cids: CIDs to resolve (the decimal value after the ``:0x`` in a
                Google Maps place URL's data segment), or a :class:`CidLookupEntry`
                for a cid whose source Google Maps URL is also known - see that
                class for why passing it is preferred. Transparently chunked
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

        entries = [entry if isinstance(entry, CidLookupEntry) else CidLookupEntry(cid=entry) for entry in cids]
        for i in range(0, len(entries), _MAX_CIDS_PER_REQUEST):
            self._resolve_chunk(entries[i : i + _MAX_CIDS_PER_REQUEST], result)
        return result

    def _resolve_chunk(self, entries: list[CidLookupEntry], result: RedataCidBatchResult) -> None:
        body = self._post_resolve_cids(entries)

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
        for entry in entries:
            if entry.cid not in accounted:
                result.pending.add(entry.cid)

    def _post_resolve_cids(self, entries: list[CidLookupEntry]) -> dict:
        base_url = self.base_url
        if base_url is None:
            # __post_init__ already validates this for the normal construction
            # path; this only narrows the type for mypy.
            raise GatewayRequestError("UL_REDATA_API_URL is not configured.")

        # A plain int when only the cid is known - matches the documented
        # shorthand exactly - or {"cid", "url"} once the source Google Maps
        # URL is available, which REData resolves via more reliably than cid
        # alone (see CidLookupEntry).
        payload_cids: list[int | dict[str, Any]] = [{"cid": entry.cid, "url": entry.url} if entry.url else entry.cid for entry in entries]

        try:
            response = self.session.post(
                f"{base_url.rstrip('/')}/api/v1/places/resolve-cids/",
                json={"cids": payload_cids},
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
