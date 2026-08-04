"""Tests for the API rate limiter's minimum-interval (spacing) enforcement.

Covers ApiRateLimit.min_interval_seconds/last_call_at, ServiceDefaults' matching
field, _reserve_call()'s new spacing check, SiteAdminApiLimitsView's POST
handling of the new form field, and the GDELT/Nominatim defaults that motivated
this feature - a rolling per-minute *count* doesn't guarantee even spacing
between calls, which is what these providers actually require.
"""

from __future__ import annotations

from datetime import timedelta

from django.contrib.auth.models import User
from django.test import Client
from django.urls import reverse
from django.utils import timezone
from model_bakery import baker

from urbanlens.core.tests.testcase import SimpleTestCase, TestCase
from urbanlens.dashboard.models.api_call_log.model import ApiCallLog
from urbanlens.dashboard.models.api_rate_limit.model import ApiRateLimit
from urbanlens.dashboard.services.admin.site_admin import add_user_to_site_admin_group
from urbanlens.dashboard.services.core.rate_limiter import SERVICE_REGISTRY, RateLimitExceededError, ServiceDefaults, _reserve_call


class ServiceDefaultsMinIntervalFieldTests(SimpleTestCase):
    def test_defaults_to_none(self) -> None:
        self.assertIsNone(ServiceDefaults(display_name="Test Service").min_interval_seconds)

    def test_can_be_set(self) -> None:
        defaults = ServiceDefaults(display_name="Test Service", min_interval_seconds=5.0)
        self.assertEqual(defaults.min_interval_seconds, 5.0)


class ReserveCallMinIntervalTests(TestCase):
    def setUp(self) -> None:
        super().setUp()
        self.service = "test_min_interval_service"
        self.cfg = ApiRateLimit.objects.create(
            service=self.service,
            display_name="Test Min Interval Service",
            calls_per_minute=None,
            calls_per_day=None,
            calls_per_30_days=None,
            min_interval_seconds=5.0,
        )

    def test_first_ever_call_is_allowed_and_stamps_last_call_at(self) -> None:
        self.assertIsNone(self.cfg.last_call_at)
        _reserve_call(self.service)
        self.cfg.refresh_from_db()
        self.assertIsNotNone(self.cfg.last_call_at)

    def test_call_before_interval_elapses_is_blocked(self) -> None:
        ApiRateLimit.objects.filter(service=self.service).update(last_call_at=timezone.now() - timedelta(seconds=1))
        with self.assertRaises(RateLimitExceededError):
            _reserve_call(self.service)

    def test_blocked_call_logs_a_failed_rate_limited_entry(self) -> None:
        ApiRateLimit.objects.filter(service=self.service).update(last_call_at=timezone.now() - timedelta(seconds=1))
        with self.assertRaises(RateLimitExceededError):
            _reserve_call(self.service)
        entry = ApiCallLog.objects.filter(service=self.service, was_rate_limited=True).latest("created")
        self.assertFalse(entry.success)

    def test_blocked_call_does_not_advance_last_call_at(self) -> None:
        stale = timezone.now() - timedelta(seconds=1)
        ApiRateLimit.objects.filter(service=self.service).update(last_call_at=stale)
        with self.assertRaises(RateLimitExceededError):
            _reserve_call(self.service)
        self.cfg.refresh_from_db()
        self.assertEqual(self.cfg.last_call_at, stale)

    def test_call_after_interval_elapses_is_allowed(self) -> None:
        ApiRateLimit.objects.filter(service=self.service).update(last_call_at=timezone.now() - timedelta(seconds=10))
        entry_pk = _reserve_call(self.service)
        self.assertIsNotNone(ApiCallLog.objects.get(pk=entry_pk))

    def test_allowed_call_advances_last_call_at(self) -> None:
        ApiRateLimit.objects.filter(service=self.service).update(last_call_at=timezone.now() - timedelta(seconds=10))
        _reserve_call(self.service)
        self.cfg.refresh_from_db()
        self.assertGreater(self.cfg.last_call_at, timezone.now() - timedelta(seconds=5))

    def test_no_min_interval_configured_never_blocks_on_spacing(self) -> None:
        ApiRateLimit.objects.filter(service=self.service).update(min_interval_seconds=None, last_call_at=timezone.now())
        # Immediately after a call with no min_interval configured - would
        # block if the spacing check ever ran unconditionally on last_call_at.
        _reserve_call(self.service)


class ApiLimitsAdminPageMinIntervalFieldTests(TestCase):
    def setUp(self) -> None:
        super().setUp()
        self.admin = baker.make(User)
        add_user_to_site_admin_group(self.admin)
        self.client = Client()
        self.client.force_login(self.admin)
        self.cfg = ApiRateLimit.objects.create(service="test_admin_service", display_name="Test Admin Service", calls_per_minute=10, calls_per_day=100)

    def test_post_saves_min_interval_seconds(self) -> None:
        self.client.post(
            reverse("site_admin_api_limits"),
            {"service": self.cfg.service, "enabled": "on", "calls_per_minute": "10", "calls_per_day": "100", "min_interval_seconds": "5", "notes": ""},
        )
        self.cfg.refresh_from_db()
        self.assertEqual(self.cfg.min_interval_seconds, 5.0)

    def test_blank_min_interval_seconds_clears_it(self) -> None:
        self.cfg.min_interval_seconds = 5.0
        self.cfg.save(update_fields=["min_interval_seconds"])
        self.client.post(
            reverse("site_admin_api_limits"),
            {"service": self.cfg.service, "enabled": "on", "calls_per_minute": "10", "calls_per_day": "100", "min_interval_seconds": "", "notes": ""},
        )
        self.cfg.refresh_from_db()
        self.assertIsNone(self.cfg.min_interval_seconds)


class ConfiguredMinIntervalDefaultsTests(SimpleTestCase):
    """Regression guard for the two services that motivated this feature."""

    def test_openhistoricalmap_requires_nominatim_spacing(self) -> None:
        self.assertEqual(SERVICE_REGISTRY["openhistoricalmap"].min_interval_seconds, 1.0)

    def test_gdelt_requires_gdelt_spacing(self) -> None:
        from urbanlens.dashboard.plugins.builtin.gdelt import GdeltPlugin

        self.assertEqual(GdeltPlugin().get_service_defaults()["gdelt"].min_interval_seconds, 5.0)
