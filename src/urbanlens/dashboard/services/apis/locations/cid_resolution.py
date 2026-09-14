"""Resolves Google Maps CIDs to coordinates, choosing REData or Google Places."""

from __future__ import annotations

from dataclasses import dataclass, field
import logging

import requests

from urbanlens.dashboard.services.apis.locations.google.geocoding import GoogleGeocodingGateway
from urbanlens.dashboard.services.apis.locations.google.redata_cid_gateway import CidLookupEntry, RedataCidGateway, RedataPermissionError
from urbanlens.dashboard.services.core.gateway import GatewayRequestError
from urbanlens.dashboard.services.core.rate_limiter import RateLimitExceededError
from urbanlens.UrbanLens.settings.app import settings

logger = logging.getLogger(__name__)

PROVIDER_REDATA = "redata"
PROVIDER_GOOGLE = "google_places"


@dataclass(frozen=True, slots=True)
class CidResolutionResult:
    """Outcome of one ``resolve_cids`` call."""

    provider: str
    resolved: dict[int, tuple[float, float]] = field(default_factory=dict)
    #: Confirmed no answer exists (e.g. Google's NOT_FOUND/ZERO_RESULTS, or
    #: REData explicitly returning null) - a terminal result, never retried.
    unresolvable: set[int] = field(default_factory=set)
    #: Rate-limited or a transient failure - the caller should retry these
    #: later, not treat them as done.
    pending: list[int] = field(default_factory=list)
    #: REData rejected the API key itself (401/403) - also left in `pending` for its count, but this
    #: flag tells the caller retrying is pointless until the key/scope is fixed, so it should stop
    #: and surface the failure instead of looping forever.
    auth_failed: bool = False
    #: The REData request itself failed outright (network error, non-200, unparseable body) - also
    #: left in `pending` for its count, but this distinguishes "the whole batch made zero progress
    #: this attempt" from a response that resolved/deferred cids normally.
    #: Lets the caller count *consecutive* failures across retries and eventually give up on a
    request_failed: bool = False


def resolve_cids(cids: list[int], urls_by_cid: dict[int, str] | None = None) -> CidResolutionResult:
    """Resolve a batch of CIDs to coordinates via whichever provider is configured.

    Args:
        cids: Google Maps CIDs to resolve.
        urls_by_cid: The source Google Maps URL for any of ``cids`` that came from one (e.g. a Takeout CSV import) - passed through to REData when it's the configured provider, since it resolves via a place's own URL faster and more reliably than the bare cid alone...

    Returns:
        A :class:`CidResolutionResult` partitioning every input cid into resolved/unresolvable/pending."""
    if settings.redata_api_url and settings.redata_api_key:
        return _resolve_via_redata(cids, urls_by_cid)
    return _resolve_via_google(cids)


def _resolve_via_redata(cids: list[int], urls_by_cid: dict[int, str] | None = None) -> CidResolutionResult:
    entries = [CidLookupEntry(cid=cid, url=(urls_by_cid or {}).get(cid)) for cid in cids]
    try:
        batch = RedataCidGateway().resolve_cids(entries)
    except RedataPermissionError:
        logger.exception("REData rejected the API key resolving %d cid(s) - not retrying until UL_REDATA_API_KEY's scopes are fixed.", len(cids))
        return CidResolutionResult(provider=PROVIDER_REDATA, pending=list(cids), auth_failed=True)
    except GatewayRequestError:
        logger.warning("REData CID batch resolution failed for %d cid(s) - deferring for retry.", len(cids))
        return CidResolutionResult(provider=PROVIDER_REDATA, pending=list(cids), request_failed=True)

    return CidResolutionResult(
        provider=PROVIDER_REDATA,
        resolved=batch.resolved,
        unresolvable=batch.unresolvable,
        pending=list(batch.pending),
    )


def _resolve_via_google(cids: list[int]) -> CidResolutionResult:
    gateway = GoogleGeocodingGateway()
    result = CidResolutionResult(provider=PROVIDER_GOOGLE)

    for i, cid in enumerate(cids):
        try:
            lat, lon = gateway.get_coordinates_by_cid(cid)
        except RateLimitExceededError:
            # Every cid from here on hasn't been attempted at all yet - stop
            # rather than let the rest fail the same way one by one.
            result.pending.extend(cids[i:])
            break
        except requests.RequestException:
            logger.warning("Transient error resolving cid %d via Google Places - will retry.", cid, exc_info=True)
            result.pending.append(cid)
            continue

        if lat is None or lon is None:
            # Google's own status was e.g. NOT_FOUND/ZERO_RESULTS/REQUEST_DENIED -
            # see get_coordinates_by_cid's docstring. Not a transient condition.
            result.unresolvable.add(cid)
        else:
            result.resolved[cid] = (lat, lon)

    return result
