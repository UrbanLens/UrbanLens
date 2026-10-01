"""H54 (P113): the Celery broker must not share Dragonfly's keyspace.

Before D16, Dragonfly held sessions, the Channels layer, the Django cache and the Celery broker in
one keyspace under ``volatile-lru``, and the broker's keys were the only ones with no TTL - so one
account's task backlog was paid for by everyone else's session and cache traffic. D16 moved the
broker to RabbitMQ (`settings/base.py`'s ``CELERY_BROKER_URL`` precedence:
``UL_CELERY_BROKER_URL or RABBITMQ_URL or DRAGONFLY_URL or "redis://localhost:6379/0"``), which
removes the broker from the shared keyspace entirely rather than bounding it there. No regression
test existed for this precedence when the fix landed; this backfills it.

These reload ``settings.base`` directly, the same way ``test_demo_login_view.py`` reloads the root
URLconf: the module is checked for its own recomputed attribute, not through ``django.conf.settings``,
which took its own copy of these names at Django startup and does not observe a later reload of the
module it copied them from.
"""

from __future__ import annotations

import importlib
from types import ModuleType
from unittest import mock

from urbanlens.core.tests.testcase import SimpleTestCase
import urbanlens.UrbanLens.settings.base as settings_base

_ENV_KEYS = ("UL_CELERY_BROKER_URL", "UL_RABBITMQ_URL", "UL_DRAGONFLY_URL", "UL_VALKEY_URL", "UL_REDIS_URL")


class CeleryBrokerPrecedenceTests(SimpleTestCase):
    """Reloads `settings.base` under a controlled environment, then restores the real one."""

    def setUp(self) -> None:
        super().setUp()
        self.addCleanup(importlib.reload, settings_base)

    def _reload_with(self, **env: str) -> ModuleType:
        """Reload `settings.base` with exactly `env` set for the broker/result-backend inputs.

        Every key in `_ENV_KEYS` not passed is cleared to "" (falsy to `os.getenv`'s `or`
        chain), so a stale value from the real environment can't leak into the reloaded module.
        """
        full_env = {key: env.get(key, "") for key in _ENV_KEYS}
        with mock.patch.dict("os.environ", full_env):
            return importlib.reload(settings_base)

    def test_rabbitmq_takes_the_broker_role_over_dragonfly(self) -> None:
        """The fixed shape: RabbitMQ is the broker, so its keys are never in Dragonfly's keyspace."""
        reloaded = self._reload_with(
            UL_RABBITMQ_URL="amqp://rabbit.example/",
            UL_DRAGONFLY_URL="redis://dragonfly.example/0",
        )

        self.assertEqual(reloaded.CELERY_BROKER_URL, "amqp://rabbit.example/")
        self.assertNotEqual(
            reloaded.CELERY_BROKER_URL,
            reloaded.DRAGONFLY_URL,
            "the broker resolved to Dragonfly's own URL - its keys would share Dragonfly's keyspace again",
        )

    def test_broker_falls_back_to_dragonfly_when_rabbitmq_is_not_configured(self) -> None:
        """The negative case: an environment with no RabbitMQ must keep working, on Dragonfly."""
        reloaded = self._reload_with(UL_DRAGONFLY_URL="redis://dragonfly.example/0")

        self.assertEqual(reloaded.CELERY_BROKER_URL, "redis://dragonfly.example/0")

    def test_broker_falls_back_to_local_redis_when_nothing_is_configured(self) -> None:
        """The second negative case: bare dev environment, no store configured at all."""
        reloaded = self._reload_with()

        self.assertEqual(reloaded.CELERY_BROKER_URL, "redis://localhost:6379/0")

    def test_explicit_broker_override_wins_over_rabbitmq_and_dragonfly(self) -> None:
        """`UL_CELERY_BROKER_URL` is an escape hatch and must outrank both defaults."""
        reloaded = self._reload_with(
            UL_CELERY_BROKER_URL="amqp://explicit.example/",
            UL_RABBITMQ_URL="amqp://rabbit.example/",
            UL_DRAGONFLY_URL="redis://dragonfly.example/0",
        )

        self.assertEqual(reloaded.CELERY_BROKER_URL, "amqp://explicit.example/")

    def test_result_backend_still_prefers_dragonfly_even_when_rabbitmq_is_the_broker(self) -> None:
        """The result backend is deliberately not moved: it wants a fast store, not a queue.

        A regression here would look like an improvement (`H54, done, and the result backend
        moved too`) but would put Celery's high-churn result/status writes back into the shared
        keyspace this fix removes the broker from.
        """
        reloaded = self._reload_with(
            UL_RABBITMQ_URL="amqp://rabbit.example/",
            UL_DRAGONFLY_URL="redis://dragonfly.example/0",
        )

        self.assertEqual(reloaded.CELERY_RESULT_BACKEND, "redis://dragonfly.example/0")
        self.assertNotEqual(reloaded.CELERY_RESULT_BACKEND, reloaded.CELERY_BROKER_URL)

    def test_result_backend_falls_back_to_the_broker_when_dragonfly_is_absent(self) -> None:
        """The negative case for the result backend: no Dragonfly, so it shares the broker's store."""
        reloaded = self._reload_with(UL_RABBITMQ_URL="amqp://rabbit.example/")

        self.assertEqual(reloaded.CELERY_RESULT_BACKEND, "amqp://rabbit.example/")
