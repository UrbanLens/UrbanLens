"""Gateway for REData's CID -> coordinate resolution and cached place-detail endpoints."""

from __future__ import annotations

from dataclasses import dataclass, field
import logging
from typing import TYPE_CHECKING, Any, ClassVar

from urbanlens.dashboard.services.apis.locations.cid_validation import InvalidCidError, parse_cid
from urbanlens.dashboard.services.core.gateway import Gateway, GatewayRequestError, UpstreamBusyError, read_capped, upstream_retry_after
from urbanlens.UrbanLens.settings.app import settings

if TYPE_CHECKING:
    from collections.abc import Sequence

logger = logging.getLogger(__name__)


class RedataPermissionError(GatewayRequestError):
    """Raised when REData rejects the API key itself (401/403), not a transient failure."""


_REQUEST_TIMEOUT = 60

#: see api-reference.md. Chunked transparently so callers never have to think
#: about this limit.
_MAX_CIDS_PER_REQUEST = 10_000


@dataclass(frozen=True, slots=True)
class CidLookupEntry:
    """One ``resolve_cids`` request entry - a CID, optionally with its source Google Maps URL."""

    cid: int
    url: str | None = None

    def as_request(self) -> str | dict[str, str]:
        """This entry as ``resolve-cids`` takes it: the bare cid, or ``{"cid", "url"}`` when the URL is known.

        The cid travels as decimal digits, never a JSON number, so no JSON reader on the way can hand
        it to a float64 (REData P120). REData resolves a place more reliably from its own URL.
        """
        cid = str(int(self.cid))
        return {"cid": cid, "url": self.url} if self.url else cid


@dataclass(frozen=True, slots=True)
class RedataCidBatchResult:
    """One (possibly chunked) ``resolve_cids`` call's outcome."""

    resolved: dict[int, tuple[float, float]] = field(default_factory=dict)
    #: REData confirmed, after repeated attempts, no resolvable location exists.
    unresolvable: set[int] = field(default_factory=set)
    #: Just queued or already in flight server-side (Celery, on REData's end) -
    #: poll again later. Never overlaps with `resolved`/`unresolvable`.
    pending: set[int] = field(default_factory=set)
    #: Refused by REData before any lookup, with its ``cid_validation`` code - never worth asking again.
    rejected: dict[int, str] = field(default_factory=dict)


@dataclass(slots=True, kw_only=True)
class RedataCidGateway(Gateway):
    """REST client for REData's ``POST /places/resolve-cids/`` endpoint."""

    service_key: ClassVar[str] = "redata_cid_lookup"
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
        return {"Authorization": f"Bearer {self.api_key}", "Accept": "application/json", "Content-Type": "application/json"}

    def resolve_cids(self, cids: Sequence[int | CidLookupEntry]) -> RedataCidBatchResult:
        """Resolve a batch of Google Maps CIDs to coordinates via REData.

        Args:
            cids: CIDs to resolve (the decimal value after the ``:0x`` in a
                Google Maps place URL's data segment), or a :class:`CidLookupEntry`
                for a cid whose source Google Maps URL is also known - see that
                class for why passing it is preferred. Transparently chunked
                into REData's 10,000-per-request cap.

        Returns:
            A :class:`RedataCidBatchResult` partitioning every input cid.

        Raises:
            RedataPermissionError: REData rejected the API key itself (401/403) - not transient, callers should stop retrying.
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
                cid = parse_cid(cid_str)
            except InvalidCidError:
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
                result.pending.add(parse_cid(cid_str))
            except InvalidCidError:
                logger.warning("REData returned a non-integer cid in 'pending': %r", cid_str)

        for refusal in body.get("rejected") or []:
            refused = _rejected_cid(refusal, entries)
            if refused is None:
                logger.warning("REData refused an entry this request cannot match: %r", refusal)
                continue
            result.rejected[refused] = str(refusal.get("error") or "rejected")
            result.pending.discard(refused)
            logger.warning("REData refused cid %d: %s", refused, refusal.get("message") or result.rejected[refused])

        # Defensive: a cid REData didn't mention in either bucket at all -
        # treat as pending (retry later) rather than silently dropping it.
        accounted = result.resolved.keys() | result.unresolvable | result.pending | result.rejected.keys()
        for entry in entries:
            if entry.cid not in accounted:
                result.pending.add(entry.cid)

    def _post_resolve_cids(self, entries: list[CidLookupEntry]) -> dict:
        base_url = self.base_url
        if base_url is None:
            # __post_init__ already validates this for the normal construction
            # path; this only narrows the type for mypy.
            raise GatewayRequestError("UL_REDATA_API_URL is not configured.")

        try:
            response = self.session.post(
                f"{base_url.rstrip('/')}/api/v1/places/resolve-cids/",
                json={"cids": [entry.as_request() for entry in entries]},
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

    def get_place_detail(self, cid: int) -> dict[str, Any] | None:
        """Read REData's cached deep-scrape record for an already-resolved CID.

        Args:
            cid: The Google Maps CID (see :meth:`resolve_cids`/:class:`CidLookupEntry`).

        Returns:
            The place payload dict (``scrape_pending``/``scrape_stale`` say whether the scrape has run yet / is due a refresh - the fields it fills are still returned as last captured either way), or None when REData has never resolved this CID at all -...

        Raises:
            RedataPermissionError: REData rejected the API key itself (401/403) - not transient, callers should stop retrying.
        """
        base_url = self.base_url
        if base_url is None:
            raise GatewayRequestError("UL_REDATA_API_URL is not configured.")

        try:
            response = self.session.get(f"{base_url.rstrip('/')}/api/v1/places/cid/{cid}/", headers=self._headers, timeout=_REQUEST_TIMEOUT)
        except OSError as exc:
            raise GatewayRequestError(f"Could not reach REData: {exc}") from exc

        if response.status_code == 404:
            return None

        if response.status_code != 200:
            logger.warning("REData place detail lookup for cid %d failed (%s): %s", cid, response.status_code, response.text[:500])
            if response.status_code in (401, 403):
                raise RedataPermissionError(f"REData rejected the request with status {response.status_code} - check UL_REDATA_API_KEY's scopes.")
            raise GatewayRequestError(f"REData request failed with status {response.status_code}.")

        try:
            return dict(response.json())
        except ValueError as exc:
            raise GatewayRequestError("REData returned an unparseable response.") from exc

    def download_media(self, cid: int, media_id: int) -> tuple[bytes, str]:
        """Download one deep-scraped media item's actual file bytes.

        Args:
            cid: The place's Google Maps CID.
            media_id: The media item's id, from a :meth:`get_place_detail`
                payload's ``media`` list.

        Returns:
            Tuple of (file bytes, content-type).

        Raises:
            RedataPermissionError: REData rejected the API key itself (401/403).
        """
        base_url = self.base_url
        if base_url is None:
            raise GatewayRequestError("UL_REDATA_API_URL is not configured.")

        try:
            response = self.session.get(f"{base_url.rstrip('/')}/api/v1/places/cid/{cid}/media/{media_id}/download/", headers=self._headers, timeout=_REQUEST_TIMEOUT, stream=True)
        except OSError as exc:
            raise GatewayRequestError(f"Could not reach REData: {exc}") from exc

        if response.status_code == 200:
            return read_capped(response, what="REData cid media"), response.headers.get("Content-Type", "application/octet-stream")

        if response.status_code in (401, 403):
            raise RedataPermissionError(f"REData rejected the request with status {response.status_code} - check UL_REDATA_API_KEY's scopes.")
        logger.warning("REData media download for cid %d media %d failed (%s): %s", cid, media_id, response.status_code, response.text[:500])
        if (wait := upstream_retry_after(response)) is not None:
            raise UpstreamBusyError(f"REData request failed with status {response.status_code}.", retry_after=wait)
        raise GatewayRequestError(f"REData request failed with status {response.status_code}.")


def _rejected_cid(refusal: object, entries: list[CidLookupEntry]) -> int | None:
    """Which of *entries* one of REData's ``rejected`` items refers to, by its ``index`` in the request."""
    if not isinstance(refusal, dict):
        return None
    index = refusal.get("index")
    if isinstance(index, int) and not isinstance(index, bool) and 0 <= index < len(entries):
        return entries[index].cid
    return None
