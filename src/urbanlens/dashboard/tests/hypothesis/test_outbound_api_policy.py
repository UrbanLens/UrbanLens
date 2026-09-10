"""Which deployments may call a paid provider at all.

`rate_limiter._reserve_call` is the single point every outbound gateway call
passes through, which is why the demo's spend guard lives there rather than in
each of the 58 plugins. This covers that guard, and the development rule added
beside it.

**Why development needed one.** A load run on the development stack imported
1,000 pins. The `Pin` post_save chain enqueued 2,644 tasks, most of them
`enrich_wiki_location`, and the worker spent hours calling
`https://redata.urbanlens.org` - the *production* REData, from a dev machine,
with a live key - which fans out to Google Places and answers `429 ... request
budget is exhausted` (P109).

The demo guard exempts REData on the grounds that it "costs nothing but our own
capacity". That is true of the demo, which points at our own instance and is the
thing REData exists to show off. It is not true of a development box, where the
same call reaches a billable provider one hop later. So the development rule
does not carry the exemption.

Refusing rather than mocking, deliberately: a refused call raises
`ServiceDisabledError` from the same place a rate limit does, so every caller
takes a path it already has to handle - providers do fail - and no fixture has
to be kept in step with a provider's real shape.
"""

from __future__ import annotations

from collections.abc import Iterator
import contextlib
from unittest import mock

from django.conf import settings as django_settings
from django.test import TestCase, override_settings

from urbanlens.dashboard.services.core.rate_limiter import outbound_calls_permitted
from urbanlens.UrbanLens.environments.meta import EnvironmentTypes

#: A keyed provider that bills per call.
PAID = "google_places"

#: This project's own service. Exempt on the demo, not on a dev box.
OWN = "redata_place_details"


@contextlib.contextmanager
def _deployment(
    environment: str = EnvironmentTypes.PRODUCTION, *, demo: bool = False, allow: bool | None = None
) -> Iterator[None]:
    """Pretend to be one deployment.

    `ENVIRONMENT_NAME` is a Django setting (from ``UL_ENVIRONMENT``) while
    `demo_mode` and `allow_outbound_apis` are Pydantic app settings, so this
    needs both mechanisms. `TESTING` is forced off because the suite itself sets
    it, and with it on the guard permits everything by design - leaving it alone
    would make every assertion below vacuous.

    ``allow=None`` is the real default and means the variable is unset, which is
    what almost every deployment looks like.
    """
    with (
        override_settings(ENVIRONMENT_NAME=str(environment), TESTING=False),
        mock.patch.multiple("urbanlens.UrbanLens.settings.app.settings", demo_mode=demo, allow_outbound_apis=allow),
    ):
        yield


class TheSettingsThisReliesOnExistTests(TestCase):
    """`override_settings` will happily invent a name production does not have.

    Every test below would pass against a guard reading a setting that only
    exists inside these tests, so the names are checked against the real
    settings module first.
    """

    def test_environment_name_is_a_real_setting(self) -> None:
        self.assertTrue(hasattr(django_settings, "ENVIRONMENT_NAME"))

    def test_testing_is_a_real_setting(self) -> None:
        self.assertTrue(hasattr(django_settings, "TESTING"))

    def test_the_suite_really_does_set_testing(self) -> None:
        """If this ever became False, the guard would start refusing mid-suite."""
        self.assertTrue(django_settings.TESTING)


class TheProductionTrapTests(TestCase):
    """The bug this guard nearly shipped with, pinned so it cannot come back.

    `app_settings.environment_name` looks like the obvious thing to read and is
    not wired to `UL_ENVIRONMENT`: it is a separate Pydantic field defaulting to
    `local`. A production deployment that never sets `UL_ENVIRONMENT_NAME`
    reports `local` from it, so a guard reading it would have refused every
    outbound call in production.
    """

    def test_production_still_calls_out_when_the_pydantic_field_says_local(self) -> None:
        with (
            _deployment(EnvironmentTypes.PRODUCTION),
            mock.patch.multiple("urbanlens.UrbanLens.settings.app.settings", environment_name=EnvironmentTypes.LOCAL),
        ):
            self.assertTrue(outbound_calls_permitted(PAID))


class UnderTheSuiteTests(TestCase):
    """The suite inherits a development `UL_ENVIRONMENT` from its container."""

    def test_the_guard_stands_aside_for_tests(self) -> None:
        """Otherwise every gateway in every test would be refused."""
        with override_settings(ENVIRONMENT_NAME=str(EnvironmentTypes.DEVELOPMENT), TESTING=True):
            self.assertTrue(outbound_calls_permitted(PAID))


class DeploymentsThatSpendTests(TestCase):
    """Where the budget is real, every call goes out."""

    def test_production_permits_a_paid_provider(self) -> None:
        with _deployment(EnvironmentTypes.PRODUCTION):
            self.assertTrue(outbound_calls_permitted(PAID))

    def test_staging_permits_a_paid_provider(self) -> None:
        """Staging is a deployment people look at; it has to show real data."""
        with _deployment(EnvironmentTypes.STAGING):
            self.assertTrue(outbound_calls_permitted(PAID))


class TheDemoGuardTests(TestCase):
    """Unchanged behaviour, pinned because it had no test."""

    def test_the_demo_refuses_a_paid_provider(self) -> None:
        with _deployment(demo=True):
            self.assertFalse(outbound_calls_permitted(PAID))

    def test_the_demo_still_calls_our_own_service(self) -> None:
        """The demo exists to show REData off, and points at our own instance."""
        with _deployment(demo=True):
            self.assertTrue(outbound_calls_permitted(OWN))


class DevelopmentSpendsNothingTests(TestCase):
    """The rule this file was written for."""

    def test_development_refuses_a_paid_provider(self) -> None:
        with _deployment(EnvironmentTypes.DEVELOPMENT):
            self.assertFalse(outbound_calls_permitted(PAID))

    def test_development_refuses_our_own_service_too(self) -> None:
        """The demo's exemption does not carry over, and this is the whole point.

        A dev box's REData URL is the production one, and REData reaches a paid
        provider one hop later - so "it is only our own capacity" stops being
        true at the boundary the demo guard was reasoning about.
        """
        with _deployment(EnvironmentTypes.DEVELOPMENT):
            self.assertFalse(outbound_calls_permitted(OWN))

    def test_local_refuses_too(self) -> None:
        """A checkout run outside Docker is the same machine and the same key."""
        with _deployment(EnvironmentTypes.LOCAL):
            self.assertFalse(outbound_calls_permitted(PAID))

    def test_a_developer_can_opt_back_in(self) -> None:
        """Working *on* an integration needs the real thing; it just is not the default."""
        with _deployment(EnvironmentTypes.DEVELOPMENT, allow=True):
            self.assertTrue(outbound_calls_permitted(PAID))
            self.assertTrue(outbound_calls_permitted(OWN))

    def test_the_opt_in_does_not_reopen_the_demo(self) -> None:
        """Two different guards for two different budgets; neither overrides the other."""
        with _deployment(EnvironmentTypes.DEVELOPMENT, demo=True, allow=True):
            self.assertFalse(outbound_calls_permitted(PAID))


class TheGuardIsWiredAtTheChokePointTests(TestCase):
    """Not merely a function that returns the right answer.

    `service_is_enabled` is what `_reserve_call` consults, and it is reached by
    every gateway in the project. A guard that were correct but unreferenced
    would pass every test above and stop nothing.
    """

    def test_service_is_enabled_refuses_on_development(self) -> None:
        from urbanlens.dashboard.services.core.rate_limiter import service_is_enabled

        with _deployment(EnvironmentTypes.DEVELOPMENT):
            self.assertFalse(service_is_enabled(PAID))

    def test_service_is_enabled_is_not_short_circuited_by_a_supplied_config(self) -> None:
        """The guard runs before the caller's own row is trusted.

        `_reserve_call` holds the `ApiRateLimit` row and passes it in to save
        re-reads. If the guard were checked after that, an enabled row would let
        the call straight through.
        """
        from urbanlens.dashboard.services.core.rate_limiter import service_is_enabled

        enabled_row = mock.Mock(enabled=True)
        with _deployment(EnvironmentTypes.DEVELOPMENT):
            self.assertFalse(service_is_enabled(PAID, config=enabled_row))


class AnExplicitAnswerWinsTests(TestCase):
    """`UL_ALLOW_OUTBOUND_APIS` overrides the environment's default both ways.

    The direction that matters is `false` on a deployment the environment would
    otherwise trust. `dev_env.py --environment staging` sets
    `UL_ENVIRONMENT=staging` purely to get gunicorn - infra's own comment says
    "this is one axis, not two: the application branches on UL_ENVIRONMENT
    alone" - so a throwaway environment is otherwise indistinguishable from the
    real staging deployment and would call providers for real.
    """

    def test_a_throwaway_staging_environment_can_refuse(self) -> None:
        with _deployment(EnvironmentTypes.STAGING, allow=False):
            self.assertFalse(outbound_calls_permitted(PAID))

    def test_a_real_deployment_can_be_switched_off_during_an_incident(self) -> None:
        """Taking a provider out of the path should not need a code change."""
        with _deployment(EnvironmentTypes.PRODUCTION, allow=False):
            self.assertFalse(outbound_calls_permitted(PAID))

    def test_unset_still_means_ask_the_environment(self) -> None:
        with _deployment(EnvironmentTypes.PRODUCTION, allow=None):
            self.assertTrue(outbound_calls_permitted(PAID))
        with _deployment(EnvironmentTypes.DEVELOPMENT, allow=None):
            self.assertFalse(outbound_calls_permitted(PAID))

    def test_it_cannot_reopen_the_demo(self) -> None:
        """The demo's budget is a different question, asked first."""
        with _deployment(EnvironmentTypes.PRODUCTION, demo=True, allow=True):
            self.assertFalse(outbound_calls_permitted(PAID))

    def test_it_cannot_close_the_demos_own_service(self) -> None:
        """Nor should the flag be able to break the demo by switching REData off."""
        with _deployment(EnvironmentTypes.PRODUCTION, demo=True, allow=False):
            self.assertTrue(outbound_calls_permitted(OWN))
