"""Rate limiting for external API calls.

Provides ``check_rate_limit`` and ``log_api_call`` helpers used by the
``_RateLimitedSession`` inside every ``Gateway`` subclass that declares a
``service_key``.  Configuration is persisted in ``ApiRateLimit`` rows, which
are auto-created on first access using the defaults in ``SERVICE_REGISTRY``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import timedelta
import logging
import time
from typing import Any

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Service registry — default config for every known external API service.
# Rows are auto-created from these defaults the first time a service is seen.
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ServiceDefaults:
    """Default rate-limit configuration for one external API service."""

    display_name: str
    calls_per_minute: int | None = 20
    calls_per_day: int | None = 500
    usa_only: bool = False
    notes: str = ""


SERVICE_REGISTRY: dict[str, ServiceDefaults] = {
    "google_places": ServiceDefaults(
        display_name="Google Places API",
        calls_per_minute=20,
        calls_per_day=200,
        notes="Free tier: $200/month credit. Geocoding/details billed per call.",
    ),
    "google_geocoding": ServiceDefaults(
        display_name="Google Geocoding API",
        calls_per_minute=20,
        calls_per_day=500,
        notes="Free tier: $200/month credit (~40,000 calls/month).",
    ),
    "google_maps": ServiceDefaults(
        display_name="Google Maps (Static/StreetView)",
        calls_per_minute=20,
        calls_per_day=200,
        notes="Static Maps: 25,000 free/month. Street View: billed per call.",
    ),
    "google_search": ServiceDefaults(
        display_name="Google Custom Search",
        calls_per_minute=10,
        calls_per_day=100,
        notes="CSE free tier: 100 queries/day hard limit.",
    ),
    "nps": ServiceDefaults(
        display_name="National Park Service API",
        calls_per_minute=10,
        calls_per_day=500,
        usa_only=True,
        notes="Free API. USA only — NPS covers US national parks exclusively.",
    ),
    "openweathermap": ServiceDefaults(
        display_name="OpenWeatherMap",
        calls_per_minute=20,
        calls_per_day=500,
        notes="Free tier: 1,000 calls/day.",
    ),
    "wikipedia": ServiceDefaults(
        display_name="Wikipedia",
        calls_per_minute=30,
        calls_per_day=2000,
        notes="Free API. Be polite — set a descriptive User-Agent.",
    ),
    "wikimedia": ServiceDefaults(
        display_name="Wikimedia Commons",
        calls_per_minute=30,
        calls_per_day=1000,
        notes="Free API.",
    ),
    "smithsonian": ServiceDefaults(
        display_name="Smithsonian Open Access",
        calls_per_minute=20,
        calls_per_day=500,
        usa_only=True,
        notes="Free API. USA-centric archive.",
    ),
    "loopnet": ServiceDefaults(
        display_name="LoopNet",
        calls_per_minute=5,
        calls_per_day=100,
        usa_only=True,
        notes="US commercial real estate. Scraped — be conservative to avoid blocking.",
    ),
    "nominatim": ServiceDefaults(
        display_name="Nominatim (OpenStreetMap)",
        calls_per_minute=1,
        calls_per_day=500,
        notes="Free API. Hard limit: 1 req/second per OSM ToS.",
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
    "datagov": ServiceDefaults(
        display_name="Data.gov",
        calls_per_minute=10,
        calls_per_day=500,
        usa_only=True,
        notes="US government open data. Free API.",
    ),
    "digital_commonwealth": ServiceDefaults(
        display_name="Digital Commonwealth",
        calls_per_minute=10,
        calls_per_day=200,
        usa_only=True,
        notes="Massachusetts-based digital archive. Free API.",
    ),
    "library_of_congress": ServiceDefaults(
        display_name="Library of Congress",
        calls_per_minute=10,
        calls_per_day=200,
        usa_only=True,
        notes="Free API. USA-centric archive.",
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
    "esri": ServiceDefaults(
        display_name="Esri ArcGIS REST",
        calls_per_minute=20,
        calls_per_day=500,
        notes="Public Esri basemap/wayback services. No key required.",
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
    "open_aerial_map": ServiceDefaults(
        display_name="OpenAerialMap",
        calls_per_minute=20,
        calls_per_day=500,
        notes="Free, no key required. Open licensed aerial imagery metadata.",
    ),
    "openhistoricalmap": ServiceDefaults(
        display_name="OpenHistoricalMap",
        calls_per_minute=1,
        calls_per_day=500,
        notes="Free, no key required. OSM-based historic map data. Nominatim: 1 req/second hard limit.",
    ),
    "usgs": ServiceDefaults(
        display_name="USGS EarthExplorer / TNM",
        calls_per_minute=10,
        calls_per_day=500,
        usa_only=True,
        notes="M2M requires an applicationToken from EarthExplorer account settings. TNM is fully public.",
    ),
    "wayback_machine": ServiceDefaults(
        display_name="Internet Archive Wayback Machine",
        calls_per_minute=10,
        calls_per_day=500,
        notes="Free, no key required. Be polite — the Archive is a public resource.",
    ),
    "mapbox": ServiceDefaults(
        display_name="Mapbox",
        calls_per_minute=20,
        calls_per_day=500,
        notes="Requires a Mapbox public access token. Static Images API has a free tier.",
    ),
    "bing_maps": ServiceDefaults(
        display_name="Bing Maps",
        calls_per_minute=20,
        calls_per_day=500,
        notes="Requires a Bing Maps key from Azure portal. Static imagery has a free tier.",
    ),
    "mapillary": ServiceDefaults(
        display_name="Mapillary",
        calls_per_minute=20,
        calls_per_day=1000,
        notes="Requires a client access token from mapillary.com/dashboard/developers. Free tier available.",
    ),
    "kartaview": ServiceDefaults(
        display_name="KartaView",
        calls_per_minute=20,
        calls_per_day=500,
        notes="Free, no key required. Crowdsourced street-level imagery.",
    ),
}


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def get_limit_config(service: str) -> Any:
    """Return the ``ApiRateLimit`` row for ``service``, creating it if absent.

    Uses the ``SERVICE_REGISTRY`` defaults when creating a new row.

    Args:
        service: The service key (e.g. ``"nps"``).

    Returns:
        An ``ApiRateLimit`` instance.
    """
    from urbanlens.dashboard.models.api_rate_limit import ApiRateLimit

    defaults_entry = SERVICE_REGISTRY.get(service)
    if defaults_entry:
        row, _ = ApiRateLimit.objects.get_or_create(
            service=service,
            defaults={
                "display_name": defaults_entry.display_name,
                "calls_per_minute": defaults_entry.calls_per_minute,
                "calls_per_day": defaults_entry.calls_per_day,
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


def check_rate_limit(service: str) -> bool:
    """Return ``True`` if a call to ``service`` is currently permitted.

    Queries the ``ApiCallLog`` table using a rolling window to enforce the
    per-minute and per-day limits configured in ``ApiRateLimit``.  A ``False``
    result means the call should be skipped; a ``_RateLimitedSession`` will
    log the blocked attempt automatically.

    Args:
        service: The service key.

    Returns:
        ``True`` if the call is allowed, ``False`` if rate limited.
    """
    from urbanlens.dashboard.models.api_call_log import ApiCallLog

    try:
        config = get_limit_config(service)
    except Exception:
        logger.exception("Failed to read rate limit config for %s — allowing call", service)
        return True

    if not config.enabled:
        return False

    try:
        if config.calls_per_minute is not None:
            recent_minute = (
                ApiCallLog.objects.for_service(service)
                .since(timedelta(minutes=1))
                .exclude(was_geo_filtered=True)
                .count()
            )
            if recent_minute >= config.calls_per_minute:
                logger.warning(
                    "Rate limit hit for %s: %d/%d calls in last minute",
                    service, recent_minute, config.calls_per_minute,
                )
                return False

        if config.calls_per_day is not None:
            today_count = (
                ApiCallLog.objects.for_service(service)
                .today()
                .exclude(was_geo_filtered=True)
                .count()
            )
            if today_count >= config.calls_per_day:
                logger.warning(
                    "Daily rate limit hit for %s: %d/%d calls today",
                    service, today_count, config.calls_per_day,
                )
                return False
    except Exception:
        logger.exception("Failed to check rate limit counts for %s — allowing call", service)
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
        )
    except Exception:
        logger.exception("Failed to log API call for service %s", service)


# ---------------------------------------------------------------------------
# Session wrapper
# ---------------------------------------------------------------------------

class _RateLimitedSession:
    """Wraps ``requests.Session`` to enforce rate limits and log every call.

    This is NOT a subclass of ``requests.Session`` — it delegates all
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

        t0 = time.monotonic()
        try:
            resp = self._session.request(method, url, **kwargs)
            elapsed_ms = int((time.monotonic() - t0) * 1000)
            log_api_call(
                self._service_key,
                success=resp.ok,
                response_ms=elapsed_ms,
                endpoint=str(url),
            )
            return resp
        except RateLimitExceededError:
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


class RateLimitExceededError(Exception):
    """Raised when a rate limit prevents an API call from proceeding."""

    def __init__(self, service: str) -> None:
        super().__init__(f"Rate limit exceeded for service '{service}'")
        self.service = service
