"""Resolves Google Maps CIDs to coordinates, choosing REData or Google Places.

Single chokepoint for the "which provider resolves a CID" decision (see
``docs/redata-cid-resolution.md`` for the full background):

- REData configured (``UL_REDATA_API_URL``/``UL_REDATA_API_KEY`` both set) -
  the primary deployment's path. A batch call to REData's
  ``POST /places/resolve-cids/`` (see ``../REData/docs/api-reference.md``,
  "Google Maps CID resolution" - deliberately asynchronous on REData's end, so
  a cid it hasn't finished resolving yet comes back as ``pending``, not an
  error). On a request failure (REData unreachable, non-200, ...), the whole
  batch is reported as ``pending`` for the caller to retry later - no fallback
  to Google here, so behavior stays predictable per-install rather than
  silently mixing providers.
- REData not configured - assumed to be an install without access to it (e.g.
  someone else running UrbanLens themselves). Falls back to calling Google
  Places directly, one CID at a time, via the existing
  ``GoogleGeocodingGateway.get_coordinates_by_cid`` (already rate-limited and
  cached - see ``services.rate_limiter`` and ``GeocodedLocation``).

Only ever called from background work (see ``tasks.resolve_deferred_pin_locations``)
- never from a request/response cycle, since both providers can be slow.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import logging

import requests

from urbanlens.dashboard.services.apis.locations.google.geocoding import GoogleGeocodingGateway
from urbanlens.dashboard.services.apis.locations.google.redata_cid_gateway import RedataCidGateway
from urbanlens.dashboard.services.gateway import GatewayRequestError
from urbanlens.dashboard.services.rate_limiter import RateLimitExceededError
from urbanlens.UrbanLens.settings.app import settings

logger = logging.getLogger(__name__)

PROVIDER_REDATA = "redata"
PROVIDER_GOOGLE = "google_places"


@dataclass(frozen=True, slots=True)
class CidResolutionResult:
    """Outcome of one ``resolve_cids`` call.

    Every cid passed in ends up in exactly one of ``resolved``, ``unresolvable``,
    or ``pending``.
    """

    provider: str
    resolved: dict[int, tuple[float, float]] = field(default_factory=dict)
    #: Confirmed no answer exists (e.g. Google's NOT_FOUND/ZERO_RESULTS, or
    #: REData explicitly returning null) - a terminal result, never retried.
    unresolvable: set[int] = field(default_factory=set)
    #: Rate-limited or a transient failure - the caller should retry these
    #: later, not treat them as done.
    pending: list[int] = field(default_factory=list)


def resolve_cids(cids: list[int]) -> CidResolutionResult:
    """Resolve a batch of CIDs to coordinates via whichever provider is configured.

    Args:
        cids: Google Maps CIDs to resolve.

    Returns:
        A :class:`CidResolutionResult` partitioning every input cid into
        resolved/unresolvable/pending.
    """
    if settings.redata_api_url and settings.redata_api_key:
        return _resolve_via_redata(cids)
    return _resolve_via_google(cids)


def _resolve_via_redata(cids: list[int]) -> CidResolutionResult:
    try:
        batch = RedataCidGateway().resolve_cids(cids)
    except GatewayRequestError:
        logger.warning("REData CID batch resolution failed for %d cid(s) - deferring for retry.", len(cids))
        return CidResolutionResult(provider=PROVIDER_REDATA, pending=list(cids))

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
