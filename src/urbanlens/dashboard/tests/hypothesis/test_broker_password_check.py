"""A deployment still on RabbitMQ's published default password is warned about; local development is not."""

from __future__ import annotations

from django.test import override_settings

from urbanlens.core.tests.testcase import SimpleTestCase
from urbanlens.dashboard.checks import check_broker_password_is_not_the_default

_DEFAULT = "amqp://urbanlens:guest@urbanlens_rabbitmq:5672/urbanlens"


class BrokerPasswordCheckTests(SimpleTestCase):
    def test_a_deployment_on_the_default_password_is_warned(self) -> None:
        for environment in ("production", "staging"):
            with (
                self.subTest(environment=environment),
                override_settings(ENVIRONMENT_NAME=environment, CELERY_BROKER_URL=_DEFAULT),
            ):
                self.assertEqual(
                    [warning.id for warning in check_broker_password_is_not_the_default()], ["dashboard.W005"]
                )

    def test_its_own_password_or_local_development_passes(self) -> None:
        with override_settings(ENVIRONMENT_NAME="production", CELERY_BROKER_URL=_DEFAULT.replace("guest", "s3cret")):
            self.assertEqual(check_broker_password_is_not_the_default(), [])
        with override_settings(ENVIRONMENT_NAME="development", CELERY_BROKER_URL=_DEFAULT):
            self.assertEqual(check_broker_password_is_not_the_default(), [])
