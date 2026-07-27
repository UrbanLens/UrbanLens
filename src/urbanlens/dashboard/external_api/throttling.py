"""Per-credential, tiered rate limiting for the external API.

Keyed by the credential itself, not the underlying user or IP: a user with
several keys (e.g. one per connected app) shouldn't have one misbehaving app
burn through a budget shared with their other keys, which is what DRF's
built-in ``UserRateThrottle``/``AnonRateThrottle`` (used by the internal API,
see ``REST_FRAMEWORK["DEFAULT_THROTTLE_CLASSES"]``) would do instead.

Three limits apply together, replacing the single flat cap this module used to
impose:

- **read** (``external_api_read``) - the common case. A mobile client doing a
  first full sync legitimately makes hundreds of reads in a few minutes, which
  a cap tight enough to be meaningful for writes would refuse.
- **write** (``external_api_write``) - far tighter, since writes cost more and
  a client with a runaway outbox is the failure mode worth bounding.
- **burst** (``external_api_burst``) - a short-window cap applied to *every*
  request regardless of tier, so a stampede is smoothed out without lowering
  either hourly ceiling.

A request's tier is derived from what the view declares it requires, so an
endpoint is classified by the same declaration that gates its access - there is
no second list to keep in sync.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, ClassVar

from rest_framework.throttling import SimpleRateThrottle

if TYPE_CHECKING:
    from rest_framework.request import Request
    from rest_framework.views import APIView

#: Scope-name suffixes that mark a scope as mutating. ``:manage`` counts as a
#: write because the scopes using it (e.g. ``push:manage``) register and remove
#: things rather than merely reading them.
_WRITE_SCOPE_SUFFIXES = (":write", ":manage")

TIER_READ = "read"
TIER_WRITE = "write"


def request_tier(view: APIView, method: str) -> str:
    """Classify one request as the read or the write tier.

    An explicit ``view.throttle_tier_by_method`` mapping wins when present, for
    the rare endpoint whose cost doesn't match its scopes (an expensive
    read-only export, say). Otherwise the tier follows the view's
    ``required_scopes_by_method`` declaration for *method*: any required scope
    ending in a write suffix makes the whole request a write.

    A method with no declaration at all is treated as a write. That is the
    conservative reading - an endpoint whose author forgot to declare its
    scopes should land in the tighter bucket, not the looser one. (It will also
    be refused outright by ``HasApiKeyScope``, which fails closed on the same
    input; this is the throttle layer agreeing rather than relying on that.)

    Args:
        view: The view handling the request.
        method: The request's HTTP method, e.g. ``"GET"``.

    Returns:
        Either :data:`TIER_READ` or :data:`TIER_WRITE`.
    """
    explicit: dict[str, str] = getattr(view, "throttle_tier_by_method", {}) or {}
    if method in explicit:
        return explicit[method]

    declared: dict[str, frozenset[str]] = getattr(view, "required_scopes_by_method", {}) or {}
    required = declared.get(method)
    if not required:
        return TIER_WRITE
    if any(str(scope).endswith(_WRITE_SCOPE_SUFFIXES) for scope in required):
        return TIER_WRITE
    return TIER_READ


class ExternalApiRateThrottle(SimpleRateThrottle):
    """Base for the external API's per-credential throttles.

    Subclasses set ``scope`` (the ``DEFAULT_THROTTLE_RATES`` key) and
    optionally ``tier``. A throttle with a ``tier`` only counts requests in
    that tier and waves the rest through untouched; one with the default empty
    ``tier`` counts every request, which is how the burst cap layers on top of
    the two hourly caps.
    """

    #: The tier this throttle counts, or ``""`` to count every request.
    tier: ClassVar[str] = ""

    def allow_request(self, request: Request, view: APIView) -> bool:
        """Apply this throttle only to requests in its tier.

        Args:
            request: The incoming request.
            view: The view handling it.

        Returns:
            True when the request is permitted (including when this throttle
            doesn't apply to the request's tier at all).
        """
        if self.tier and request_tier(view, request.method or "") != self.tier:
            return True
        return super().allow_request(request, view)

    def get_cache_key(self, request: Request, view: APIView) -> str | None:
        """Build the throttle cache key from the authenticated credential, or opt out entirely.

        Returning ``None`` disables throttling for the request (DRF's
        convention) - correct here since a request with no resolved credential
        will already be rejected by authentication/permissions before this
        would matter.

        Args:
            request: The incoming request.
            view: The view handling it.

        Returns:
            The per-credential cache key, or None to skip throttling.
        """
        credential = getattr(request, "auth", None)
        if credential is None:
            return None
        # The type name disambiguates the pk namespace - an ApiKey and an
        # OAuth2 AccessToken can share a pk, and each credential deserves its
        # own budget for the same reason two ApiKeys do.
        return self.cache_format % {"scope": self.scope, "ident": f"{type(credential).__name__}:{credential.pk}"}


class ExternalApiReadThrottle(ExternalApiRateThrottle):
    """The hourly cap on non-mutating external API requests."""

    scope = "external_api_read"
    tier = TIER_READ


class ExternalApiWriteThrottle(ExternalApiRateThrottle):
    """The (tighter) hourly cap on mutating external API requests."""

    scope = "external_api_write"
    tier = TIER_WRITE


class ExternalApiBurstThrottle(ExternalApiRateThrottle):
    """The short-window cap applied to every external API request, both tiers."""

    scope = "external_api_burst"


class LocationSearchThrottle(ExternalApiReadThrottle):
    """A separate budget for autocomplete, which is charged per keystroke.

    Views using this replace :class:`ExternalApiReadThrottle` with it rather
    than stacking the two. Autocomplete is the one read endpoint whose request
    count tracks typing rather than data volume, so counting it against the
    shared hourly read cap would let a few minutes of searching exhaust the
    budget the client needs for actually syncing. Its own (larger) ceiling
    keeps that traffic bounded without coupling the two.

    :class:`ExternalApiBurstThrottle` still applies on top, so a client that
    forgets to debounce its input is still smoothed out.
    """

    scope = "external_api_location_search"
