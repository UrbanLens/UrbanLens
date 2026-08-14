"""Rate limiting for external API calls.

Provides ``check_rate_limit`` and ``log_api_call`` helpers used by the
``_RateLimitedSession`` inside every ``Gateway`` subclass that declares a
``service_key``.  Configuration is persisted in ``ApiRateLimit`` rows, which
are auto-created on first access using the defaults in ``SERVICE_REGISTRY``.

``check_rate_limit`` (a COUNT query) and ``log_api_call`` (an INSERT) are
individually cheap but, called back-to-back with no locking, let concurrent
callers race: several requests can all see the count under the limit before
any of them has logged a call, producing a real burst above the configured
limit. ``_RateLimitedSession`` closes this by going through
``_reserve_call``/``_finalize_call`` instead of calling ``check_rate_limit``
and ``log_api_call`` directly - see their docstrings.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import timedelta
from decimal import Decimal
import logging
import time
from typing import Any

from django.db import DatabaseError, transaction
from django.utils import timezone

from urbanlens.dashboard.exceptions import DashboardError

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Service registry - default config for external API services that have not
# yet been converted to plugins. Plugin-provided integrations declare their
# defaults via ``UrbanLensPlugin.get_service_defaults`` instead; the merged
# view lives in ``all_service_defaults``. Rows are auto-created from these
# defaults the first time a service is seen.
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ServiceDefaults:
    """Default rate-limit configuration for one external API service."""

    display_name: str
    calls_per_minute: int | None = 20
    calls_per_day: int | None = 500
    calls_per_30_days: int | None = None
    #: Minimum seconds required between consecutive calls, enforced
    #: independently of the budgets above - see ApiRateLimit.min_interval_seconds's
    #: own docstring for why a rolling-window count alone isn't equivalent.
    min_interval_seconds: float | None = None
    usa_only: bool = False
    notes: str = ""
    #: Estimated USD cost per successful call, if confidently known from the
    #: provider's published pricing. None means "not yet priced" (which may
    #: still be a free service - see ``notes``), not "confirmed free". Only
    #: populate this from a specific, verifiable published rate; a wrong
    #: number here is worse than no cost-tracking at all for a feature whose
    #: whole purpose is informing real spending decisions - see
    #: ApiCallLog.cost_estimate's own docstring for the same caveat.
    cost_per_call: Decimal | None = None


SERVICE_REGISTRY: dict[str, ServiceDefaults] = {
    "google_geocoding": ServiceDefaults(
        display_name="Google Geocoding API",
        calls_per_minute=20,
        calls_per_day=500,
        # Places Details (used for CID lookups - see get_coordinates_by_cid) is
        # billed under the Essentials SKU: 10,000 free calls/month. Capped one
        # short of that so a full month of free-tier installs never crosses
        # into billing purely from float/rounding in the 30-day rolling window.
        calls_per_30_days=9999,
        notes="Free tier: 10,000 calls/month (Places Details Essentials SKU).",
        # Google's published rate is $5/1000 requests, consistent with this
        # entry's own $200-credit/~40,000-calls note (200/40000 = 0.005).
        cost_per_call=Decimal("0.005"),
    ),
    "redata_cid_lookup": ServiceDefaults(
        display_name="REData CID Resolution",
        # Our own outbound throttle, well under REData's own dedicated
        # 200 requests/hour-per-key limit on this endpoint (see
        # ../REData/docs/api-reference.md) - deliberately generous since each
        # call is a batch of up to 10,000 CIDs, not one lookup.
        calls_per_minute=10,
        calls_per_day=None,
        calls_per_30_days=None,
        notes="Batch CID->coordinate resolution via POST /places/resolve-cids/. See docs/designs/redata-cid-resolution.md.",
    ),
    "redata_places": ServiceDefaults(
        display_name="REData Places",
        # Shares REData's single 1,000 req/hour "lookup" pool with redata_api
        # (property records) and cultural-resources - NOT redata_cid_lookup,
        # which has its own separate, dedicated 200/hour pool. Deliberately
        # conservative since this rate limiter has no cross-service
        # shared-budget concept and redata_api already draws from the same pool.
        calls_per_minute=20,
        calls_per_day=None,
        notes="Places API (New) via REData - permanently cached on REData's end. See services.apis.locations.places_resolution.",
    ),
    "redata_photos": ServiceDefaults(
        display_name="REData Photo Relevance",
        # Each call is a batch (up to 200 photos / 1,000 votes / 1,000
        # confidence lookups per REData's own limits), so real call volume
        # stays low relative to photo/vote counts - generous but still bounded
        # in case a burst of uploads or votes fires many small batches back to
        # back. See services.photos.redata_relevance.
        calls_per_minute=30,
        calls_per_day=None,
        notes="Photo submission/voting/confidence via POST /photos/, /photos/votes/, /photos/confidence/.",
    ),
    "redata_labels": ServiceDefaults(
        display_name="REData Label Suggestions",
        # Taxonomy/assignment syncs are batched (up to 2,000 labels / 500
        # locations per REData's own limits) and only fire on actual writes;
        # suggestion lookups are one call per dialog open. Generous but still
        # bounded, matching redata_photos.
        calls_per_minute=30,
        calls_per_day=None,
        notes="Tag/category taxonomy + assignment sync and suggestions via POST /labels/, /labels/assignments/, /labels/suggest/.",
    ),
    "redata_geocode": ServiceDefaults(
        display_name="REData Geocoding",
        # Shares REData's single 1,000 req/hour "lookup" pool (see the
        # api-reference.md rate-limiting section) with weather/imagery/
        # elevation/hazards/etc. below - deliberately conservative on our own
        # side since this rate limiter has no cross-service shared-budget
        # concept and every one of them draws from the same REData-side pool.
        calls_per_minute=20,
        calls_per_day=None,
        notes="Forward/reverse geocoding via GET /geocode/, /geocode/reverse/. See services.apis.locations.geocode_resolution.",
    ),
    "redata_weather": ServiceDefaults(
        display_name="REData Weather",
        calls_per_minute=20,
        calls_per_day=None,
        notes="Current conditions/forecast/sun times via GET /weather/ - every registered provider (Open-Meteo, OpenWeatherMap) in one call. See services.apis.locations.weather_resolution.",
    ),
    "redata_routing": ServiceDefaults(
        display_name="REData Routing",
        calls_per_minute=20,
        calls_per_day=None,
        notes="Route/drive-time legs via POST /routes/ (as_given capability only). See services.apis.locations.routing_resolution.",
    ),
    "openweathermap": ServiceDefaults(
        display_name="OpenWeatherMap",
        calls_per_minute=20,
        calls_per_day=500,
        notes="Free tier: 1,000 calls/day.",
    ),
    "overpass": ServiceDefaults(
        display_name="Overpass API (OpenStreetMap)",
        # OverpassGateway spreads every call across a pool of public instances
        # and drops any that error out of rotation until the next day, so this
        # limit governs our total load, not the load on any single instance.
        # Each logical lookup may spend more than one call when it fails over.
        calls_per_minute=240,
        calls_per_day=24_000,
        notes="Free API. Load is distributed across several public Overpass instances, and any instance that errors/times out is dropped until the next day. Each logical lookup may spend more than one call when it fails over.",
    ),
    "digital_commonwealth": ServiceDefaults(
        display_name="Digital Commonwealth",
        calls_per_minute=10,
        calls_per_day=200,
        usa_only=True,
        notes="Massachusetts-based digital archive. Free API.",
    ),
    "apple_maps": ServiceDefaults(
        display_name="Apple Maps Server API",
        calls_per_minute=50,
        calls_per_day=2500,
        notes="Requires a JWT generated from Apple Developer credentials. Geocoding/search is billable.",
    ),
    "google_earth": ServiceDefaults(
        display_name="Google Earth Engine",
        calls_per_minute=10,
        calls_per_day=200,
        notes="Requires OAuth2. Free for non-commercial use via Earth Engine sign-up.",
    ),
    "wayback_machine": ServiceDefaults(
        display_name="Internet Archive Wayback Machine",
        calls_per_minute=10,
        calls_per_day=500,
        notes="Free, no key required. Be polite - the Archive is a public resource.",
    ),
    "hibp": ServiceDefaults(
        display_name="Have I Been Pwned (Pwned Passwords)",
        calls_per_minute=60,
        calls_per_day=5000,
        notes="Free k-anonymity range API. Used when users set or change passwords.",
    ),
    "sms": ServiceDefaults(
        display_name="Twilio SMS",
        calls_per_minute=10,
        calls_per_day=200,
        notes="Billed per message sent - keep this conservative.",
    ),
    "whatsapp": ServiceDefaults(
        display_name="Twilio WhatsApp",
        calls_per_minute=10,
        calls_per_day=200,
        notes="Billed per message sent - keep this conservative.",
    ),
    "article_expansion": ServiceDefaults(
        display_name="Article Expansion Writing (AI)",
        calls_per_minute=10,
        calls_per_day=500,
        notes="Drafts plain-text paragraphs for pin/wiki articles from a linked page during AI link extraction. Cost varies by provider/model - see ApiCallLog.cost_estimate for actuals.",
    ),
    "article_safety": ServiceDefaults(
        display_name="Article Expansion Safety (AI)",
        calls_per_minute=20,
        calls_per_day=1000,
        notes="Judges AI-drafted article text for appropriateness and safety-related implications before it is appended. Fail-closed when unavailable.",
    ),
    "trivia_moderation": ServiceDefaults(
        display_name="Trivia Question Moderation (AI)",
        calls_per_minute=20,
        calls_per_day=1000,
        notes="Classifies user-submitted and AI-generated Trivia questions before they enter rotation. Cost varies by provider/model - see ApiCallLog.cost_estimate for actuals.",
    ),
    "trivia_generation": ServiceDefaults(
        display_name="Trivia Question Generation (AI)",
        calls_per_minute=5,
        calls_per_day=200,
        notes="Generates candidate Trivia questions from wiki article content. Runs from a scheduled background sweep, not per-request.",
    ),
    "trivia_answer_check": ServiceDefaults(
        display_name="Trivia Answer Checking (AI)",
        calls_per_minute=30,
        calls_per_day=2000,
        notes="Judges a non-exact-match Trivia answer as possibly correct but differently phrased. Only called on a normalized-string mismatch.",
    ),
    "trivia_wiki_incorporation": ServiceDefaults(
        display_name="Trivia Wiki Incorporation (AI)",
        calls_per_minute=5,
        calls_per_day=200,
        notes="Drafts a plain-text paragraph folding a well-upvoted user-submitted Trivia question into its location's wiki article. Runs from a scheduled background sweep, not per-request.",
    ),
}


def all_service_defaults() -> dict[str, ServiceDefaults]:
    """Every known service's default config: static registry plus plugins.

    Plugin-declared defaults win over a same-keyed ``SERVICE_REGISTRY`` entry
    so converting an integration to a plugin fully transfers ownership of its
    configuration.

    Returns:
        Mapping of service key to its :class:`ServiceDefaults`.
    """
    from urbanlens.dashboard.plugins import plugin_registry

    merged = dict(SERVICE_REGISTRY)
    merged.update(plugin_registry.service_defaults())
    return merged


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def get_limit_config(service: str) -> Any:
    """Return the ``ApiRateLimit`` row for ``service``, creating it if absent.

    Uses the merged :func:`all_service_defaults` (static registry plus
    plugin declarations) when creating a new row.

    Args:
        service: The service key (e.g. ``"nps"``).

    Returns:
        An ``ApiRateLimit`` instance.
    """
    from urbanlens.dashboard.models.api_rate_limit import ApiRateLimit

    defaults_entry = all_service_defaults().get(service)
    if defaults_entry:
        row, _ = ApiRateLimit.objects.get_or_create(
            service=service,
            defaults={
                "display_name": defaults_entry.display_name,
                "calls_per_minute": defaults_entry.calls_per_minute,
                "calls_per_day": defaults_entry.calls_per_day,
                "calls_per_30_days": defaults_entry.calls_per_30_days,
                "min_interval_seconds": defaults_entry.min_interval_seconds,
                "usa_only": defaults_entry.usa_only,
                "notes": defaults_entry.notes,
            },
        )
    else:
        row, _ = ApiRateLimit.objects.get_or_create(
            service=service,
            defaults={
                "display_name": service.replace("_", " ").title(),
                "calls_per_minute": 20,
                "calls_per_day": 500,
            },
        )
    return row


def service_is_permitted(service: str) -> bool:
    """
    Check if the service is enabled and not rate limited.

    Args:
        service: The service key.

    Returns:
        ``True`` if the service is enabled and not rate limited, ``False`` otherwise.
    """
    return service_is_enabled(service) and check_rate_limit(service)


def service_is_enabled(service: str) -> bool:
    """Check if the service is enabled.

    Args:
        service: The service key.

    Returns:
        ``True`` if the service is enabled, ``False`` otherwise.
    """
    try:
        config = get_limit_config(service)
    except Exception:
        # TODO: Catch specific exceptions
        logger.exception("Failed to read rate limit config for %s - allowing call", service)
        return False
    return config.enabled


def check_rate_limit(service: str) -> bool:
    """Return ``True`` if a call to ``service`` is currently permitted.

    Queries the ``ApiCallLog`` table using a rolling window to enforce the
    per-minute, per-day, and per-30-day limits configured in
    ``ApiRateLimit``.  A ``False`` result means the call should be skipped; a
    ``_RateLimitedSession`` will log the blocked attempt automatically.

    Args:
        service: The service key.

    Returns:
        ``True`` if the call is allowed, ``False`` if rate limited.
    """
    from urbanlens.dashboard.models.api_call_log import ApiCallLog

    try:
        config = get_limit_config(service)
    except DatabaseError:
        # Allow the call rather than break a feature when the database is the thing
        # that failed. Deliberately *not* a bare except: this limiter caps spend on
        # paid third-party APIs, so a bug here silently uncapping it - a broken plugin
        # rate-limit declaration, say - must surface rather than read as "allowed".
        logger.exception("Failed to read rate limit config for %s - allowing call", service)
        return True

    try:
        if config.calls_per_minute is not None:
            recent_minute = ApiCallLog.objects.for_service(service).since(timedelta(minutes=1)).billable().count()
            if recent_minute >= config.calls_per_minute:
                logger.warning(
                    "Rate limit hit for %s: %d/%d calls in last minute",
                    service,
                    recent_minute,
                    config.calls_per_minute,
                )
                return False

        if config.calls_per_day is not None:
            today_count = ApiCallLog.objects.for_service(service).today().billable().count()
            if today_count >= config.calls_per_day:
                logger.warning(
                    "Daily rate limit hit for %s: %d/%d calls today",
                    service,
                    today_count,
                    config.calls_per_day,
                )
                return False

        if config.calls_per_30_days is not None:
            recent_30_days = ApiCallLog.objects.for_service(service).since(timedelta(days=30)).billable().count()
            if recent_30_days >= config.calls_per_30_days:
                logger.warning(
                    "30-day rate limit hit for %s: %d/%d calls in the last 30 days",
                    service,
                    recent_30_days,
                    config.calls_per_30_days,
                )
                return False
    except DatabaseError:
        logger.exception("Failed to check rate limit counts for %s - allowing call", service)
        return True

    return True


def log_api_call(
    service: str,
    *,
    success: bool = True,
    response_ms: int | None = None,
    endpoint: str = "",
    was_rate_limited: bool = False,
    was_geo_filtered: bool = False,
    was_service_disabled: bool = False,
    cost_estimate: Decimal | None = None,
) -> None:
    """Record one API call in the ``ApiCallLog`` table.

    Failures are swallowed so that logging problems never break callers.

    Args:
        service: The service key.
        success: Whether the call succeeded (HTTP 2xx, no exception).
        response_ms: Round-trip time in milliseconds.
        endpoint: URL or endpoint path (truncated to 500 chars).
        was_rate_limited: True if the call was blocked by rate limiting.
        was_geo_filtered: True if the call was skipped due to geo filtering.
        cost_estimate: Estimated USD cost of this call, if known - see
            ``ServiceDefaults.cost_per_call``.
    """
    from urbanlens.dashboard.models.api_call_log import ApiCallLog

    try:
        ApiCallLog.objects.create(
            service=service,
            success=success,
            response_ms=response_ms,
            endpoint=endpoint[:500] if endpoint else "",
            was_rate_limited=was_rate_limited,
            was_geo_filtered=was_geo_filtered,
            was_service_disabled=was_service_disabled,
            cost_estimate=cost_estimate,
        )
    except Exception:
        logger.exception("Failed to log API call for service %s", service)


def _reserve_call(service: str, *, endpoint: str = "") -> int:
    """Atomically check ``service``'s rate limit and reserve a logged call slot.

    ``check_rate_limit`` (COUNT) and ``log_api_call`` (INSERT), called as two
    separate steps with no locking, let concurrent callers race: several
    requests can all pass the COUNT check before any of them has inserted a
    log row, letting a burst of calls through above the configured limit.
    This function closes that gap by locking the service's ``ApiRateLimit``
    row (the natural one-row-per-service counter for this domain) for the
    duration of the count check and the reservation insert, via
    ``select_for_update()`` inside ``transaction.atomic()`` - so a second
    concurrent caller for the same service blocks until the first has
    committed its reservation, and then sees it in its own count.

    A rolling-window budget alone still doesn't guarantee even spacing - all
    of a generous per-minute allowance can land in the same few seconds and
    still be "within budget". For a hard per-request spacing requirement
    like Nominatim's 1 req/second or GDELT's 1 req/5s, ``min_interval_seconds``
    is checked against ``last_call_at`` under the same lock, so it can't race
    with the count check above.

    The lock is held only for the check-and-insert - it is released as soon
    as this function returns, well before the actual outbound network
    request happens, so concurrent calls to *different* services (or calls
    that are ultimately blocked) are never serialized by it.

    The blocked/disabled branches below record their ``ApiCallLog`` row and
    then exit the ``atomic()`` block normally rather than raising from inside
    it - raising from inside would roll back that same transaction and take
    the just-written log row with it, silently losing every blocked-attempt
    record this function exists to produce.

    Args:
        service: The service key.
        endpoint: URL or endpoint path being requested, recorded on the
            reservation row (truncated to 500 chars).

    Returns:
        The pk of the reserved ``ApiCallLog`` row. Callers must update it via
        ``_finalize_call`` once the request completes.

    Raises:
        RateLimitExceededError: If the call would exceed the configured rate
            limit, or land sooner than ``min_interval_seconds`` after the
            last one. The blocked attempt is logged before raising.
        ServiceDisabledError: If the service is administratively disabled.
            The skipped attempt is logged before raising.
    """
    from urbanlens.dashboard.models.api_call_log import ApiCallLog
    from urbanlens.dashboard.models.api_rate_limit import ApiRateLimit

    truncated_endpoint = endpoint[:500] if endpoint else ""

    # Ensure the row exists (auto-created from defaults) before locking it -
    # get_or_create is safe to call outside the lock since it already handles
    # its own creation race.
    get_limit_config(service)

    to_raise: RequestCancelledError | None = None
    entry_pk = -1

    with transaction.atomic():
        config = ApiRateLimit.objects.select_for_update().get(service=service)

        if config.min_interval_seconds is not None and config.last_call_at is not None:
            elapsed = (timezone.now() - config.last_call_at).total_seconds()
            if elapsed < config.min_interval_seconds:
                logger.warning(
                    "Minimum interval not yet elapsed for %s: %.2fs since last call (need %.2fs)",
                    service,
                    elapsed,
                    config.min_interval_seconds,
                )
                ApiCallLog.objects.create(service=service, endpoint=truncated_endpoint, success=False, was_rate_limited=True)
                to_raise = RateLimitExceededError(service)

        if to_raise is None and not check_rate_limit(service):
            ApiCallLog.objects.create(service=service, endpoint=truncated_endpoint, success=False, was_rate_limited=True)
            to_raise = RateLimitExceededError(service)

        if to_raise is None and not service_is_enabled(service):
            ApiCallLog.objects.create(service=service, endpoint=truncated_endpoint, success=False, was_service_disabled=True)
            to_raise = ServiceDisabledError(service)

        if to_raise is None:
            entry = ApiCallLog.objects.create(service=service, endpoint=truncated_endpoint, success=True)
            entry_pk = entry.pk
            if config.min_interval_seconds is not None:
                config.last_call_at = timezone.now()
                config.save(update_fields=["last_call_at"])

    if to_raise is not None:
        raise to_raise
    return entry_pk


def _finalize_call(entry_pk: int, *, success: bool, response_ms: int | None = None, cost_estimate: Decimal | None = None) -> None:
    """Update a reservation row created by ``_reserve_call`` with the request's outcome.

    Updates the existing row in place rather than inserting a new one, so a
    reserved-but-not-yet-finalized call still counts toward
    ``check_rate_limit``'s window queries (which count rows regardless of
    ``success``) without double-counting once finalized.

    Failures are swallowed so that logging problems never break callers.

    Args:
        entry_pk: pk of the ``ApiCallLog`` row returned by ``_reserve_call``.
        success: Whether the call succeeded (HTTP 2xx, no exception).
        response_ms: Round-trip time in milliseconds.
        cost_estimate: Estimated USD cost of this call, if known - see
            ``ServiceDefaults.cost_per_call``.
    """
    from urbanlens.dashboard.models.api_call_log import ApiCallLog

    try:
        ApiCallLog.objects.filter(pk=entry_pk).update(success=success, response_ms=response_ms, cost_estimate=cost_estimate)
    except Exception:
        logger.exception("Failed to finalize API call log entry %s", entry_pk)


# ---------------------------------------------------------------------------
# Session wrapper
# ---------------------------------------------------------------------------


class _RateLimitedSession:
    """Wraps ``requests.Session`` to enforce rate limits and log every call.

    This is NOT a subclass of ``requests.Session`` - it delegates all
    attribute access to a real session so that caller code using
    ``self.session.get(...)`` continues to work unchanged.
    """

    def __init__(self, service_key: str) -> None:
        import requests

        self._service_key = service_key
        self._session = requests.Session()

    def __getattr__(self, name: str):
        return getattr(self._session, name)

    def get(self, url, **kwargs):
        """Rate-checked GET."""
        return self._do_request("GET", url, **kwargs)

    def post(self, url, **kwargs):
        """Rate-checked POST."""
        return self._do_request("POST", url, **kwargs)

    def put(self, url, **kwargs):
        """Rate-checked PUT."""
        return self._do_request("PUT", url, **kwargs)

    def patch(self, url, **kwargs):
        """Rate-checked PATCH."""
        return self._do_request("PATCH", url, **kwargs)

    def delete(self, url, **kwargs):
        """Rate-checked DELETE."""
        return self._do_request("DELETE", url, **kwargs)

    def request(self, method, url, **kwargs):
        """Rate-checked generic request."""
        return self._do_request(method, url, **kwargs)

    def _do_request(self, method: str, url: str, **kwargs):
        """Reserve a rate-limit slot, make the request, finalize the logged result.

        The reservation (see ``_reserve_call``) atomically checks the rate
        limit and logs the attempt in one locked transaction, so this
        no longer has a check-then-log gap for concurrent callers to race
        through.
        """
        entry_pk = _reserve_call(self._service_key, endpoint=str(url))

        # requests has no default timeout at all: a gateway call that forgets
        # timeout= would otherwise block its caller (and, when running under a
        # call_with_deadline guard, pin an executor slot) indefinitely. The
        # (connect, read) tuple bounds each phase separately; callers that pass
        # their own timeout are untouched, including long-running offline jobs.
        kwargs.setdefault("timeout", (5, 30))

        t0 = time.monotonic()
        try:
            resp = self._session.request(method, url, **kwargs)
            elapsed_ms = int((time.monotonic() - t0) * 1000)
            # Only a call that actually reached the provider and succeeded is
            # billable - a rate-limited/disabled call above never went out,
            # and a failed response wasn't necessarily charged either way, so
            # estimating a cost for it would overstate real spend.
            cost_estimate = all_service_defaults().get(self._service_key, ServiceDefaults(display_name="")).cost_per_call if resp.ok else None
            _finalize_call(entry_pk, success=resp.ok, response_ms=elapsed_ms, cost_estimate=cost_estimate)
            return resp
        except Exception:
            elapsed_ms = int((time.monotonic() - t0) * 1000)
            _finalize_call(entry_pk, success=False, response_ms=elapsed_ms)
            raise


class RequestCancelledError(DashboardError):
    """Raised when a request is cancelled.

    Args:
        service: The rate-limiter service key the cancelled request targeted.
        message: Optional message override for subclasses; without it, the
            subclass's formatted message would be mistaken for the service
            name and wrapped again (e.g. ``Request cancelled for service
            'Rate limit exceeded for service 'nps'''``).
    """

    def __init__(self, service: str, message: str | None = None) -> None:
        super().__init__(message or f"Request cancelled for service '{service}'")
        self.service = service


class RateLimitExceededError(RequestCancelledError):
    """Raised when a rate limit prevents an API call from proceeding."""

    def __init__(self, service: str) -> None:
        super().__init__(service, f"Rate limit exceeded for service '{service}'")


class ServiceDisabledError(RequestCancelledError):
    """Raised when a service is disabled."""

    def __init__(self, service: str) -> None:
        super().__init__(service, f"Service '{service}' is disabled")
