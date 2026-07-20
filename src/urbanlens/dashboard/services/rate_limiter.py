"""Rate limiting for external API calls.

Provides ``check_rate_limit`` and ``log_api_call`` helpers used by the
``_RateLimitedSession`` inside every ``Gateway`` subclass that declares a
``service_key``.  Configuration is persisted in ``ApiRateLimit`` rows, which
are auto-created on first access using the defaults in ``SERVICE_REGISTRY``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import timedelta
from decimal import Decimal
import logging
import time
from typing import Any

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
        notes="Free tier: $200/month credit (~40,000 calls/month).",
        # Google's published rate is $5/1000 requests, consistent with this
        # entry's own $200-credit/~40,000-calls note (200/40000 = 0.005).
        cost_per_call=Decimal("0.005"),
    ),
    "google_search": ServiceDefaults(
        display_name="Google Custom Search",
        calls_per_minute=10,
        calls_per_day=100,
        notes="CSE free tier: 100 queries/day hard limit.",
    ),
    "openweathermap": ServiceDefaults(
        display_name="OpenWeatherMap",
        calls_per_minute=20,
        calls_per_day=500,
        notes="Free tier: 1,000 calls/day.",
    ),
    "overpass": ServiceDefaults(
        display_name="Overpass API (OpenStreetMap)",
        calls_per_minute=2,
        calls_per_day=500,
        notes="Free API. Be conservative; public Overpass instances are shared community infrastructure.",
    ),
    "brave_search": ServiceDefaults(
        display_name="Brave Search API",
        calls_per_minute=10,
        calls_per_day=200,
        notes="Free tier: 2,000 queries/month.",
    ),
    "digital_commonwealth": ServiceDefaults(
        display_name="Digital Commonwealth",
        calls_per_minute=10,
        calls_per_day=200,
        usa_only=True,
        notes="Massachusetts-based digital archive. Free API.",
    ),
    "routexl": ServiceDefaults(
        display_name="RouteXL",
        calls_per_minute=5,
        calls_per_day=50,
        notes="Route optimisation. Usage may be billed.",
    ),
    "news": ServiceDefaults(
        display_name="News API",
        calls_per_minute=10,
        calls_per_day=100,
        notes="Free tier varies by provider.",
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
    "openhistoricalmap": ServiceDefaults(
        display_name="OpenHistoricalMap",
        calls_per_minute=1,
        calls_per_day=500,
        notes="Free, no key required. OSM-based historic map data. Nominatim: 1 req/second hard limit.",
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
    except Exception:
        # TODO: Catch specific exceptions
        logger.exception("Failed to read rate limit config for %s - allowing call", service)
        return True

    try:
        if config.calls_per_minute is not None:
            recent_minute = ApiCallLog.objects.for_service(service).since(timedelta(minutes=1)).exclude(was_geo_filtered=True).count()
            if recent_minute >= config.calls_per_minute:
                logger.warning(
                    "Rate limit hit for %s: %d/%d calls in last minute",
                    service,
                    recent_minute,
                    config.calls_per_minute,
                )
                return False

        if config.calls_per_day is not None:
            today_count = ApiCallLog.objects.for_service(service).today().exclude(was_geo_filtered=True).count()
            if today_count >= config.calls_per_day:
                logger.warning(
                    "Daily rate limit hit for %s: %d/%d calls today",
                    service,
                    today_count,
                    config.calls_per_day,
                )
                return False

        if config.calls_per_30_days is not None:
            recent_30_days = ApiCallLog.objects.for_service(service).since(timedelta(days=30)).exclude(was_geo_filtered=True).count()
            if recent_30_days >= config.calls_per_30_days:
                logger.warning(
                    "30-day rate limit hit for %s: %d/%d calls in the last 30 days",
                    service,
                    recent_30_days,
                    config.calls_per_30_days,
                )
                return False
    except Exception:
        # TODO: Catch specific exceptions
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
        """Check rate limit, make the request, log the result."""
        if not check_rate_limit(self._service_key):
            log_api_call(
                self._service_key,
                success=False,
                endpoint=str(url),
                was_rate_limited=True,
            )
            raise RateLimitExceededError(self._service_key)

        if not service_is_enabled(self._service_key):
            log_api_call(
                self._service_key,
                success=False,
                endpoint=str(url),
                was_service_disabled=True,
            )
            raise ServiceDisabledError(self._service_key)

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
            log_api_call(
                self._service_key,
                success=resp.ok,
                response_ms=elapsed_ms,
                endpoint=str(url),
                cost_estimate=cost_estimate,
            )
            return resp
        except RateLimitExceededError:
            log_api_call(
                self._service_key,
                success=False,
                endpoint=str(url),
                was_rate_limited=True,
            )
            raise
        except Exception:
            elapsed_ms = int((time.monotonic() - t0) * 1000)
            log_api_call(
                self._service_key,
                success=False,
                response_ms=elapsed_ms,
                endpoint=str(url),
            )
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
