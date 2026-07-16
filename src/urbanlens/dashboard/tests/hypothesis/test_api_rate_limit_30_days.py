"""Tests for the API rate limiter's new rolling 30-day window.

Covers ApiRateLimit.calls_per_30_days, ServiceDefaults' matching field,
check_rate_limit()'s new window check, and SiteAdminApiLimitsView's POST
handling of the new form field / category grouping for the tabs UI.
"""

from __future__ import annotations

from datetime import timedelta
from unittest.mock import patch

from django.contrib.auth.models import User
from django.test import Client
from django.urls import reverse
from django.utils import timezone
from model_bakery import baker

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.models.api_call_log.model import ApiCallLog
from urbanlens.dashboard.models.api_rate_limit.model import ApiRateLimit
from urbanlens.dashboard.services.rate_limiter import ServiceDefaults, check_rate_limit
from urbanlens.dashboard.services.site_admin import add_user_to_site_admin_group


def _log_call(service: str, *, days_ago: float = 0.0) -> ApiCallLog:
    entry = ApiCallLog.objects.create(service=service, success=True)
    if days_ago:
        ApiCallLog.objects.filter(pk=entry.pk).update(created=timezone.now() - timedelta(days=days_ago))
    return entry


class ServiceDefaultsThirtyDayFieldTests(TestCase):
    def test_defaults_to_none(self) -> None:
        defaults = ServiceDefaults(display_name="Test Service")
        self.assertIsNone(defaults.calls_per_30_days)

    def test_can_be_set(self) -> None:
        defaults = ServiceDefaults(display_name="Test Service", calls_per_30_days=300)
        self.assertEqual(defaults.calls_per_30_days, 300)


class CheckRateLimitThirtyDayWindowTests(TestCase):
    def setUp(self) -> None:
        super().setUp()
        self.service = "test_30d_service"
        ApiRateLimit.objects.create(
            service=self.service,
            display_name="Test 30-Day Service",
            calls_per_minute=None,
            calls_per_day=None,
            calls_per_30_days=3,
        )

    def test_none_configured_never_blocks(self) -> None:
        ApiRateLimit.objects.filter(service=self.service).update(calls_per_30_days=None)
        for _ in range(10):
            _log_call(self.service)
        self.assertTrue(check_rate_limit(self.service))

    def test_under_the_limit_is_allowed(self) -> None:
        _log_call(self.service)
        _log_call(self.service)
        self.assertTrue(check_rate_limit(self.service))

    def test_at_the_limit_is_blocked(self) -> None:
        for _ in range(3):
            _log_call(self.service)
        self.assertFalse(check_rate_limit(self.service))

    def test_calls_older_than_30_days_do_not_count(self) -> None:
        for _ in range(5):
            _log_call(self.service, days_ago=31)
        self.assertTrue(check_rate_limit(self.service))

    def test_geo_filtered_calls_do_not_count(self) -> None:
        for _ in range(5):
            entry = _log_call(self.service)
            ApiCallLog.objects.filter(pk=entry.pk).update(was_geo_filtered=True)
        self.assertTrue(check_rate_limit(self.service))


class ApiLimitsAdminPageThirtyDayFieldTests(TestCase):
    def setUp(self) -> None:
        super().setUp()
        self.admin = baker.make(User)
        add_user_to_site_admin_group(self.admin)
        self.client = Client()
        self.client.force_login(self.admin)
        self.cfg = ApiRateLimit.objects.create(service="test_admin_service", display_name="Test Admin Service", calls_per_minute=10, calls_per_day=100)

    def test_post_saves_calls_per_30_days(self) -> None:
        self.client.post(
            reverse("site_admin_api_limits"),
            {"service": self.cfg.service, "enabled": "on", "calls_per_minute": "10", "calls_per_day": "100", "calls_per_30_days": "3000", "notes": ""},
        )
        self.cfg.refresh_from_db()
        self.assertEqual(self.cfg.calls_per_30_days, 3000)

    def test_blank_calls_per_30_days_clears_it(self) -> None:
        self.cfg.calls_per_30_days = 500
        self.cfg.save(update_fields=["calls_per_30_days"])
        self.client.post(
            reverse("site_admin_api_limits"),
            {"service": self.cfg.service, "enabled": "on", "calls_per_minute": "10", "calls_per_day": "100", "calls_per_30_days": "", "notes": ""},
        )
        self.cfg.refresh_from_db()
        self.assertIsNone(self.cfg.calls_per_30_days)

    def test_page_renders_category_tabs(self) -> None:
        response = self.client.get(reverse("site_admin_api_limits"))
        self.assertEqual(response.status_code, 200)
        self.assertIn("tabs", response.context)
        tab_names = [tab["name"] for tab in response.context["tabs"]]
        self.assertIn("Other", tab_names)
        # "Other" (the uncategorized catch-all) must always sort last, not by name.
        self.assertEqual(tab_names[-1], "Other")

    def test_known_service_is_categorized_not_left_in_other(self) -> None:
        with patch("urbanlens.dashboard.services.rate_limiter.all_service_defaults", return_value={"wikipedia": ServiceDefaults(display_name="Wikipedia")}):
            response = self.client.get(reverse("site_admin_api_limits"))
        tab_names = [tab["name"] for tab in response.context["tabs"]]
        self.assertIn("Reference & Archives", tab_names)
