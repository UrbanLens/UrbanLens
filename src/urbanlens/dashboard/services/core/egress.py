"""This deployment's egress policy, applied at the point every external call passes through (D26).

The rules are in :mod:`urbanlens.UrbanLens.egress`. This module reads the settings they take, finds each
service's :class:`~urbanlens.UrbanLens.egress.EgressCategory`, and refuses a call the environment does
not allow with :class:`~urbanlens.dashboard.services.core.rate_limiter.EnvironmentRefusedError`.

A refusal is not a failure: nothing was sent and the source is not down; it is not available here.
It writes no ``ApiCallLog`` row, and logs once per service every :data:`LOG_INTERVAL_SECONDS`.
"""

from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
import functools
import logging
import threading
import time
from typing import TYPE_CHECKING, ParamSpec, TypeVar

from urbanlens.UrbanLens.egress import (
    BEAT_EGRESS,
    UNCLASSIFIED_CATEGORY,
    BeatEgress,
    EgressCategory,
    EgressDecision,
    background_egress_permitted,
    decide,
    default_environment_share,
    full_egress,
    hosted_basemap_key,
    policy_environment,
)

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator, Mapping

logger = logging.getLogger(__name__)

P = ParamSpec("P")
R = TypeVar("R")

#: How long a refused service logs at DEBUG after logging once at INFO.
LOG_INTERVAL_SECONDS = 600.0


@dataclass(frozen=True, slots=True)
class Unledgered:
    """An external path that does not go through a gateway's rate-limited session or an ``api_call_slot``.

    Attributes:
        category: How it is classified.
        enforced_by: Where the policy holds it, or why it is not held.
    """

    category: EgressCategory
    enforced_by: str


#: External calls with no ``ApiRateLimit`` row (and no ``ServiceDefaults``) of their own, classified here so the
#: policy and the completeness test cover them too.
UNLEDGERED_SERVICES: Mapping[str, Unledgered] = {
    "email": Unledgered(EgressCategory.MESSAGING, "settings.EMAIL_DELIVERY_BACKEND: the console backend off production unless UL_EMAIL_SEND_OUTSIDE_PRODUCTION"),
    "unified_push": Unledgered(EgressCategory.MESSAGING, "services.notifications.push.send_push_to_devices: require_egress before each batch"),
    "stripe": Unledgered(EgressCategory.PUBLIC_WRITE, "services.billing.stripe_client: is_configured and configure ask before any SDK call"),
    "google_oauth_refresh": Unledgered(EgressCategory.QUOTA, "the calendar and photos gateways ask for their own service before refreshing"),
    "google_oauth_connect": Unledgered(EgressCategory.QUOTA, "not held: a person connecting or disconnecting their own account"),
    "social_sign_in": Unledgered(EgressCategory.QUOTA, "not held: Google and Discord sign-in, inside social_core"),
    "flickr_oauth": Unledgered(EgressCategory.QUOTA, "not held: a person connecting their own Flickr account"),
    "user_url_fetch": Unledgered(
        EgressCategory.QUOTA,
        "not held: a URL a person supplied, fetched for them through url_safety (link pages, gallery photos, remote tile templates, social-link checks, avatars)",
    ),
    "github_contributors": Unledgered(EgressCategory.QUOTA, "not held: the thanks page, cached"),
    "git_fetch": Unledgered(EgressCategory.QUOTA, "not held: the site-admin update check"),
    "gotify": Unledgered(EgressCategory.INTERNAL, "self-hosted"),
    "clamd": Unledgered(EgressCategory.INTERNAL, "self-hosted"),
    "ai_inference": Unledgered(EgressCategory.INTERNAL, "the sandboxed transport; the provider call behind it is held by the feature's api_call_slot"),
}

#: Service keys that reach REData or our own hosts but carry no ``ServiceDefaults`` of their own: gateways whose
#: limits come from the generic fallback, labels on REData-backed providers, and keys the policy asks about
#: before a call made on another key's session.
UNREGISTERED_SERVICES: Mapping[str, EgressCategory] = {
    "redata_boundary": EgressCategory.REDATA,
    "redata_place_details": EgressCategory.REDATA,
    "redata_json": EgressCategory.REDATA,
    "redata_location_context": EgressCategory.REDATA,
    # Street-level and archive providers: labels on gateways whose calls REData makes.
    "mapillary": EgressCategory.REDATA,
    "kartaview": EgressCategory.REDATA,
    "panoramax": EgressCategory.REDATA,
    "smithsonian": EgressCategory.REDATA,
    "library_of_congress": EgressCategory.REDATA,
    "internet_archive": EgressCategory.REDATA,
    "chronicling_america": EgressCategory.REDATA,
    # Twilio's shared base; its channels are ``sms`` and ``whatsapp``.
    "twilio": EgressCategory.MESSAGING,
    # The public Overpass mirrors the self-hosted primary falls back to; asked before each is tried.
    "overpass_public_mirror": EgressCategory.QUOTA,
    # Save Page Now: a write to the Internet Archive, made on the ``wayback_machine`` session.
    "wayback_save": EgressCategory.PUBLIC_WRITE,
}


def current_environment() -> str:
    """The environment the policy applies here (``"testing"`` for the test suite)."""
    from django.conf import settings

    return policy_environment(getattr(settings, "ENVIRONMENT_NAME", ""), testing=bool(getattr(settings, "TESTING", False)))


def environment_share() -> float:
    """This deployment's share of every ``quota`` and ``billed`` budget.

    Returns:
        ``UL_ENVIRONMENT_SHARE`` when set, else the environment's default. The test suite takes all of it
        whatever its container's environment holds.
    """
    environment = current_environment()
    if environment == "testing":
        return default_environment_share(environment)
    from urbanlens.UrbanLens.settings.app import settings as app_settings

    configured = app_settings.environment_share
    return default_environment_share(environment) if configured is None else configured


def share_overrides() -> Mapping[str, float]:
    """``UL_ENVIRONMENT_SHARE_OVERRIDES``; none in the test suite."""
    if current_environment() == "testing":
        return {}
    from urbanlens.UrbanLens.settings.app import settings as app_settings

    return app_settings.environment_share_overrides


#: Categories already found, so the policy asked several times per call does not rebuild every plugin's defaults
#: each time. Only a found category is kept: a plugin loaded later can still classify a key nothing knew.
_known_categories: dict[str, EgressCategory] = {}


def forget_categories() -> None:
    """Drop the remembered categories, for a test that changes what classifies a service."""
    _known_categories.clear()


def explicit_category(service: str) -> EgressCategory | None:
    """How ``service`` is classified, or None when nothing classifies it.

    Args:
        service: A rate-limiter service key, or a key from :data:`UNLEDGERED_SERVICES`.

    Returns:
        Its ``ServiceDefaults.category`` (plugin defaults winning, as ``all_service_defaults`` documents), else its
        entry in :data:`UNREGISTERED_SERVICES` or :data:`UNLEDGERED_SERVICES`.
    """
    from urbanlens.dashboard.services.core.rate_limiter import SERVICE_REGISTRY, all_service_defaults

    known = _known_categories.get(service)
    if known is not None:
        return known
    try:
        defaults = all_service_defaults().get(service)
    except Exception:
        logger.exception("Could not read plugin service defaults for %s; the core registry classifies it", service)
        defaults = SERVICE_REGISTRY.get(service)
    unledgered = UNLEDGERED_SERVICES.get(service)
    if defaults is not None and defaults.category is not None:
        category: EgressCategory | None = defaults.category
    else:
        category = UNREGISTERED_SERVICES.get(service) or (unledgered.category if unledgered else None)
    if category is not None:
        _known_categories[service] = category
    return category


_warned_unclassified: set[str] = set()


def service_category(service: str) -> EgressCategory:
    """How ``service`` is classified, treating an unclassified one as :data:`UNCLASSIFIED_CATEGORY`."""
    category = explicit_category(service)
    if category is not None:
        return category
    if service not in _warned_unclassified:
        _warned_unclassified.add(service)
        logger.warning("No egress category for service %r; treating it as %s until it is classified", service, UNCLASSIFIED_CATEGORY)
    return UNCLASSIFIED_CATEGORY


def egress_decision(service: str) -> EgressDecision:
    """This deployment's terms for ``service``."""
    return decide(service, service_category(service), current_environment(), environment_share=environment_share(), overrides=share_overrides())


def egress_permitted(service: str) -> bool:
    """Whether this deployment may call ``service`` at all. Logs nothing; see :func:`require_egress`."""
    return egress_decision(service).allowed


def service_share(service: str) -> float:
    """The multiplier on ``service``'s budgets here: 1 for an unrestricted category, 0 when it is refused."""
    return egress_decision(service).share


def direct_fallback_permitted() -> bool:
    """Whether a REData-first chain may go on to a direct ``quota`` or ``billed`` provider after REData fails.

    Only production: anywhere else a REData failure is reported as unavailable, so a busy REData does not
    push its load onto the direct provider from an address production shares.
    """
    return full_egress(current_environment())


def hosted_basemap_api_key() -> str:
    """The Protomaps key this deployment buys its hosted basemap with: production's alone (D26).

    Returns:
        ``UL_PROTOMAPS_API_KEY`` on production and in tests; elsewhere an empty string, which keeps the street and
        dark layers on the self-hosted mirror.
    """
    from urbanlens.UrbanLens.settings.app import settings as app_settings

    return hosted_basemap_key(current_environment(), app_settings.protomaps_api_key)


_last_logged: dict[str, float] = {}
_last_logged_lock = threading.Lock()


def _should_log_loudly(service: str) -> bool:
    now = time.monotonic()
    with _last_logged_lock:
        last = _last_logged.get(service)
        if last is not None and now - last < LOG_INTERVAL_SECONDS:
            return False
        _last_logged[service] = now
        return True


_REFUSALS: ContextVar[list[str] | None] = ContextVar("egress_refusals", default=None)


@contextmanager
def collect_refusals() -> Iterator[list[str]]:
    """Record every service the environment refuses while the block runs, however the caller handled it.

    A panel source that swallows a refusal and returns empty-handed is then still told apart from one that
    found nothing.

    Yields:
        The refused service keys, in order, filled in as the block runs.
    """
    refused: list[str] = []
    token = _REFUSALS.set(refused)
    try:
        yield refused
    finally:
        _REFUSALS.reset(token)


def require_egress(service: str) -> EgressDecision:
    """Refuse a call this environment does not allow, before anything is sent or recorded.

    Args:
        service: The rate-limiter service key.

    Returns:
        The decision, when the call may go ahead.

    Raises:
        EnvironmentRefusedError: The environment does not call this service.
    """
    decision = egress_decision(service)
    if decision.allowed:
        return decision
    from urbanlens.dashboard.services.core.rate_limiter import EnvironmentRefusedError

    refused = _REFUSALS.get()
    if refused is not None:
        refused.append(service)
    level = logging.INFO if _should_log_loudly(service) else logging.DEBUG
    logger.log(
        level,
        "Not calling %s: %s services are not available in the %s environment (share %s%s); nothing was sent",
        service,
        decision.category,
        decision.environment or "unknown",
        decision.share,
        ", from UL_ENVIRONMENT_SHARE_OVERRIDES" if decision.overridden else "",
    )
    raise EnvironmentRefusedError(service, category=decision.category, environment=decision.environment)


def background_tasks_allowlist() -> frozenset[str]:
    """``UL_BACKGROUND_TASKS_ALLOWLIST``: beat entries that reach outside but run here anyway."""
    from urbanlens.UrbanLens.settings.app import settings as app_settings

    return frozenset(app_settings.background_tasks_allowlist)


#: Beat entry name to the dotted paths of the task functions :func:`external_background_task` gates under it (the
#: entry's own task, and any it chains), so the completeness test can check every external entry's task is gated.
GATED_BACKGROUND_TASKS: dict[str, set[str]] = {}


def external_background_task(entry: str) -> Callable[[Callable[P, R]], Callable[P, R | None]]:
    """Mark a scheduled task as reaching outside, and skip it where background egress is off.

    Beat already leaves such an entry out of the schedule off production. This stops a run that got onto a queue
    some other way: a sweep that re-enqueues itself, a chain, or an operator's ``.delay()``.

    Args:
        entry: The task's ``CELERY_BEAT_SCHEDULE`` entry name, as :data:`~urbanlens.UrbanLens.egress.BEAT_EGRESS` classifies it.

    Returns:
        A decorator; apply it under the Celery task decorator.

    Raises:
        ValueError: ``entry`` is not classified as external.
    """
    if BEAT_EGRESS.get(entry) is not BeatEgress.EXTERNAL:
        raise ValueError(f"{entry!r} is not an external beat entry in BEAT_EGRESS")

    def decorate(func: Callable[P, R]) -> Callable[P, R | None]:
        @functools.wraps(func)
        def wrapper(*args: P.args, **kwargs: P.kwargs) -> R | None:
            environment = current_environment()
            if not background_egress_permitted(entry, environment, background_tasks_allowlist()):
                level = logging.INFO if _should_log_loudly(f"beat:{entry}") else logging.DEBUG
                logger.log(level, "Skipped %s: background work that reaches outside runs only on production, and %r is not in UL_BACKGROUND_TASKS_ALLOWLIST (environment %s)", func.__name__, entry, environment or "unknown")
                return None
            return func(*args, **kwargs)

        GATED_BACKGROUND_TASKS.setdefault(entry, set()).add(f"{func.__module__}.{func.__name__}")
        return wrapper

    return decorate


def describe_policy() -> str:
    """One line for the startup log: environment, share, overrides and allow-listed background work."""
    environment = current_environment()
    overrides = share_overrides()
    allowlist = sorted(background_tasks_allowlist())
    override_text = ", ".join(f"{service}={share:g}" for service, share in sorted(overrides.items())) or "none"
    return f"Egress policy: environment {environment or 'unknown'}, UL_ENVIRONMENT_SHARE {environment_share():g}, overrides {override_text}, background allowlist {', '.join(allowlist) or 'none'}"


def log_egress_policy() -> None:
    """Log the policy once at startup, and warn about settings it no longer reads or cannot use."""
    import os

    try:
        logger.info(describe_policy())
        for retired, replacement in (("UL_ALLOW_OUTBOUND_APIS", "UL_ENVIRONMENT_SHARE"), ("UL_BILLED_API_SHARE", "UL_ENVIRONMENT_SHARE")):
            if os.environ.get(retired, "").strip():
                logger.warning("%s is set but no longer read; the egress policy follows UL_ENVIRONMENT and %s (D26)", retired, replacement)
        for service in share_overrides():
            if explicit_category(service) is None:
                logger.warning("UL_ENVIRONMENT_SHARE_OVERRIDES names %r, which is not a classified service", service)
        unknown = sorted(background_tasks_allowlist() - set(BEAT_EGRESS))
        if unknown:
            logger.warning("UL_BACKGROUND_TASKS_ALLOWLIST names %s, which no beat entry is called", ", ".join(unknown))
    except Exception:
        logger.exception("Could not describe the egress policy")
