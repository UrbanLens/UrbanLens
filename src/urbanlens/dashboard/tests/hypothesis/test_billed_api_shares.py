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
from urbanlens.dashboard.services.core.rate_limiter import (
    BILLED_API_SHARE_BY_ENVIRONMENT,
    UNKNOWN_ENVIRONMENT_BILLED_API_SHARE,
    all_service_defaults,
    billed_api_share,
    check_rate_limit,
    free_tier_ceiling,
)

#: The Google Maps Platform SKUs REData can also bill on the same account; UrbanLens takes at most 0.4.
SHARED_WITH_REDATA = ("google_geocoding", "google_places", "azure_maps")
BILLED_WITH_A_FREE_TIER = ("google_geocoding", "google_places", "google_maps", "azure_maps")


def _share(value: float | None):
    return patch.object(rate_limiter, "_configured_billed_api_share", return_value=value)


class EnvironmentShareTests(SimpleTestCase):
    def test_the_default_shares_sum_to_no_more_than_the_whole_allotment(self) -> None:
        self.assertLessEqual(sum(BILLED_API_SHARE_BY_ENVIRONMENT.values()), 1.0)

    def test_production_takes_most_and_everything_else_a_small_share(self) -> None:
        self.assertEqual(BILLED_API_SHARE_BY_ENVIRONMENT["production"], 0.8)
        for environment in ("staging", "development", "local"):
            with self.subTest(environment=environment):
                self.assertLessEqual(BILLED_API_SHARE_BY_ENVIRONMENT[environment], 0.1)
        self.assertEqual(UNKNOWN_ENVIRONMENT_BILLED_API_SHARE, 0.0)

    def test_the_environment_picks_the_default_outside_tests(self) -> None:
        for environment, share in (
            ("production", 0.8),
            ("staging", 0.1),
            ("development", 0.05),
            ("someones-laptop", 0.0),
        ):
            with (
                self.subTest(environment=environment),
                override_settings(TESTING=False, ENVIRONMENT_NAME=environment),
                _share(None),
            ):
                self.assertEqual(billed_api_share(), share)

    def test_the_env_var_wins_including_zero(self) -> None:
        for configured in (0.0, 0.3):
            with (
                self.subTest(configured=configured),
                override_settings(TESTING=False, ENVIRONMENT_NAME="production"),
                _share(configured),
            ):
                self.assertEqual(billed_api_share(), configured)

    def test_tests_take_the_whole_allotment_since_none_reaches_a_vendor(self) -> None:
        with _share(None):
            self.assertEqual(billed_api_share(), 1.0)


class FreeTierCeilingTests(SimpleTestCase):
    def test_every_billed_google_or_azure_service_declares_its_free_tier(self) -> None:
        defaults = all_service_defaults()
        for service in BILLED_WITH_A_FREE_TIER:
            with self.subTest(service=service):
                self.assertIsNotNone(defaults[service].free_tier_per_calendar_month)

    def test_urbanlens_takes_at_most_four_tenths_of_a_sku_redata_also_bills(self) -> None:
        defaults = all_service_defaults()
        for service in SHARED_WITH_REDATA:
            with self.subTest(service=service):
                self.assertLessEqual(defaults[service].free_tier_allotment, 0.4)

    def test_the_ceiling_is_the_allowance_times_the_allotment_times_the_share(self) -> None:
        with _share(0.8):
            self.assertEqual(free_tier_ceiling("google_geocoding"), 3_200)
            self.assertEqual(free_tier_ceiling("google_places"), 320)
            self.assertEqual(free_tier_ceiling("google_maps"), 7_200)

    def test_a_free_service_has_no_ceiling(self) -> None:
        self.assertIsNone(free_tier_ceiling("overpass"))

    def test_unreadable_plugin_defaults_fall_back_to_the_core_registry_and_hold_a_plugin_service_at_zero(self) -> None:
        with (
            _share(0.8),
            patch.object(rate_limiter, "all_service_defaults", side_effect=RuntimeError("plugin broke")),
            self.assertLogs(rate_limiter.logger, "ERROR"),
        ):
            self.assertEqual(free_tier_ceiling("google_geocoding"), 3_200)
            self.assertEqual(free_tier_ceiling("some_plugin_only_service"), 0)


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
