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


class ExternalApiMediaThrottle(ExternalApiRateThrottle):
    """The cap on credential-authenticated media-file fetches.

    Its own budget rather than a share of the read/burst caps, because media
    fetches have the opposite shape to API calls: one screen of a gallery is
    dozens of file requests in a couple of seconds, which the 60/minute burst
    cap would refuse, while the whole point of metering here is that a leaked
    key must not turn ``/media/`` into an unmetered CDN. Separating them lets
    the file budget be generous without loosening the API budget.

    Applied by ``controllers.media.MediaGateView`` by hand - it is a plain
    Django ``View``, so DRF's throttle machinery never runs for it.
    """

    scope = "external_api_media"


class ExternalApiResyncThrottle(ExternalApiRateThrottle):
    """A much tighter cap for endpoints whose cost is unbounded in the caller's data.

    The ordinary write cap bounds *how many* writes a credential makes, which
    is the right question when each one costs about the same. It is the wrong
    question for a smart-list resync: one cheap POST re-evaluates every pin the
    profile owns against the list's filter and boundary, so a caller with a
    large map can turn a request that costs them nothing into seconds of
    server-side geospatial work, repeatedly, while staying comfortably inside
    the 300/hour write budget.

    Applied *in addition* to the standard three by overriding
    ``throttle_classes`` on the view (see
    ``external_api.views.PinListResyncView``), so a resync still counts against
    the burst and write caps as well.
    """

    scope = "external_api_resync"


class GameStartThrottle(ExternalApiRateThrottle):
    """A resync-style cap on starting a game session.

    Starting a SpotGuessr session is the most expensive single request in the
    games domain and its cost is unbounded in the caller's own data, exactly
    like a smart-list resync: round generation runs up to 25 eligibility passes
    over every location the player has pinned, the difficulty-proxy lookup that
    ranks candidates is an N+1 across them, and a Street View round additionally
    makes a *billed* third-party imagery call per attempt. The ordinary write
    cap bounds how many writes a credential makes, which is the wrong question
    when one of them can cost seconds of geospatial work and real money.

    Applied *in addition* to the standard three by overriding
    ``throttle_classes`` on the view, so a start still counts against the burst
    and write caps too. ``tier`` is the write tier so that a view serving both a
    cheap list GET and an expensive create POST on one URL only charges the
    POST.

    Note:
        ``DEFAULT_THROTTLE_RATES`` has no entry for this scope yet, so
        :attr:`fallback_rate` applies. Adding ``"external_api_game_start"`` to
        the settings dict takes precedence with no change here.
    """

    scope = "external_api_game_start"
    tier = TIER_WRITE

    #: Used when the settings dict has no rate for :attr:`scope`. Generous
    #: enough that a genuine player never meets it (a session is 3-20 rounds, so
    #: this is dozens of full games an hour) and tight enough that a script
    #: cannot turn session creation into an unmetered geospatial workload.
    fallback_rate: ClassVar[str] = "40/hour"

    def get_rate(self) -> str:
        """Return the configured rate for this scope, or :attr:`fallback_rate`.

        DRF raises ``ImproperlyConfigured`` for a scope missing from
        ``DEFAULT_THROTTLE_RATES``, which would take the whole endpoint down
        rather than merely leaving it untuned - a bad trade for a throttle whose
        default is deliberately conservative anyway.

        Returns:
            The rate string to enforce.
        """
        from django.core.exceptions import ImproperlyConfigured

        try:
            return super().get_rate()
        except ImproperlyConfigured:
            return self.fallback_rate


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


class GlobalSearchThrottle(ExternalApiRateThrottle):
    """A separate budget for the cross-domain search endpoint.

    Unlike :class:`LocationSearchThrottle`, which exists because autocomplete is
    charged per *keystroke*, this one exists because a single global search is
    not a single query: it fans out across every domain provider the calling
    credential is scoped for - pins, wikis, trips, photos, visits, comments and
    (for an OAuth2 client) direct messages - each with its own database work.
    Counting that against the shared hourly read cap would let a handful of
    searches starve the sync traffic the client actually depends on, and letting
    it share the *write* cap would be worse still.

    Applied *in addition* to the standard three by overriding
    ``throttle_classes`` on the view, so a search still counts against the burst
    and read caps as well.
    """

    scope = "external_api_global_search"
    tier = TIER_READ


class CalendarExportThrottle(ExternalApiRateThrottle):
    """A tight cap on trip calendar export and unexport.

    These are the only external endpoints that talk to a third party *on the
    request path*, and a single call may fan out to one upstream request per
    trip activity. The budget being consumed is therefore not only ours: an
    unthrottled client could exhaust the deployment's Google Calendar quota for
    every user at once, which is a failure mode no per-credential write cap
    would catch in time.

    Applied *in addition* to the standard three by overriding
    ``throttle_classes`` on the view.
    """

    scope = "external_api_calendar"
    tier = TIER_WRITE
