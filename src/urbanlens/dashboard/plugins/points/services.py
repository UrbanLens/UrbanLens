"""External services, process tiers and test harness: rate-limit defaults, egress classification, upstream breakers, queues, roles."""

from __future__ import annotations

from typing import TYPE_CHECKING

from urbanlens.dashboard.plugins.extensions import ExtensionPoint, FilterHook, Registry

if TYPE_CHECKING:
    from collections.abc import Callable
    from contextlib import AbstractContextManager

    from urbanlens.dashboard.services.core.database_roles import DatabaseRole
    from urbanlens.dashboard.services.core.egress import Unledgered
    from urbanlens.dashboard.services.core.provider_health import ProviderGroup
    from urbanlens.dashboard.services.core.rate_limiter import ServiceDefaults
    from urbanlens.dashboard.services.core.task_limits import QueueLimits
    from urbanlens.dashboard.services.core.upstream_breaker import UpstreamBreaker
    from urbanlens.UrbanLens.egress import EgressDecision


SERVICE_DEFAULTS: Registry[str, ServiceDefaults] = Registry(
    "services.defaults",
    "Rate-limit defaults per external service key. A plugin's claim wins over core's static registry for the same key.",
)

UNLEDGERED_SERVICES: Registry[str, Unledgered] = Registry(
    "services.unledgered",
    "External calls with no rate-limited gateway, classified for the egress policy.",
)

UPSTREAM_BREAKERS: ExtensionPoint[UpstreamBreaker] = ExtensionPoint(
    "services.upstream_breakers",
    "Breakers that stop calls to an upstream while it has told this deployment to wait. Each owns the service keys its ``covers`` accepts.",
)

PROVIDER_GROUPS: ExtensionPoint[ProviderGroup] = ExtensionPoint(
    "services.provider_groups",
    "Sets of providers that fail together because one service sits behind them, so the provider-health digest can name that service instead of the network.",
)

EGRESS_DECISIONS: FilterHook[EgressDecision, [str]] = FilterHook(
    "services.egress_decisions",
    "A service's egress decision, after the environment's policy. Receives the decision so far and the service key, and returns it or another: a plugin whose service depends on an upstream the deployment opted in by name may admit a call the policy refused.",
)

QUEUE_LIMITS: Registry[str, QueueLimits] = Registry(
    "tasks.queue_limits",
    "Time limits for a Celery queue a plugin's tasks run on, by queue name: the default soft and hard limits of its tasks, and the longest hard limit one may declare. A queue named here is one core does not otherwise know.",
)

LOCKED_DOWN_PROCESS_ROLES: ExtensionPoint[str] = ExtensionPoint(
    "tasks.locked_down_process_roles",
    "``UL_PROCESS_ROLE`` values of containers that run with reduced trust. Such a process always denies parsing untrusted bytes and never reaches data services that hold credentials.",
)

DATABASE_ROLES: ExtensionPoint[DatabaseRole] = ExtensionPoint(
    "database.roles",
    "Postgres login roles for process tiers a plugin deploys, each with its own connection budget, added to core's.",
)

TEST_GUARDS: ExtensionPoint[Callable[[], AbstractContextManager[None]]] = ExtensionPoint(
    "testing.guards",
    "Context-manager factories a test run enters for its whole duration, so a plugin can stop tests reaching a paid or networked service and pin the settings tests need.",
)

API_LIMIT_CATEGORIES: Registry[str, str] = Registry(
    "services.api_limit_categories",
    "The tab each service is listed under on the API limits page, by service key. A service not named here is listed under Other.",
)

__all__ = ["API_LIMIT_CATEGORIES", "DATABASE_ROLES", "EGRESS_DECISIONS", "LOCKED_DOWN_PROCESS_ROLES", "PROVIDER_GROUPS", "QUEUE_LIMITS", "SERVICE_DEFAULTS", "TEST_GUARDS", "UNLEDGERED_SERVICES", "UPSTREAM_BREAKERS"]
