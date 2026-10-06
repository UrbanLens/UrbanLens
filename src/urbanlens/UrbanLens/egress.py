"""Which external calls a deployment may make, by kind of service and by environment (D26).

Every UrbanLens host shares one residential address with production REData. A keyless public API's
per-address tolerance, and a billed API's free tier, are therefore one budget for all of them, and
production is meant to spend most of it. Each external service is classified as one
:class:`EgressCategory`, and this module decides per environment:

============================  ===============  ===============  =========================
Category                      production       staging          development, local
============================  ===============  ===============  =========================
``redata``, ``internal``      allowed          allowed          allowed
``ai`` (hosted providers)     allowed          allowed          refused
``quota``, ``billed``         budget x share   budget x share   budget x share (share 0)
``messaging``                 allowed          refused          refused
``public_write``              allowed          refused          refused
============================  ===============  ===============  =========================

The share is ``UL_ENVIRONMENT_SHARE``, else :data:`ENVIRONMENT_SHARE_DEFAULTS`. One service can be
given its own with ``UL_ENVIRONMENT_SHARE_OVERRIDES`` (``nominatim=0.02,sms=1``); for ``messaging``,
``public_write`` and ``ai`` an override above 0 opts the service in, and 0 turns it off. An ``ai``
service is an AI feature (``trivia_generation``) or a hosted provider (``ai_cloudflare``). A local or
self-hosted model (Ollama) is ``internal``, so development keeps it. The test suite takes the whole
of every budget, as production would, because it mocks the network.

Nothing here imports Django apps, so settings modules can use it at import time. The service layer
that reads these settings and refuses calls is ``dashboard.services.core.egress``.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from enum import StrEnum
import math
from typing import Any

from urbanlens.UrbanLens.environments.meta import EnvironmentTypes


class EgressCategory(StrEnum):
    """What kind of service an external call goes to, which decides who may spend it."""

    #: Production REData: this project's own cache in front of the paid and public sources.
    REDATA = "redata"
    #: Self-hosted or local: our Overpass, the tile cache, Ollama, ``ai_inference``, Immich.
    INTERNAL = "internal"
    #: Keyless or free-keyed public APIs, held to a per-address or per-key tolerance.
    QUOTA = "quota"
    #: A price per call, or a paid key.
    BILLED = "billed"
    #: Hosted LLM and vision providers (Cloudflare Workers AI, OpenAI, Anthropic), and the features that call
    #: them. A local or self-hosted model is ``INTERNAL``.
    AI = "ai"
    #: Email, SMS and WhatsApp to real people.
    MESSAGING = "messaging"
    #: Anything that writes to a third party, such as the Wayback Machine's Save Page Now.
    PUBLIC_WRITE = "public_write"


#: Called from every environment, budgets unscaled.
UNRESTRICTED_CATEGORIES = frozenset({EgressCategory.REDATA, EgressCategory.INTERNAL})
#: Budgets multiplied by the environment's share.
SHARED_BUDGET_CATEGORIES = frozenset({EgressCategory.QUOTA, EgressCategory.BILLED})
#: Production only, unless an override opts a service in.
PRODUCTION_ONLY_CATEGORIES = frozenset({EgressCategory.MESSAGING, EgressCategory.PUBLIC_WRITE})
#: Where a hosted AI provider may be called: production and staging, whose AI the audit measured and logged, and the
#: test suite, which mocks the network. Development, local and an unknown environment make no hosted AI call (Jess,
#: 2026-10-06), unless an override opts one in.
HOSTED_AI_ENVIRONMENTS = frozenset({EnvironmentTypes.PRODUCTION, EnvironmentTypes.STAGING, EnvironmentTypes.TESTING})

#: How a service no one has classified is treated: as a shared budget, so refused by default off production.
UNCLASSIFIED_CATEGORY = EgressCategory.BILLED

#: Each environment's share of every ``quota`` and ``billed`` budget when ``UL_ENVIRONMENT_SHARE`` does not say.
#: Production, staging and development together stay at or below 1.
ENVIRONMENT_SHARE_DEFAULTS: Mapping[str, float] = {
    EnvironmentTypes.PRODUCTION: 0.9,
    EnvironmentTypes.STAGING: 0.05,
    EnvironmentTypes.DEVELOPMENT: 0.0,
    EnvironmentTypes.LOCAL: 0.0,
    EnvironmentTypes.TESTING: 1.0,
}
#: The share of an environment the table does not name. Startup already refuses an unknown ``UL_ENVIRONMENT``.
UNKNOWN_ENVIRONMENT_SHARE = 0.0

#: Environments that call every category, at production's terms or better.
_FULL_EGRESS_ENVIRONMENTS = frozenset({EnvironmentTypes.PRODUCTION, EnvironmentTypes.TESTING})


def policy_environment(environment_name: str | None, *, testing: bool) -> str:
    """The environment the egress policy applies.

    Args:
        environment_name: ``settings.ENVIRONMENT_NAME``, from ``UL_ENVIRONMENT``.
        testing: Whether this is a test run, which inherits its container's ``UL_ENVIRONMENT`` and mocks the network.

    Returns:
        ``"testing"`` for a test run, else the lower-cased environment name.
    """
    if testing:
        return str(EnvironmentTypes.TESTING)
    return str(environment_name or "").strip().lower()


def full_egress(environment: str) -> bool:
    """Whether ``environment`` calls every category (production, and the test suite).

    Args:
        environment: A :func:`policy_environment` value.

    Returns:
        True for production and testing.
    """
    return environment in _FULL_EGRESS_ENVIRONMENTS


def hosted_ai_permitted(environment: str) -> bool:
    """Whether ``environment`` may call a hosted AI provider when nothing overrides it.

    Args:
        environment: A :func:`policy_environment` value.

    Returns:
        True for production, staging and testing; False for development, local and anything unknown.
    """
    return environment in HOSTED_AI_ENVIRONMENTS


def default_environment_share(environment: str) -> float:
    """The share of every shared budget ``environment`` holds when ``UL_ENVIRONMENT_SHARE`` is unset.

    Args:
        environment: A :func:`policy_environment` value.

    Returns:
        The default from :data:`ENVIRONMENT_SHARE_DEFAULTS`, or :data:`UNKNOWN_ENVIRONMENT_SHARE`.
    """
    return ENVIRONMENT_SHARE_DEFAULTS.get(environment, UNKNOWN_ENVIRONMENT_SHARE)


def _share(value: Any, *, what: str) -> float:
    try:
        share = float(value)
    except (TypeError, ValueError):
        raise ValueError(f"{what} must be a number between 0 and 1, not {value!r}") from None
    if math.isnan(share) or not 0.0 <= share <= 1.0:
        raise ValueError(f"{what} must be between 0 and 1, not {value!r}")
    return share


def parse_share_overrides(raw: str | Mapping[str, Any] | None) -> dict[str, float]:
    """Parse ``UL_ENVIRONMENT_SHARE_OVERRIDES``.

    Args:
        raw: ``"nominatim=0.02, sms=1"``, an already-parsed mapping, or nothing.

    Returns:
        Service key to share.

    Raises:
        ValueError: An entry is not ``service=share``, names a service twice, or holds a share outside 0-1.
    """
    if raw is None:
        return {}
    pairs: Iterable[tuple[str, Any]]
    if isinstance(raw, Mapping):
        pairs = raw.items()
    else:
        entries = [entry.strip() for entry in str(raw).split(",") if entry.strip()]
        parsed: list[tuple[str, Any]] = []
        for entry in entries:
            service, separator, value = entry.partition("=")
            if not separator or not service.strip():
                raise ValueError(f"UL_ENVIRONMENT_SHARE_OVERRIDES entry {entry!r} is not service=share")
            parsed.append((service, value))
        pairs = parsed
    overrides: dict[str, float] = {}
    for service, value in pairs:
        key = str(service).strip().lower()
        if key in overrides:
            raise ValueError(f"UL_ENVIRONMENT_SHARE_OVERRIDES names {key!r} twice")
        overrides[key] = _share(value, what=f"The override for {key!r}")
    return overrides


@dataclass(frozen=True, slots=True)
class EgressDecision:
    """Whether one service may be called here, and on what share of its budget.

    Attributes:
        service: The rate-limiter service key.
        category: How the service is classified.
        environment: The :func:`policy_environment` the decision is for.
        share: The multiplier on the service's budgets; 0 refuses it. 1 for an unrestricted category.
        overridden: The share came from ``UL_ENVIRONMENT_SHARE_OVERRIDES``.
    """

    service: str
    category: EgressCategory
    environment: str
    share: float
    overridden: bool = False

    @property
    def allowed(self) -> bool:
        """Whether a call may be made at all."""
        return self.share > 0.0


def decide(service: str, category: EgressCategory, environment: str, *, environment_share: float, overrides: Mapping[str, float]) -> EgressDecision:
    """Decide one service's terms in one environment.

    Args:
        service: The rate-limiter service key.
        category: The service's classification.
        environment: A :func:`policy_environment` value.
        environment_share: This environment's share of every shared budget.
        overrides: Per-service shares from ``UL_ENVIRONMENT_SHARE_OVERRIDES``. Ignored for an unrestricted
            category, which the admin switch on its ``ApiRateLimit`` row turns off instead.

    Returns:
        The decision.
    """
    if category in UNRESTRICTED_CATEGORIES:
        return EgressDecision(service, category, environment, 1.0)
    override = overrides.get(service)
    if override is not None:
        return EgressDecision(service, category, environment, override, overridden=True)
    if category in SHARED_BUDGET_CATEGORIES:
        return EgressDecision(service, category, environment, environment_share)
    if category is EgressCategory.AI:
        return EgressDecision(service, category, environment, 1.0 if hosted_ai_permitted(environment) else 0.0)
    return EgressDecision(service, category, environment, 1.0 if full_egress(environment) else 0.0)


def scaled_limit(limit: int | None, share: float) -> int | None:
    """One budget window's limit at ``share`` of it.

    Args:
        limit: The configured limit; None is unlimited.
        share: The deployment's share, above 0.

    Returns:
        The limit times the share, rounded down but never below one call; unchanged at a share of 1, for no
        limit, or for a limit of 0.
    """
    if limit is None or share >= 1.0 or limit <= 0:
        return limit
    return max(1, math.floor(limit * share))


def hosted_basemap_key(environment: str, protomaps_api_key: str) -> str:
    """The Protomaps key this deployment buys the hosted basemap with, or nothing.

    The hosted basemap is billed, and production holds that budget: everywhere else draws the street and
    dark layers from the self-hosted mirror whatever ``UL_PROTOMAPS_API_KEY`` says.

    Args:
        environment: A :func:`policy_environment` value.
        protomaps_api_key: ``UL_PROTOMAPS_API_KEY``.

    Returns:
        The key on production (and in tests), else an empty string.
    """
    return protomaps_api_key if full_egress(environment) else ""


SMTP_EMAIL_BACKEND = "django.core.mail.backends.smtp.EmailBackend"
CONSOLE_EMAIL_BACKEND = "django.core.mail.backends.console.EmailBackend"


def email_delivery_backend(environment: str, *, configured: str | None, send_outside_production: bool) -> str:
    """The Django backend that delivers this deployment's mail.

    Off production, mail is printed to the log by default: a staging clone of production holds real
    addresses, and development's mail relay bills per message. ``UL_EMAIL_SEND_OUTSIDE_PRODUCTION``
    restores real delivery.

    Args:
        environment: A :func:`policy_environment` value.
        configured: ``UL_EMAIL_BACKEND``, if set.
        send_outside_production: ``UL_EMAIL_SEND_OUTSIDE_PRODUCTION``.

    Returns:
        A backend class path. Production defaults to SMTP, which fails loudly when unconfigured; the test
        suite to the console, so a host's ``.env`` cannot make it send.
    """
    if environment == EnvironmentTypes.TESTING:
        return configured or CONSOLE_EMAIL_BACKEND
    if environment == EnvironmentTypes.PRODUCTION or send_outside_production:
        return configured or SMTP_EMAIL_BACKEND
    return CONSOLE_EMAIL_BACKEND


class BeatEgress(StrEnum):
    """Whether a scheduled task reaches outside this deployment."""

    #: Calls, or enqueues work whose job is to call, any external service (REData included).
    EXTERNAL = "external"
    #: This deployment's own database, cache, storage and broker only.
    INTERNAL = "internal"


#: Every ``CELERY_BEAT_SCHEDULE`` entry, classified. Off production only ``INTERNAL`` entries are scheduled,
#: plus any named in ``UL_BACKGROUND_TASKS_ALLOWLIST``; an ``EXTERNAL`` task also checks for itself
#: (``services.core.egress.external_background_task``), so a run enqueued some other way stops too.
#:
#: ``EXTERNAL`` is a task whose job is to call out. A task whose job is this deployment's own state stays
#: ``INTERNAL`` even when it sends mail, pushes or texts, or asks a provider on a user's behalf (a SpotGuessr
#: round, an upload's scan): messaging is console or nothing off production, and every such call is held by
#: the call-level policy, so skipping the task would only lose the maintenance (a check-in turning overdue,
#: a stalled game ending, a refused enqueue being replayed).
BEAT_EGRESS: Mapping[str, BeatEgress] = {
    "scheduled-database-backup-check": BeatEgress.INTERNAL,
    "scheduled-vestigial-asset-cleanup": BeatEgress.INTERNAL,
    "scheduled-location-enrichment": BeatEgress.EXTERNAL,
    "scheduled-trivia-generation": BeatEgress.EXTERNAL,
    "scheduled-trivia-wiki-incorporation": BeatEgress.EXTERNAL,
    "scheduled-demo-account-purge": BeatEgress.INTERNAL,
    "scheduled-redata-public-locations-sync": BeatEgress.EXTERNAL,
    "spotguessr-stall-sweep": BeatEgress.INTERNAL,
    "trivia-stall-sweep": BeatEgress.INTERNAL,
    "consensus-stall-sweep": BeatEgress.INTERNAL,
    "achievements-sweep": BeatEgress.INTERNAL,
    "reputation-sweep": BeatEgress.INTERNAL,
    "stripe-subscriptions-sync": BeatEgress.EXTERNAL,
    "pwyw-usage-ledger-sweep": BeatEgress.INTERNAL,
    "safety-checkin-due-reminders": BeatEgress.INTERNAL,
    "safety-checkin-final-warnings": BeatEgress.INTERNAL,
    "safety-checkin-escalation": BeatEgress.INTERNAL,
    "safety-checkin-archival-sweep": BeatEgress.INTERNAL,
    "account-deletion-reminders": BeatEgress.INTERNAL,
    "account-deletion-hard-delete": BeatEgress.INTERNAL,
    "safety-checkin-auto-delete": BeatEgress.INTERNAL,
    "undo-action-pruning": BeatEgress.INTERNAL,
    "direct-message-hard-delete": BeatEgress.INTERNAL,
    "upgrade-placeholder-pin-names": BeatEgress.INTERNAL,
    "image-thumbnail-backfill": BeatEgress.INTERNAL,
    "task-outbox-drain": BeatEgress.INTERNAL,
    "requeue-stalled-pending-uploads": BeatEgress.INTERNAL,
    "wayback-archive-sweep": BeatEgress.EXTERNAL,
    "calendar-push-sweep": BeatEgress.EXTERNAL,
    "fact-confidence-sweep": BeatEgress.INTERNAL,
    "requeue-stalled-device-scans": BeatEgress.INTERNAL,
    "discard-unretried-failed-uploads": BeatEgress.INTERNAL,
    "sweep-stale-preview-sources": BeatEgress.INTERNAL,
    "sweep-held-uploads": BeatEgress.INTERNAL,
    "retry-waiting-uploads": BeatEgress.INTERNAL,
    "adopt-stalled-comment-scans": BeatEgress.INTERNAL,
    "sweep-unnamed-files": BeatEgress.INTERNAL,
    "image-marker-thumbnail-backfill": BeatEgress.INTERNAL,
    "image-analysis-thumbnail-backfill": BeatEgress.INTERNAL,
    "pin-tombstone-pruning": BeatEgress.INTERNAL,
    "api-call-tally-rollup": BeatEgress.INTERNAL,
    "api-call-log-pruning": BeatEgress.INTERNAL,
    "provider-health-evaluation": BeatEgress.INTERNAL,
    "session-pruning": BeatEgress.INTERNAL,
    "read-notification-pruning": BeatEgress.INTERNAL,
    "public-pin-candidate-evaluation": BeatEgress.INTERNAL,
    "public-media-cache-sweep": BeatEgress.INTERNAL,
}


def background_egress_permitted(entry: str, environment: str, allowlist: Iterable[str]) -> bool:
    """Whether a background task that reaches outside may run here.

    Args:
        entry: The task's ``CELERY_BEAT_SCHEDULE`` entry name.
        environment: A :func:`policy_environment` value.
        allowlist: ``UL_BACKGROUND_TASKS_ALLOWLIST``.

    Returns:
        True on production and in tests, or when the entry is allow-listed.
    """
    return full_egress(environment) or entry in set(allowlist)


def scheduled_beat_entries(schedule: Mapping[str, Any], environment: str, allowlist: Iterable[str]) -> dict[str, Any]:
    """The beat entries this environment runs.

    Args:
        schedule: The whole ``CELERY_BEAT_SCHEDULE``.
        environment: A :func:`policy_environment` value.
        allowlist: ``UL_BACKGROUND_TASKS_ALLOWLIST``.

    Returns:
        Every entry on production and in tests; elsewhere the ``INTERNAL`` ones and the allow-listed ones. An entry
        :data:`BEAT_EGRESS` does not classify is treated as ``EXTERNAL``.
    """
    allowed = set(allowlist)
    return {name: entry for name, entry in schedule.items() if BEAT_EGRESS.get(name, BeatEgress.EXTERNAL) is BeatEgress.INTERNAL or background_egress_permitted(name, environment, allowed)}
