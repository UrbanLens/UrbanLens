"""Billed APIs stay inside the vendor's free tier summed over every deployment that holds a key.

A vendor's free allowance belongs to the billing account, which UrbanLens's production, staging
and development deployments share with REData's. Each deployment therefore enforces the
vendor's allowance, times UrbanLens's allotment of it, times its own share.
"""

from __future__ import annotations

from datetime import timedelta
from unittest.mock import patch

from django.test import override_settings
from django.utils import timezone

from urbanlens.core.tests.testcase import SimpleTestCase, TestCase
from urbanlens.dashboard.models.api_call_log.model import ApiCallLog
from urbanlens.dashboard.models.api_rate_limit.model import ApiRateLimit
from urbanlens.dashboard.services.core import rate_limiter
from urbanlens.dashboard.services.core.egress import environment_share
from urbanlens.dashboard.services.core.rate_limiter import check_rate_limit, free_tier_ceiling
from urbanlens.UrbanLens.egress import ENVIRONMENT_SHARE_DEFAULTS, UNKNOWN_ENVIRONMENT_SHARE

#: The Google Maps Platform SKUs REData can also bill on the same account; UrbanLens takes at most 0.4.
SHARED_WITH_REDATA = ("google_geocoding", "google_places", "azure_maps")
BILLED_WITH_A_FREE_TIER = ("google_geocoding", "google_places", "google_maps", "azure_maps")


def _share(value: float):
    """This deployment's share of a service, as ``UL_ENVIRONMENT_SHARE`` or an override would set it."""
    return patch.object(rate_limiter, "_service_share", return_value=value)


def _configured(value: float | None):
    return patch("urbanlens.UrbanLens.settings.app.settings.environment_share", value)


class EnvironmentShareTests(SimpleTestCase):
    """``UL_ENVIRONMENT_SHARE`` replaced ``UL_BILLED_API_SHARE`` (D26); billed keeps #210's free-tier formula."""

    def test_the_deployment_defaults_sum_to_no_more_than_the_whole_allotment(self) -> None:
        deployments = ("production", "staging", "development", "local")
        self.assertLessEqual(sum(ENVIRONMENT_SHARE_DEFAULTS[environment] for environment in deployments), 1.0)

    def test_production_takes_the_bulk_and_staging_a_sliver(self) -> None:
        self.assertEqual(ENVIRONMENT_SHARE_DEFAULTS["production"], 0.9)
        self.assertEqual(ENVIRONMENT_SHARE_DEFAULTS["staging"], 0.05)
        self.assertEqual(ENVIRONMENT_SHARE_DEFAULTS["development"], 0.0)
        self.assertEqual(ENVIRONMENT_SHARE_DEFAULTS["local"], 0.0)
        self.assertEqual(UNKNOWN_ENVIRONMENT_SHARE, 0.0)

    def test_the_environment_picks_the_default_outside_tests(self) -> None:
        for environment, share in (
            ("production", 0.9),
            ("staging", 0.05),
            ("development", 0.0),
            ("someones-laptop", 0.0),
        ):
            with (
                self.subTest(environment=environment),
                override_settings(TESTING=False, ENVIRONMENT_NAME=environment),
                _configured(None),
            ):
                self.assertEqual(environment_share(), share)

    def test_the_env_var_wins_including_zero(self) -> None:
        for configured in (0.0, 0.3):
            with (
                self.subTest(configured=configured),
                override_settings(TESTING=False, ENVIRONMENT_NAME="production"),
                _configured(configured),
            ):
                self.assertEqual(environment_share(), configured)

    def test_tests_take_the_whole_allotment_since_none_reaches_a_vendor(self) -> None:
        """Whatever the host's .env says: the suite must not depend on it."""
        with _configured(0.0):
            self.assertEqual(environment_share(), 1.0)

    def test_production_s_free_tier_ceiling_follows_its_default_share(self) -> None:
        with override_settings(TESTING=False, ENVIRONMENT_NAME="production"), _configured(None):
            self.assertEqual(free_tier_ceiling("google_geocoding"), 3_600)

    def test_an_override_gives_one_billed_service_its_own_share(self) -> None:
        with (
            override_settings(TESTING=False, ENVIRONMENT_NAME="development"),
            _configured(None),
            patch("urbanlens.UrbanLens.settings.app.settings.environment_share_overrides", {"google_geocoding": 0.01}),
        ):
            self.assertEqual(free_tier_ceiling("google_geocoding"), 40)
            self.assertEqual(free_tier_ceiling("google_places"), 0)


class CeilingIsEnforcedTests(TestCase):
    def setUp(self) -> None:
        super().setUp()
        ApiRateLimit.objects.create(
            service="google_places", display_name="g", calls_per_minute=600, calls_per_day=10_000
        )

    def test_a_deployment_given_no_share_makes_no_billed_call(self) -> None:
        with _share(0.0):
            self.assertFalse(check_rate_limit("google_places"))

    def test_the_calendar_month_is_what_counts(self) -> None:
        with _share(0.02):  # 1,000 x 0.4 x 0.02 = 8 calls this month
            ApiCallLog.objects.bulk_create([ApiCallLog(service="google_places", success=True) for _ in range(7)])
            self.assertTrue(check_rate_limit("google_places"))
            ApiCallLog.objects.create(service="google_places", success=True)
            self.assertFalse(check_rate_limit("google_places"))

    def test_last_month_s_calls_do_not_count(self) -> None:
        with _share(0.02):
            rows = ApiCallLog.objects.bulk_create([ApiCallLog(service="google_places", success=True) for _ in range(8)])
            start_of_month = timezone.now().replace(day=1, hour=0, minute=0, second=0, microsecond=0)
            ApiCallLog.objects.filter(pk__in=[row.pk for row in rows]).update(
                created=start_of_month - timedelta(minutes=5)
            )
            self.assertTrue(check_rate_limit("google_places"))

    def test_a_refused_attempt_spends_none_of_the_free_tier(self) -> None:
        with _share(0.02):
            ApiCallLog.objects.bulk_create(
                [ApiCallLog(service="google_places", success=False, was_rate_limited=True) for _ in range(20)]
            )
            self.assertTrue(check_rate_limit("google_places"))
