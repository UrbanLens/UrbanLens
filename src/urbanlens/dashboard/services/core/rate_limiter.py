"""
Rate limiting for external API calls.
``_RateLimitedSession`` closes this by going through ``_reserve_call``/``_finalize_call`` instead of calling ``check_rate_limit`` and ``log_api_call`` directly - see their docstrings."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator

    import requests

from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from datetime import timedelta
from enum import StrEnum
import logging
import math
import time
from typing import Any, ClassVar

from django.db import DatabaseError, transaction
from django.utils import timezone

from urbanlens.dashboard.exceptions import DashboardError
from urbanlens.dashboard.models.abstract.versioning import current_write_actor
from urbanlens.dashboard.services.core.gateway import GatewayRateLimitedError, GatewayRequestError, UpstreamBusyError
from urbanlens.dashboard.services.core.outages import is_unanswered_status, record_unanswered
from urbanlens.UrbanLens.egress import EgressCategory, scaled_limit

logger = logging.getLogger(__name__)

_EXTERNAL_CALLS_FORBIDDEN: ContextVar[bool] = ContextVar("external_calls_forbidden", default=False)


class ExternalCallForbiddenError(RuntimeError):
    """
    A gateway request was attempted inside :func:`external_calls_forbidden`.

    Not a :class:`GatewayRequestError`, so a caller that degrades quietly on an upstream failure does not mistake it
    for one.
    """


@contextmanager
def external_calls_forbidden() -> Iterator[None]:
    """Refuse every gateway request made for the duration, raising :class:`ExternalCallForbiddenError` before it goes out."""
    token = _EXTERNAL_CALLS_FORBIDDEN.set(True)
    try:
        yield
    finally:
        _EXTERNAL_CALLS_FORBIDDEN.reset(token)


# Service registry - default config for external API services that have not yet been converted to
# plugins.
# Plugin-provided integrations declare their defaults by contributing to ``SERVICE_DEFAULTS``
# instead; the merged view lives in ``all_service_defaults``.


class CallLedger(StrEnum):
    """How a service's calls are checked against its limits and recorded."""

    #: One ``ApiCallLog`` row per call, reserved under a lock on the service's ``ApiRateLimit`` row, so the check
    #: and the record are one step.
    ROW_PER_CALL = "row_per_call"
    #: Counted in the shared cache and rolled up into ``ApiCallLog`` every minute (``services.core.call_tally``), so
    #: a call waits on no query and no lock. For a high-frequency service: its windows are fixed rather than rolling.
    TALLIED = "tallied"


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

    #: Whether a call here can cost money.
    #: A new service is treated as billable until someone reads the provider's terms and says
    #: otherwise here, citing them in ``notes``.
    billable: bool = True
    #: The vendor's whole free allowance for one calendar month. Every deployment billed on the
    #: same account draws on it - UrbanLens's production, staging and development, and any other service billed to it.
    #: What one deployment may spend is :func:`free_tier_ceiling`. Read from code on every check,
    #: never from the admin-editable row: raising it is a bill, not a policy choice.
    free_tier_per_calendar_month: int | None = None
    #: UrbanLens's part of :attr:`free_tier_per_calendar_month` across all its deployments: at most
    #: 0.4 of a Google Maps Platform SKU another service on the account can also bill (that service takes 0.5), never above 0.9,
    #: so the hours between a UTC month turning and Google's Pacific one fall in headroom.
    free_tier_allotment: float = 0.9
    #: What kind of service this is, which decides which environments may call it and on what share of
    #: its budget (``urbanlens.UrbanLens.egress``). None means unclassified, which the policy treats as
    #: billed and the completeness test refuses.
    category: EgressCategory | None = None
    #: How its calls are counted and recorded.
    ledger: CallLedger = CallLedger.ROW_PER_CALL
    #: The tab this service sits under on the site-admin API-limits page; "Other" when None.
    group: str | None = None


SERVICE_REGISTRY: dict[str, ServiceDefaults] = {
    "google_geocoding": ServiceDefaults(
        display_name="Google Geocoding API",
        category=EgressCategory.BILLED,
        calls_per_minute=20,
        calls_per_day=500,
        calls_per_30_days=9999,
        # Geocoding is 10,000/month free, shared with the account's other services. Legacy Place Details `cid:` lookup is counted here
        # but Google bills it as Places Details (Pro, 5,000 free). This ceiling keeps that SKU free:
        # 0.4 x 10,000 here + google_places' 0.4 x 1,000 = 4,400 of 5,000. Holds by arithmetic, not design.
        free_tier_per_calendar_month=10_000,
        free_tier_allotment=0.4,
        notes="Free tier: 10,000 calls/month for Geocoding, shared with whatever else bills to the same account. The legacy Place Details cid: lookup is counted here too, though Google bills it as Places Details (Pro, 5,000 free a month).",
    ),
    "basemap_vendor_tiles": ServiceDefaults(
        display_name="Basemap Vendor Tiles",
        category=EgressCategory.QUOTA,
        # One per pan rather than one per user action, so sized for tile traffic
        # rather than a lookup allowance. These vendors are free and keyless, so the budget is
        # politeness rather than a billing guard.
        calls_per_minute=600,
        calls_per_day=None,
        notes="Raster basemap tiles fetched straight from the vendor - see services.map.basemap_vendors.",
        # One call per uncached tile, a viewport's worth at once: a locked row per tile queued them on the row lock.
        ledger=CallLedger.TALLIED,
    ),
    "google_open_buildings": ServiceDefaults(
        display_name="Google Open Buildings",
        category=EgressCategory.QUOTA,
        calls_per_minute=20,
        calls_per_day=500,
        notes="Downloads level-6 gzip CSV shards of Google's public Open Buildings v3 dataset during boundary lookups, only for cells it covers (none in the US), skipping any past MAX_SHARD_BYTES and remembering missing ones. The dataset has no quota, so this bounds our own bandwidth; the values are the generic fallback's, not tuned. See services.apis.locations.boundaries.google_open_buildings.",
    ),
    "microsoft_building_footprints": ServiceDefaults(
        display_name="Microsoft Building Footprints",
        category=EgressCategory.QUOTA,
        calls_per_minute=20,
        calls_per_day=500,
        notes="Downloads whole gzip shards of Microsoft's public building footprint dataset during boundary lookups. The dataset has no quota, so this bounds our own bandwidth; the values are the generic fallback's, not tuned. See services.apis.locations.boundaries.microsoft_buildings.",
    ),
    "overture_maps": ServiceDefaults(
        display_name="Overture Maps",
        category=EgressCategory.QUOTA,
        # Unlike its open-building-footprint siblings, Overture's STAC index returns 429 under load.
        # Values are a real budget, not bandwidth hygiene; still generic fallback numbers (no published quota).
        calls_per_minute=20,
        calls_per_day=500,
        notes="Overture's public GeoParquet release via services.apis.locations.boundaries.overture_maps, read only where no mirror covers the point. Free public dataset, but its STAC index rate-limits us under load.",
        billable=False,
    ),
    "openweathermap": ServiceDefaults(
        display_name="OpenWeatherMap",
        category=EgressCategory.QUOTA,
        calls_per_minute=20,
        calls_per_day=500,
        notes="Free tier: 1,000 calls/day.",
        # Free per this entry's own notes; see `ServiceDefaults.billable`.
        billable=False,
    ),
    "overpass": ServiceDefaults(
        display_name="Overpass API (OpenStreetMap)",
        # The deployment's own Overpass instance (UL_OVERPASS_API_URL); the public instances it fails over to are
        # budgeted separately, as overpass_public_mirror.
        category=EgressCategory.INTERNAL,
        calls_per_minute=240,
        calls_per_day=24_000,
        notes="Free: this deployment's own Overpass instance (UL_OVERPASS_API_URL). An endpoint that errors or times out is dropped for minutes, longer on each repeat, or for the wait it states, and each logical lookup may spend more than one call when it fails over.",
        # Free per this entry's own notes; see `ServiceDefaults.billable`.
        billable=False,
    ),
    "overpass_public_mirror": ServiceDefaults(
        display_name="Overpass API public mirrors (overpass-api.de, maps.mail.ru)",
        category=EgressCategory.QUOTA,
        # Every Overpass lookup when no primary is configured, and the fallback when it is down. The public instances
        # allow a few concurrent slots and about 10,000 queries a day per address, which other software on the same
        # address also spends.
        calls_per_minute=10,
        calls_per_day=2_000,
        notes="Free API: the public Overpass instances, asked when no primary is configured or it is down. Their limits are per address, shared with everything else calling them from it.",
        billable=False,
    ),
    "apple_maps": ServiceDefaults(
        display_name="Apple Maps Server API",
        category=EgressCategory.BILLED,
        calls_per_minute=50,
        calls_per_day=2500,
        notes="Requires a JWT generated from Apple Developer credentials. Geocoding/search is billable.",
    ),
    "google_earth": ServiceDefaults(
        display_name="Google Earth Engine",
        category=EgressCategory.BILLED,
        calls_per_minute=10,
        calls_per_day=200,
        notes="Requires OAuth2. Free for non-commercial use via Earth Engine sign-up.",
    ),
    "wayback_machine": ServiceDefaults(
        display_name="Internet Archive Wayback Machine",
        category=EgressCategory.QUOTA,
        calls_per_minute=10,
        calls_per_day=500,
        notes="Free, no key required. Be polite - the Archive is a public resource.",
        # Free per this entry's own notes; see `ServiceDefaults.billable`.
        billable=False,
    ),
    "hibp": ServiceDefaults(
        display_name="Have I Been Pwned (Pwned Passwords)",
        category=EgressCategory.QUOTA,
        calls_per_minute=60,
        calls_per_day=5000,
        notes="Free k-anonymity range API. Used when users set or change passwords.",
        # Free per this entry's own notes; see `ServiceDefaults.billable`.
        billable=False,
    ),
    "virustotal": ServiceDefaults(
        display_name="VirusTotal",
        category=EgressCategory.QUOTA,
        # Both capped below that (not merely at it) on purpose: check_rate_limit's rolling window is
        # ours, not VirusTotal's, so a call that lands right at our own ceiling isn't guaranteed to
        # land inside VirusTotal's - clock skew or window-boundary misalignment could still trip
        calls_per_minute=3,
        calls_per_day=480,
        notes=("Free public API tier, hash-lookup only. Fast path before ClamAV on externally-fetched image assets - never sent a user upload or a user's own cloud photo library. See services.security.virustotal_scan."),
    ),
    "sms": ServiceDefaults(
        display_name="Twilio SMS",
        category=EgressCategory.MESSAGING,
        calls_per_minute=10,
        calls_per_day=200,
        notes="Billed per message sent - keep this conservative.",
    ),
    "whatsapp": ServiceDefaults(
        display_name="Twilio WhatsApp",
        category=EgressCategory.MESSAGING,
        calls_per_minute=10,
        calls_per_day=200,
        notes="Billed per message sent - keep this conservative.",
    ),
}


def all_service_defaults() -> dict[str, ServiceDefaults]:
    """
    Every known service's default config: static registry plus plugins.
    Plugin-declared defaults win over a same-keyed ``SERVICE_REGISTRY`` entry so converting an integration to a plugin fully transfers ownership of its configuration.

    Returns:
        Mapping of service key to its :class:`ServiceDefaults`."""
    from urbanlens.dashboard.plugins.points import SERVICE_DEFAULTS

    merged = dict(SERVICE_REGISTRY)
    merged.update(SERVICE_DEFAULTS.as_dict())
    return merged


# Public API


def _service_share(service: str) -> float:
    """This deployment's share of *service*'s budgets (``services.core.egress.service_share``)."""
    from urbanlens.dashboard.services.core.egress import service_share

    return service_share(service)


def free_tier_ceiling(service: str) -> int | None:
    """
    How many calls to *service* this deployment may make this calendar month, or None.

    Args:
        service: The service key.

    Returns:
        The vendor's free allowance, times UrbanLens's allotment, times this deployment's share of
        the service (``UL_ENVIRONMENT_SHARE`` or its override), rounded down; None for a service
        with no declared free tier.
    """
    try:
        # Plugin defaults win, as all_service_defaults documents.
        defaults = all_service_defaults().get(service)
    except Exception:
        logger.exception("Could not read plugin service defaults for %s; the core registry decides its free tier", service)
        defaults = SERVICE_REGISTRY.get(service)
        if defaults is None:
            # A plugin's service whose free tier cannot be read spends none of it.
            return 0
    if defaults is None or defaults.free_tier_per_calendar_month is None:
        return None
    return math.floor(defaults.free_tier_per_calendar_month * defaults.free_tier_allotment * _service_share(service))


def call_ledger(service: str) -> CallLedger:
    """
    How *service*'s calls are counted and recorded.

    Args:
        service: The service key.

    Returns:
        Its declared ledger; one row per call for a service nothing declares, or whose defaults cannot be read.
    """
    try:
        defaults = all_service_defaults().get(service)
    except Exception:
        logger.exception("Could not read plugin service defaults for %s; the core registry decides its ledger", service)
        defaults = SERVICE_REGISTRY.get(service)
    return defaults.ledger if defaults is not None else CallLedger.ROW_PER_CALL


def _calls_in(rows: Any, service: str) -> int:
    """How many calls *rows* stand for: one each, except a tallied service's rolled-up rows, which carry a count."""
    if call_ledger(service) is CallLedger.TALLIED:
        from django.db.models import Sum

        return int(rows.aggregate(total=Sum("calls"))["total"] or 0)
    return int(rows.count())


def _is_billable(service: str) -> bool:
    """Whether a call to *service* can cost money; unknown services are assumed to."""
    defaults = SERVICE_REGISTRY.get(service)
    if defaults is None:
        try:
            defaults = all_service_defaults().get(service)
        except Exception:
            logger.exception("Could not read plugin service defaults for %s", service)
    return defaults is None or defaults.billable


def _refuse_if_billable(service: str, what: str, error: BaseException) -> bool:
    """
    The answer to "may this call go ahead" when *what* could not be read.

    Refused for anything that can cost money: this limiter is the only cap on spend at paid
    third-party APIs, and the database being unreadable is exactly when nobody is watching it.

    Args:
        service: The service key.
        what: What could not be read, for the log line.
        error: The failure, logged with its traceback.
    """
    billable = _is_billable(service)
    logger.error("Failed to read %s for %s - %s the call (billable=%s)", what, service, "refusing" if billable else "allowing", billable, exc_info=error)
    return not billable


def _fallback_values(service: str) -> dict[str, Any]:
    """The limits a service with no registered defaults gets."""
    return {"display_name": service.replace("_", " ").title(), "calls_per_minute": 20, "calls_per_day": 500}


def _still_at_fallback(row: Any, service: str) -> bool:
    """Whether ``row`` holds exactly what the fallback created it with, so nobody has chosen its limits."""
    untouched = {**_fallback_values(service), "calls_per_30_days": None, "min_interval_seconds": None, "usa_only": False, "notes": ""}
    return all(getattr(row, field) == value for field, value in untouched.items())


def get_limit_config(service: str) -> Any:
    """
    Return the ``ApiRateLimit`` row for ``service``, creating it if absent.
    A row still holding the generic fallback takes the service's registered defaults once there are some; ``enabled`` is never touched.
    A row holding a service's own defaults is never rewritten here, since an admin may have chosen them: a changed default
    reaches existing rows only through a data migration that touches rows still holding an old default exactly (0064 for 0.8.0's,
    0068 for every earlier release's).

    Args:
        service: The service key (e.g. ``"google_places"``).

    Returns:
        An ``ApiRateLimit`` instance."""
    from urbanlens.dashboard.models.api_rate_limit import ApiRateLimit

    defaults_entry = all_service_defaults().get(service)
    if defaults_entry:
        values = {
            "display_name": defaults_entry.display_name,
            "calls_per_minute": defaults_entry.calls_per_minute,
            "calls_per_day": defaults_entry.calls_per_day,
            "calls_per_30_days": defaults_entry.calls_per_30_days,
            "min_interval_seconds": defaults_entry.min_interval_seconds,
            "usa_only": defaults_entry.usa_only,
            "notes": defaults_entry.notes,
        }
        row, created = ApiRateLimit.objects.get_or_create(service=service, defaults=values)
        if not created and _still_at_fallback(row, service) and any(getattr(row, field) != value for field, value in values.items()):
            for field, value in values.items():
                setattr(row, field, value)
            row.save(update_fields=[*values, "updated"])
    else:
        row, _ = ApiRateLimit.objects.get_or_create(service=service, defaults=_fallback_values(service))
    return row


def service_is_permitted(service: str) -> bool:
    """
    Check if the service is enabled and not rate limited.

    Args:
        service: The service key.

    Returns:
        ``True`` if the service is enabled and not rate limited, ``False`` otherwise."""
    return service_is_enabled(service) and check_rate_limit(service)


def service_is_enabled(service: str, config: Any = None) -> bool:
    """
    Check if the service is enabled.

    Args:
        service: The service key.
        config: An already-loaded ``ApiRateLimit`` row for ``service``, when the caller has one.

    Returns:
        ``True`` if the service is enabled, ``False`` otherwise."""
    # Checked before config/cached-config so callers asking ahead of time get the refusal.
    # _reserve_call raises the environment's refusal first; this answers pre-check callers.
    from urbanlens.dashboard.services.core.egress import egress_permitted

    if not egress_permitted(service):
        return False
    if config is not None:
        return bool(config.enabled)
    try:
        config = get_limit_config(service)
    except DatabaseError:
        # Reports the service as disabled, which refuses the call - the opposite of
        # check_rate_limit's choice, and deliberate: "is this service switched on" has no safe
        # affirmative answer when it cannot be read.
        logger.exception("Failed to read rate limit config for %s - treating the service as disabled", service)
        return False
    return config.enabled


def check_rate_limit(service: str, config: Any = None) -> bool:
    """
    Return ``True`` if a call to ``service`` is currently permitted.
    Only the windows that are actually configured are counted - a service with no ``calls_per_30_days`` never pays for that ``COUNT(*)``.
    Each window holds this deployment's share of the configured limit (``services.core.egress.service_share``): all of
    it for a first-party service, our own hosts and a ``deployed`` service (where one is called at all), ``UL_ENVIRONMENT_SHARE`` of it for a ``quota`` or
    ``billed`` service.

    Args:
        service: The service key.
        config: An already-loaded ``ApiRateLimit`` row, when the caller has one (see :func:`service_is_enabled` for why).

    Returns:
        ``True`` if the call is allowed, ``False`` if rate limited."""
    from urbanlens.dashboard.models.api_call_log import ApiCallLog

    if config is None:
        try:
            config = get_limit_config(service)
        except DatabaseError as exc:
            return _refuse_if_billable(service, "the rate limit config", exc)

    share = _service_share(service)
    per_minute = scaled_limit(config.calls_per_minute, share)
    per_day = scaled_limit(config.calls_per_day, share)
    per_30_days = scaled_limit(config.calls_per_30_days, share)
    try:
        if per_minute is not None:
            recent_minute = _calls_in(ApiCallLog.objects.for_service(service).since(timedelta(minutes=1)).billable(), service)
            if recent_minute >= per_minute:
                logger.warning(
                    "Rate limit hit for %s: %d/%d calls in last minute",
                    service,
                    recent_minute,
                    per_minute,
                )
                return False

        if per_day is not None:
            today_count = _calls_in(ApiCallLog.objects.for_service(service).today().billable(), service)
            if today_count >= per_day:
                logger.warning(
                    "Daily rate limit hit for %s: %d/%d calls today",
                    service,
                    today_count,
                    per_day,
                )
                return False

        if per_30_days is not None:
            recent_30_days = _calls_in(ApiCallLog.objects.for_service(service).since(timedelta(days=30)).billable(), service)
            if recent_30_days >= per_30_days:
                logger.warning(
                    "30-day rate limit hit for %s: %d/%d calls in the last 30 days",
                    service,
                    recent_30_days,
                    per_30_days,
                )
                return False

        free_tier = free_tier_ceiling(service)
        if free_tier is not None:
            this_month = _calls_in(ApiCallLog.objects.for_service(service).this_calendar_month().billable(), service)
            if this_month >= free_tier:
                logger.warning("Free-tier ceiling reached for %s: %d/%d calls this calendar month - refusing to spend past it", service, this_month, free_tier)
                return False
    except DatabaseError as exc:
        return _refuse_if_billable(service, "the rate limit counts", exc)

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
    status_code: int | None = None,
    was_rejected_input: bool = False,
) -> int | None:
    """
    Record one API call in the ``ApiCallLog`` table.
    Failures are swallowed so that logging problems never break callers.

    Args:
        service: The service key.
        success: Whether the call succeeded (HTTP 2xx, no exception).
        response_ms: Round-trip time in milliseconds.
        endpoint: URL or endpoint path (truncated to 500 chars).
        was_rate_limited: True if the call was blocked by rate limiting.
        was_geo_filtered: True if the call was skipped due to geo filtering.
        status_code: The upstream's HTTP status, when the caller holds the response.
        was_rejected_input: True if the call was refused before it was made because its input could not return data.

    Returns:
        The pk of the row written, or None when the call was tallied or the row could not be written.
    """
    from urbanlens.dashboard.models.api_call_log import ApiCallLog

    if call_ledger(service) is CallLedger.TALLIED:
        from urbanlens.dashboard.services.core import call_tally

        outcome = (
            call_tally.Tallied.RATE_LIMITED
            if was_rate_limited
            else call_tally.Tallied.SERVICE_DISABLED
            if was_service_disabled
            else call_tally.Tallied.GEO_FILTERED
            if was_geo_filtered
            else call_tally.Tallied.REJECTED_INPUT
            if was_rejected_input
            else call_tally.Tallied.MADE
        )
        call_tally.record(service, outcome, endpoint=endpoint, success=success, status_code=status_code, response_ms=response_ms)
        return None

    try:
        entry = ApiCallLog.objects.create(
            service=service,
            profile_id=current_write_actor(),
            success=success,
            response_ms=response_ms,
            endpoint=endpoint[:500] if endpoint else "",
            was_rate_limited=was_rate_limited,
            was_geo_filtered=was_geo_filtered,
            was_service_disabled=was_service_disabled,
            status_code=status_code,
            was_rejected_input=was_rejected_input,
        )
    except Exception:
        logger.exception("Failed to log API call for service %s", service)
        return None
    return entry.pk


def _reserve_call(service: str, *, endpoint: str = "") -> int:
    """
    Atomically check ``service``'s rate limit and reserve a logged call slot.

    Args:
        service: The service key.
        endpoint: URL or endpoint path being requested, recorded on the reservation row (truncated to 500 chars).

    Returns:
        The pk of the reserved ``ApiCallLog`` row.

    Raises:
        EnvironmentRefusedError: If this environment does not call the service; nothing is recorded.
        RateLimitExceededError: If the call would exceed the configured rate limit, or land sooner than ``min_interval_seconds`` after the last one.
        ServiceDisabledError: If the service is administratively disabled.
        RateLimiterUnavailableError: If the limit cannot be read or the call cannot be recorded."""
    from urbanlens.dashboard.models.api_call_log import ApiCallLog
    from urbanlens.dashboard.models.api_rate_limit import ApiRateLimit
    from urbanlens.dashboard.services.core.egress import require_egress

    # Before the transaction: a call this environment never makes is not a call, and leaves no row.
    require_egress(service)
    truncated_endpoint = endpoint[:500] if endpoint else ""
    # Read once, outside the lock: who this call is being made for. Bound by
    # WriteSourceMiddleware for a request and left unset for the site's own
    # scheduled work, which belongs to nobody.
    actor_id = current_write_actor()

    to_raise: RequestCancelledError | None = None
    entry_pk: int

    try:
        with transaction.atomic():
            # Ensure the row exists (auto-created from defaults) before locking it - get_or_create is safe
            # to call outside the lock since it already handles its own creation race.
            # Its result is deliberately discarded: the row must be re-read under the lock below, and that
            # locked instance is then threaded into check_rate_limit/service_is_enabled rather than each
            get_limit_config(service)
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
                    ApiCallLog.objects.create(service=service, profile_id=actor_id, endpoint=truncated_endpoint, success=False, was_rate_limited=True)
                    to_raise = RateLimitExceededError(service)

            if to_raise is None and not check_rate_limit(service, config):
                ApiCallLog.objects.create(service=service, profile_id=actor_id, endpoint=truncated_endpoint, success=False, was_rate_limited=True)
                to_raise = RateLimitExceededError(service)

            if to_raise is None and not service_is_enabled(service, config):
                ApiCallLog.objects.create(service=service, profile_id=actor_id, endpoint=truncated_endpoint, success=False, was_service_disabled=True)
                to_raise = ServiceDisabledError(service)

            if to_raise is None:
                entry = ApiCallLog.objects.create(service=service, profile_id=actor_id, endpoint=truncated_endpoint, success=True)
                entry_pk = entry.pk
                if config.min_interval_seconds is not None:
                    config.last_call_at = timezone.now()
                    config.save(update_fields=["last_call_at"])
    except DatabaseError as exc:
        # Refused, as service_is_enabled refuses: an unread limit has no safe affirmative answer.
        logger.exception("Failed to reserve a call to %s - refusing it", service)
        raise RateLimiterUnavailableError(service) from exc

    if to_raise is not None:
        raise to_raise
    return entry_pk


def _finalize_call(
    entry_pk: int,
    *,
    success: bool,
    response_ms: int | None = None,
    status_code: int | None = None,
    endpoint: str | None = None,
) -> None:
    """
    Update a reservation row created by ``_reserve_call`` with the request's outcome.
    Updates the existing row in place rather than inserting a new one, so a reserved-but-not-yet-finalized call still counts toward ``check_rate_limit``'s window queries (which count rows regardless of ``success``) without double-counting once finalized.

    Args:
        entry_pk: pk of the ``ApiCallLog`` row returned by ``_reserve_call``.
        success: Whether the call succeeded (HTTP 2xx, no exception).
        response_ms: Round-trip time in milliseconds.
        status_code: The upstream's HTTP status; None when no response arrived.
        endpoint: Replaces the reserved endpoint when given - a call can know what it called only once it is made."""
    from urbanlens.dashboard.models.api_call_log import ApiCallLog

    outcome: dict[str, Any] = {"success": success, "response_ms": response_ms, "status_code": status_code}
    if endpoint is not None:
        outcome["endpoint"] = endpoint[:500]
    try:
        ApiCallLog.objects.filter(pk=entry_pk).update(**outcome)
    except Exception:
        logger.exception("Failed to finalize API call log entry %s", entry_pk)


def _release_call(entry_pk: int) -> None:
    """
    Drop a reservation row for a call the environment refused after it was reserved: a refusal leaves no row.

    Args:
        entry_pk: pk of the ``ApiCallLog`` row returned by ``_reserve_call``.
    """
    from urbanlens.dashboard.models.api_call_log import ApiCallLog

    try:
        ApiCallLog.objects.filter(pk=entry_pk).delete()
    except Exception:
        logger.exception("Failed to release API call log entry %s", entry_pk)


@dataclass(slots=True)
class ApiCallSlot:
    """
    One reserved call, filled in by the caller and recorded when the slot closes.

    Attributes:
        service: The service key the slot reserved.
        entry_pk: The pk of the reserved ``ApiCallLog`` row, for code inside the slot to attach its own data to;
            None when the call is tallied rather than recorded per row.
        success: Whether the call succeeded; left False, a call that raised is recorded as failed.
        status_code: The upstream's HTTP status, when there was one.
        endpoint: What was called, when that is only known once it is made; replaces the reserved endpoint.
        refused: The environment refused the call after the slot opened (the provider behind a feature), so
            nothing was sent and the reservation is released instead of recorded.
    """

    service: str = ""
    entry_pk: int | None = None
    success: bool = False
    status_code: int | None = None
    endpoint: str | None = None
    refused: bool = False


#: The slot the running code is inside, so a call made within one fills in its row instead of writing a second.
_CURRENT_SLOT: ContextVar[ApiCallSlot | None] = ContextVar("api_call_slot", default=None)


def current_call_slot() -> ApiCallSlot | None:
    """
    The :func:`api_call_slot` the caller is running inside, if any.

    Returns:
        The open slot, or None.
    """
    return _CURRENT_SLOT.get()


@contextmanager
def api_call_slot(service: str, *, endpoint: str = "") -> Iterator[ApiCallSlot]:
    """
    Reserve one call to *service* before making it, and record its outcome after.

    For calls that do not go through a gateway's rate-limited session (SDK clients and other
    non-HTTP callers). The check and the ledger row are one locked step, so concurrent
    callers cannot all pass a check that only one of them fits under.

    Args:
        service: The service key.
        endpoint: What is being called, recorded on the ledger row.

    Yields:
        The slot; set its fields before the block ends.

    Raises:
        RequestCancelledError: Refused before the block ran - not available in this environment
            (:class:`EnvironmentRefusedError`), over a limit, disabled, the limiter could not be read,
            or the provider is backed off (``provider_health``).
    """
    from urbanlens.dashboard.services.core import call_tally, provider_health
    from urbanlens.dashboard.services.core.egress import require_egress

    require_egress(service)
    provider_health.check_admission(service, endpoint=endpoint)
    tallied = call_ledger(service) is CallLedger.TALLIED
    admission = call_tally.admit(service, endpoint=endpoint) if tallied else None
    entry_pk = None if tallied else _reserve_call(service, endpoint=endpoint)
    slot = ApiCallSlot(service=service, entry_pk=entry_pk)
    started = time.monotonic()
    token = _CURRENT_SLOT.set(slot)
    try:
        yield slot
    finally:
        _CURRENT_SLOT.reset(token)
        elapsed_ms = int((time.monotonic() - started) * 1000)
        if admission is not None:
            if slot.refused:
                admission.release()
            else:
                admission.record(success=slot.success, status_code=slot.status_code, response_ms=elapsed_ms)
        elif entry_pk is not None:
            if slot.refused:
                _release_call(entry_pk)
            else:
                _finalize_call(
                    entry_pk,
                    success=slot.success,
                    response_ms=elapsed_ms,
                    status_code=slot.status_code,
                    endpoint=slot.endpoint,
                )


# Session wrapper

type CallObserver = Callable[[int, requests.Response | None], None]

_CALL_OBSERVERS: ContextVar[tuple[CallObserver, ...]] = ContextVar("api_call_observers", default=())


@contextmanager
def observe_calls(observer: CallObserver) -> Iterator[None]:
    """
    Call ``observer`` with the row and response of each request the rate-limited session makes inside.

    While active, calls are recorded one row each and never tallied. Observers nest: every active one is told of
    each call, the outermost first. An observer that raises is logged and does not fail the call or stop the others.

    Args:
        observer: Called with the pk of the call's ``ApiCallLog`` row and the response, once the row is finalized;
            the response is None when the request raised instead of answering.

    Yields:
        Nothing; the observer applies for the block.
    """
    token = _CALL_OBSERVERS.set((*_CALL_OBSERVERS.get(), observer))
    try:
        yield
    finally:
        _CALL_OBSERVERS.reset(token)


def _notify_observers(observers: tuple[CallObserver, ...], entry_pk: int, response: requests.Response | None) -> None:
    """Hand a finalized call to its observers; a failure is logged and never reaches the caller."""
    for observer in observers:
        try:
            observer(entry_pk, response)
        except Exception:
            logger.exception("Call observer failed for API call log entry %s", entry_pk)


class _RateLimitedSession:
    """
    Wraps ``requests.Session`` to enforce rate limits and log every call.
    This is NOT a subclass of ``requests.Session`` - it delegates all attribute access to a real session so that caller code using ``self.session.get(...)`` continues to work unchanged."""

    def __init__(self, service_key: str, endpoint_for_log: Callable[[str], str] | None = None, *, session: requests.Session | None = None) -> None:
        import requests

        self._service_key = service_key
        # Reuse gateway's session to keep connections across instances (Gateway.reuses_connections).
        self._session = session if session is not None else requests.Session()
        # How this service's URLs are described in ApiCallLog; default is the URL itself.
        self._endpoint_for_log = endpoint_for_log or str

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
        """
        Reserve a rate-limit slot, make the request, record the result.
        The reservation (see ``_reserve_call``) atomically checks the rate limit and logs the attempt in one locked transaction, so this no longer has a check-then-log gap for concurrent callers to race through.
        A tallied service is checked and counted in one atomic step on the shared counters instead, and its outcome added to a tally rolled up into the ledger every minute (``services.core.call_tally``)."""
        from urbanlens.dashboard.services.core import call_tally, provider_health
        from urbanlens.dashboard.services.core.egress import require_egress
        from urbanlens.dashboard.services.core.input_validation import check_request_parameters
        from urbanlens.dashboard.services.core.task_limits import check_task_deadline, within_task_deadline
        from urbanlens.dashboard.services.core.upstream_breaker import breaker_for

        if _EXTERNAL_CALLS_FORBIDDEN.get():
            raise ExternalCallForbiddenError(f"{self._service_key}: {method}")
        # First: a refusal by environment is neither a rejected input, a throttle nor a provider's ill health.
        require_egress(self._service_key)
        observers = _CALL_OBSERVERS.get()
        # Before the breaker and the reservation: an input no API can answer is "no data" whatever else is true.
        check_request_parameters(self._service_key, params=kwargs.get("params"), json=kwargs.get("json"))
        check_task_deadline()
        endpoint = self._endpoint_for_log(str(url))
        breaker = breaker_for(self._service_key)
        if breaker is not None and (wait := breaker.wait(str(url), kwargs.get("params"))) is not None:
            log_api_call(self._service_key, success=False, endpoint=endpoint, was_rate_limited=True)
            record_unanswered(self._service_key)
            raise UpstreamThrottledError(self._service_key, retry_after=wait)
        provider_health.check_admission(self._service_key, endpoint=endpoint)
        admission: call_tally.Admission | None = None
        entry_pk: int | None = None
        try:
            if call_ledger(self._service_key) is CallLedger.TALLIED and not observers:
                admission = call_tally.admit(self._service_key, endpoint=endpoint)
            else:
                entry_pk = _reserve_call(self._service_key, endpoint=endpoint)
        except RequestCancelledError as exc:
            if exc.transient:
                record_unanswered(self._service_key)
            raise

        # requests has no default timeout; a forgotten timeout= would block indefinitely.
        # The (connect, read) tuple bounds each phase. Caller's timeout is kept, cut to remaining task deadline.
        kwargs["timeout"] = within_task_deadline(kwargs.get("timeout", (5, 30)))

        t0 = time.monotonic()
        try:
            resp = self._session.request(method, url, **kwargs)
            elapsed_ms = int((time.monotonic() - t0) * 1000)
            if admission is not None:
                admission.record(success=resp.ok, status_code=resp.status_code, response_ms=elapsed_ms)
            elif entry_pk is not None:
                _finalize_call(entry_pk, success=resp.ok, response_ms=elapsed_ms, status_code=resp.status_code)
                _notify_observers(observers, entry_pk, resp)
            if breaker is not None:
                breaker.observe(str(url), kwargs.get("params"), resp)
            if is_unanswered_status(resp.status_code):
                record_unanswered(self._service_key)
            return resp
        except Exception:
            elapsed_ms = int((time.monotonic() - t0) * 1000)
            if admission is not None:
                admission.record(success=False, status_code=None, response_ms=elapsed_ms)
            elif entry_pk is not None:
                _finalize_call(entry_pk, success=False, response_ms=elapsed_ms)
                _notify_observers(observers, entry_pk, None)
            record_unanswered(self._service_key)
            raise


class RequestCancelledError(DashboardError, GatewayRequestError):
    """
    Raised when a request is cancelled.

    Also a :class:`GatewayRequestError`: every gateway call passes through
    ``_reserve_call`` via ``_RateLimitedSession``, and a refusal is routine (development and the
    demo refuse most services; ``RateLimiterUnavailableError`` fires when ``ul_web`` hits its limit).
    Before this, a caller catching ``GatewayRequestError`` let a refusal escape as 500.
    Multiple inheritance preserves the exact type existing handlers expect.

    Args:
        service: The rate-limiter service key the cancelled request targeted.
        message: Optional message override for subclasses; without it, the subclass's formatted message would be mistaken for the service name and wrapped again (e.g. ``Request cancelled for service 'Rate limit exceeded for service 'nps'''``)."""

    #: Whether asking again soon could succeed. A disabled service is a settled answer; a limit or an
    #: unreadable limiter is not.
    transient: ClassVar[bool] = True

    def __init__(self, service: str, message: str | None = None) -> None:
        super().__init__(message or f"Request cancelled for service '{service}'")
        self.service = service


class RateLimitExceededError(RequestCancelledError):
    """
    Raised when a rate limit prevents an API call from proceeding.

    Args:
        service: The rate-limiter service key.
        message: Optional message override for subclasses."""

    def __init__(self, service: str, message: str | None = None) -> None:
        super().__init__(service, message or f"Rate limit exceeded for service '{service}'")


class UpstreamThrottledError(RateLimitExceededError, UpstreamBusyError, GatewayRateLimitedError):
    """
    Refused without a request, because the upstream told this deployment to wait and the wait has not passed.

    Args:
        service: The rate-limiter service key.
        retry_after: Seconds until the upstream's breaker closes.
    """

    def __init__(self, service: str, *, retry_after: int) -> None:
        super().__init__(service, f"'{service}' is throttled upstream for another {retry_after}s")
        self.retry_after = retry_after


class ServiceDisabledError(RequestCancelledError):
    """Raised when a service is disabled."""

    transient: ClassVar[bool] = False

    def __init__(self, service: str) -> None:
        super().__init__(service, f"Service '{service}' is disabled")


class EnvironmentRefusedError(ServiceDisabledError):
    """
    This environment does not call the service at all: not a failure, and not worth retrying here.

    A :class:`ServiceDisabledError`, so every caller that already degrades on a switched-off service keeps
    working. Not transient, so it is never recorded as an unanswered call or held against the provider's
    health. Its ``is_outage`` stays True in the sense that property is documented for - nothing was learned
    about what was asked - so no caller stores the refusal as an empty answer. A provider chain skips the
    source rather than deferring it (``services.locations.boundaries``), and a panel says it is not available
    in this environment (``services.pins.external_data``).

    Attributes:
        service: The service key.
        category: How the service is classified.
        environment: The environment that refused it.
    """

    def __init__(self, service: str, *, category: str, environment: str) -> None:
        RequestCancelledError.__init__(self, service, f"'{service}' ({category}) is not available in the {environment or 'unknown'} environment")
        self.category = category
        self.environment = environment


class RateLimiterUnavailableError(RequestCancelledError):
    """Raised when a call is refused because its rate limit could not be read or recorded."""

    def __init__(self, service: str) -> None:
        super().__init__(service, f"Rate limit for service '{service}' could not be checked")
