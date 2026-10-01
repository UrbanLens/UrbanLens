"""A deployment missing configuration refuses to start instead of running on a local default.

Each test reloads ``settings.base`` under a controlled environment and a command line that is not pytest's, because
every guard stands aside under pytest (test settings import ``base`` before overriding what it needs). Keys are set
to "" rather than removed, so ``load_dotenv`` cannot refill them from a checkout's ``.env``.
"""

from __future__ import annotations

import importlib
from types import ModuleType
from unittest import mock

from django.core.exceptions import ImproperlyConfigured

from urbanlens.core.tests.testcase import SimpleTestCase
from urbanlens.UrbanLens.environments.factory import select_environment
from urbanlens.UrbanLens.environments.meta import EnvironmentTypes, environment_from_env
from urbanlens.UrbanLens.environments.prod import Production
from urbanlens.UrbanLens.settings import _env
from urbanlens.UrbanLens.settings.app import AppSettings
import urbanlens.UrbanLens.settings.base as settings_base

#: Everything these guards read, so nothing from the real environment decides a result.
_INPUTS = (
    "UL_ENVIRONMENT",
    "DJANGO_SECRET_KEY",
    "DJANGO_DEBUG",
    "DJANGO_TESTING",
    "UL_SITE_URL",
    "UL_CELERY_BROKER_URL",
    "UL_RABBITMQ_URL",
    "UL_DRAGONFLY_URL",
    "UL_VALKEY_URL",
    "UL_REDIS_URL",
    "UL_CHANNEL_LAYER_URL",
    "UL_EMAIL_BACKEND",
    "UL_UNSAFE_ALLOW_HTTP",
)

#: A deployment configured completely.
_CONFIGURED = {
    "DJANGO_SECRET_KEY": "k" * 64,
    "UL_SITE_URL": "https://urbanlens.example",
    "UL_RABBITMQ_URL": "amqp://rabbit.example:5672/",
    "UL_DRAGONFLY_URL": "redis://dragonfly.example:6379/0",
}


class _ReloadsSettings(SimpleTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.addCleanup(importlib.reload, settings_base)

    def _reload(self, environment: str, **env: str) -> ModuleType:
        """Reload ``settings.base`` as a daphne process would see it.

        Args:
            environment: ``UL_ENVIRONMENT``; "" for unset.
            **env: The rest of the inputs; any not named are blank.

        Returns:
            The reloaded module.
        """
        full = dict.fromkeys(_INPUTS, "")
        full.update(env, UL_ENVIRONMENT=environment)
        with (
            mock.patch.dict("os.environ", full),
            mock.patch("sys.argv", ["daphne", "urbanlens.UrbanLens.asgi:application"]),
        ):
            return importlib.reload(settings_base)

    def _refuses(self, environment: str, **env: str) -> str:
        with self.assertRaises(ImproperlyConfigured) as caught:
            self._reload(environment, **env)
        return str(caught.exception)


class AnUnsetEnvironmentIsProductionTests(SimpleTestCase):
    """Compose, the entrypoint and the Dockerfile ARG all default to production; the settings used to say local."""

    def test_unset_and_blank_are_production(self) -> None:
        for environ in ({}, {"UL_ENVIRONMENT": ""}, {"UL_ENVIRONMENT": "   "}):
            with self.subTest(environ=environ):
                self.assertEqual(environment_from_env(environ), EnvironmentTypes.PRODUCTION)

    def test_a_known_name_is_normalised(self) -> None:
        self.assertEqual(environment_from_env({"UL_ENVIRONMENT": " Development\n"}), EnvironmentTypes.DEVELOPMENT)

    def test_an_unknown_name_refuses_rather_than_guessing(self) -> None:
        with self.assertRaisesRegex(ImproperlyConfigured, "prodution"):
            environment_from_env({"UL_ENVIRONMENT": "prodution"})

    def test_the_environment_object_agrees(self) -> None:
        """Site settings and the developer toolbar read this one; unset used to show admins the dev toolbar."""
        with mock.patch.dict("os.environ", {"UL_ENVIRONMENT": ""}):
            self.assertIsInstance(select_environment(None), Production)


class TheAppSettingsFieldReadsTheSameVariableTests(SimpleTestCase):
    """``AppSettings.environment_name`` read ``UL_ENVIRONMENT_NAME``, which nothing sets, and defaulted to local.

    The integration commands' production lock and ``seed_dev_environment``'s refusal both read it, so neither ever
    engaged on production."""

    def _fresh(self, value: str) -> AppSettings:
        # The metaclass caches one instance per class; a subclass gets its own, built from this environment.
        fresh_class = type("FreshAppSettings", (AppSettings,), {})
        # No env file: a blank variable is ignored and would fall through to the host's .env.
        with mock.patch.dict("os.environ", {"UL_ENVIRONMENT": value}):
            return fresh_class(_env_file=None)

    def test_production_is_seen_as_production(self) -> None:
        self.assertEqual(self._fresh("production").environment_name, "production")

    def test_it_is_parsed_the_same_way(self) -> None:
        self.assertEqual(self._fresh(" Staging ").environment_name, "staging")

    def test_unset_is_production_here_too(self) -> None:
        self.assertEqual(self._fresh("").environment_name, "production")


class RequiredDeploymentSettingsTests(_ReloadsSettings):
    def test_a_fully_configured_production_starts(self) -> None:
        reloaded = self._reload("production", **_CONFIGURED)

        self.assertEqual(reloaded.ENVIRONMENT_NAME, "production")
        self.assertFalse(reloaded.DEBUG)
        self.assertEqual(reloaded.SITE_URL, "https://urbanlens.example")
        self.assertEqual(reloaded.CELERY_BROKER_URL, "amqp://rabbit.example:5672/")

    def test_an_unset_environment_is_held_to_production_rules(self) -> None:
        """G6-16: an image run without UL_ENVIRONMENT got DEBUG on and a random SECRET_KEY."""
        self.assertIn("DJANGO_SECRET_KEY", self._refuses(""))
        reloaded = self._reload("", **_CONFIGURED)
        self.assertEqual(reloaded.ENVIRONMENT_NAME, "production")
        self.assertFalse(reloaded.DEBUG)

    def test_each_missing_setting_is_named(self) -> None:
        for missing, named in (
            ("UL_SITE_URL", "UL_SITE_URL"),
            ("UL_RABBITMQ_URL", "UL_RABBITMQ_URL"),
            ("UL_DRAGONFLY_URL", "UL_DRAGONFLY_URL"),
            ("DJANGO_SECRET_KEY", "DJANGO_SECRET_KEY"),
        ):
            for environment in ("production", "staging"):
                with self.subTest(missing=missing, environment=environment):
                    self.assertIn(named, self._refuses(environment, **{**_CONFIGURED, missing: ""}))

    def test_a_localhost_site_url_is_refused(self) -> None:
        """G3-6/G6-18: compose used to default it to http://localhost:<port>, so the old warning never fired."""
        for url in ("http://localhost:21800", "http://127.0.0.1", "https://app.localhost"):
            with self.subTest(url=url):
                self.assertIn("UL_SITE_URL", self._refuses("production", **{**_CONFIGURED, "UL_SITE_URL": url}))

    def test_the_broker_may_be_named_either_way(self) -> None:
        reloaded = self._reload(
            "production", **{**_CONFIGURED, "UL_RABBITMQ_URL": "", "UL_CELERY_BROKER_URL": "amqp://explicit/"}
        )
        self.assertEqual(reloaded.CELERY_BROKER_URL, "amqp://explicit/")

    def test_the_older_store_names_still_count(self) -> None:
        """damballa's stacks predate the rename and set only UL_VALKEY_URL."""
        reloaded = self._reload(
            "production", **{**_CONFIGURED, "UL_DRAGONFLY_URL": "", "UL_VALKEY_URL": "redis://valkey.example/0"}
        )
        self.assertEqual(reloaded.DRAGONFLY_URL, "redis://valkey.example/0")

    def test_debug_is_refused_outside_development(self) -> None:
        for environment in ("production", "staging"):
            with self.subTest(environment=environment):
                self.assertIn("DJANGO_DEBUG", self._refuses(environment, **_CONFIGURED, DJANGO_DEBUG="true"))

    def test_development_still_starts_with_nothing_configured(self) -> None:
        """The negative control: the guards must not reach local development."""
        for environment in ("local", "development", "testing"):
            with self.subTest(environment=environment):
                reloaded = self._reload(environment)
                self.assertTrue(reloaded.SITE_URL.startswith("http://localhost:"))
                self.assertEqual(reloaded.CELERY_BROKER_URL, "redis://localhost:6379/0")

    def test_development_may_still_debug(self) -> None:
        self.assertTrue(self._reload("development", DJANGO_DEBUG="true").DEBUG)


class DeploymentDefaultsTests(_ReloadsSettings):
    def test_an_unconfigured_mail_backend_sends_rather_than_prints_in_a_deployment(self) -> None:
        """The console backend drops safety alerts into a log without an error."""
        self.assertEqual(
            self._reload("production", **_CONFIGURED).EMAIL_DELIVERY_BACKEND,
            "django.core.mail.backends.smtp.EmailBackend",
        )
        self.assertEqual(
            self._reload("development").EMAIL_DELIVERY_BACKEND, "django.core.mail.backends.console.EmailBackend"
        )

    def test_a_configured_mail_backend_wins_everywhere(self) -> None:
        reloaded = self._reload(
            "production", **_CONFIGURED, UL_EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend"
        )
        self.assertEqual(reloaded.EMAIL_DELIVERY_BACKEND, "django.core.mail.backends.locmem.EmailBackend")

    def test_a_deployment_trusts_only_origins_it_serves(self) -> None:
        """It used to trust https://urbanlens.org and https://localhost:<port> on every install."""
        reloaded = self._reload("production", **_CONFIGURED)
        exact, wildcard = reloaded._derive_trusted_origins(
            reloaded.ALLOWED_HOSTS, "https://urbanlens.example", allow_http=False
        )

        self.assertEqual(reloaded.CORS_ALLOWED_ORIGINS, exact)
        self.assertEqual(reloaded.CSRF_TRUSTED_ORIGINS, [*exact, *wildcard])

    def test_development_keeps_its_localhost_origins(self) -> None:
        reloaded = self._reload("development", UL_UNSAFE_ALLOW_HTTP="true")
        self.assertIn(f"http://localhost:{reloaded._APP_PORT}", reloaded.CSRF_TRUSTED_ORIGINS)


class TheChannelLayerHasItsOwnStoreTests(_ReloadsSettings):
    """G4-21: the shared store refuses writes once full, and a refused group_send is a lost live message."""

    def _channel_host(self, module: ModuleType) -> str:
        return module.CHANNEL_LAYERS["default"]["CONFIG"]["hosts"][0]["address"]

    def test_it_uses_its_own_url(self) -> None:
        reloaded = self._reload("production", **_CONFIGURED, UL_CHANNEL_LAYER_URL="redis://channels.example:6379/0")

        self.assertEqual(self._channel_host(reloaded), "redis://channels.example:6379/0")
        self.assertEqual(reloaded.CACHES["default"]["LOCATION"], "redis://dragonfly.example:6379/0")

    def test_it_falls_back_to_the_shared_store(self) -> None:
        """A deployment that has not provisioned the instance keeps working, like the proxied-bytes split."""
        self.assertEqual(
            self._channel_host(self._reload("production", **_CONFIGURED)), "redis://dragonfly.example:6379/0"
        )


class TheGuardsStandAsideForTestsTests(SimpleTestCase):
    """Test settings import ``base`` first; a guard that fired under pytest would stop the suite loading."""

    def test_pytest_is_never_a_deployment(self) -> None:
        self.assertFalse(_env.deployment_settings_required("production", argv=["/app/.venv/bin/pytest", "-q"]))
        self.assertTrue(_env.deployment_settings_required("production", argv=["daphne"]))
        self.assertFalse(_env.deployment_settings_required("development", argv=["daphne"]))

    def test_a_missing_value_falls_back_only_where_allowed(self) -> None:
        kwargs = {"environment": "production", "fallback": "fb", "reason": "because"}
        self.assertEqual(_env.require_deployment_setting("X", " set ", argv=["daphne"], **kwargs), "set")
        with self.assertRaisesRegex(ImproperlyConfigured, "X must be set when UL_ENVIRONMENT is 'production'. because"):
            _env.require_deployment_setting("X", "  ", argv=["daphne"], **kwargs)
        self.assertEqual(_env.require_deployment_setting("X", None, argv=["pytest"], **kwargs), "fb")

    def test_loopback_hosts(self) -> None:
        for host in ("localhost", "LOCALHOST.", "127.0.0.1", "127.1.2.3", "::1", "app.localhost"):
            with self.subTest(host=host):
                self.assertTrue(_env.is_loopback_host(host))
        public: tuple[str | None, ...] = ("urbanlens.org", "localhost.example.com", "10.0.0.1", "", None)
        for candidate in public:
            with self.subTest(host=candidate):
                self.assertFalse(_env.is_loopback_host(candidate))
