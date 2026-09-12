"""Gateway for REData's CID -> coordinate resolution and cached place-detail endpoints."""

from __future__ import annotations

from dataclasses import dataclass, field
import logging
from typing import TYPE_CHECKING, Any, ClassVar

from urbanlens.dashboard.services.core.gateway import Gateway, GatewayRequestError
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


@dataclass(frozen=True, slots=True)
class RedataCidBatchResult:
    """One (possibly chunked) ``resolve_cids`` call's outcome."""

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

        Returns:
            A :class:`RedataCidBatchResult` partitioning every input cid.

        Raises:
            RedataPermissionError: REData rejected the API key itself (401/403) - not transient, callers should stop retrying."""
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

        # A plain int when only the cid is known - matches the documented shorthand exactly - or
        # {"cid", "url"} once the source Google Maps URL is available, which REData resolves via
        # more reliably than cid alone (see CidLookupEntry).
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

    def get_place_detail(self, cid: int) -> dict[str, Any] | None:
        """Read REData's cached deep-scrape record for an already-resolved CID.

        Returns:
            The place payload dict (``scrape_pending``/``scrape_stale`` say whether the scrape has run yet / is due a refresh - the fields it fills are still returned as last captured either way), or None when REData has never resolved this CID at all -...

        Raises:
            RedataPermissionError: REData rejected the API key itself (401/403) - not transient, callers should stop retrying."""
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

        Returns:
            Tuple of (file bytes, content-type).

        Raises:
            RedataPermissionError: REData rejected the API key itself (401/403)."""
        base_url = self.base_url
        if base_url is None:
            raise GatewayRequestError("UL_REDATA_API_URL is not configured.")

        try:
            response = self.session.get(f"{base_url.rstrip('/')}/api/v1/places/cid/{cid}/media/{media_id}/download/", headers=self._headers, timeout=_REQUEST_TIMEOUT)
        except OSError as exc:
            raise GatewayRequestError(f"Could not reach REData: {exc}") from exc

        if response.status_code == 200:
            return response.content, response.headers.get("Content-Type", "application/octet-stream")

        if response.status_code in (401, 403):
            raise RedataPermissionError(f"REData rejected the request with status {response.status_code} - check UL_REDATA_API_KEY's scopes.")
        logger.warning("REData media download for cid %d media %d failed (%s): %s", cid, media_id, response.status_code, response.text[:500])
        raise GatewayRequestError(f"REData request failed with status {response.status_code}.")
