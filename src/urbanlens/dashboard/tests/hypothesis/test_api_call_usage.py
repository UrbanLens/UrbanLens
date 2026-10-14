"""The API usage ledger records units (calls, models, tokens) and the site-admin usage report reads them."""

from __future__ import annotations

from unittest.mock import Mock, patch

from django.contrib.auth.models import User
from django.urls import reverse
from model_bakery import baker

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.models.api_call_log.model import ApiCallLog
from urbanlens.dashboard.services.admin.site_admin import add_user_to_site_admin_group
from urbanlens.dashboard.services.core.rate_limiter import ServiceDefaults, _RateLimitedSession


class SummaryByServiceTests(TestCase):
    def test_counts_calls_rather_than_rows(self) -> None:
        ApiCallLog.objects.create(service="svc", success=True, calls=5)
        ApiCallLog.objects.create(service="svc", success=True)

        summary = {row["service"]: row for row in ApiCallLog.objects.summary_by_service()}

        self.assertEqual(summary["svc"]["total"], 6)

    def test_reports_no_money(self) -> None:
        ApiCallLog.objects.create(service="svc", success=True)

        summary = {row["service"]: row for row in ApiCallLog.objects.summary_by_service()}

        self.assertEqual({key for key in summary["svc"] if "cost" in key}, set())


class SessionRecordsEachCallTests(TestCase):
    """_RateLimitedSession logs one row per request, successful or not."""

    def _mock_session(self, service_key: str, *, ok: bool) -> _RateLimitedSession:
        session = _RateLimitedSession(service_key)
        session._session = Mock()
        session._session.request.return_value = Mock(ok=ok, status_code=200 if ok else 500)
        return session

    def _get(self, service_key: str, *, ok: bool) -> None:
        defaults = {service_key: ServiceDefaults(display_name="Svc")}
        with (
            patch("urbanlens.dashboard.services.core.rate_limiter.check_rate_limit", return_value=True),
            patch("urbanlens.dashboard.services.core.rate_limiter.service_is_enabled", return_value=True),
            patch("urbanlens.dashboard.services.core.rate_limiter.all_service_defaults", return_value=defaults),
        ):
            self._mock_session(service_key, ok=ok).get("https://example.com/api")

    def test_a_successful_call_is_logged_as_a_success(self) -> None:
        self._get("some_svc", ok=True)

        entry = ApiCallLog.objects.get(service="some_svc")
        self.assertTrue(entry.success)
        self.assertEqual(entry.status_code, 200)

    def test_a_failed_response_is_logged_as_a_failure(self) -> None:
        self._get("some_svc", ok=False)

        entry = ApiCallLog.objects.get(service="some_svc")
        self.assertFalse(entry.success)
        self.assertEqual(entry.status_code, 500)


class SiteAdminApiUsageIncludesPluginsTests(TestCase):
    """SiteAdminStatsApiUsagePartialView used to only iterate SERVICE_REGISTRY, silently omitting every plugin-declared service (the great majority of this app's integrations)."""

    def setUp(self) -> None:
        super().setUp()
        self.admin = baker.make(User)
        add_user_to_site_admin_group(self.admin)
        self.client.force_login(self.admin)

    def test_plugin_only_service_appears_in_the_report(self) -> None:
        defaults = {"a_plugin_service": ServiceDefaults(display_name="A Plugin Service")}
        with patch("urbanlens.dashboard.services.core.rate_limiter.all_service_defaults", return_value=defaults):
            response = self.client.get(reverse("site_admin_stats_api"))

        self.assertContains(response, "A Plugin Service")

    def test_a_services_calls_are_reported(self) -> None:
        defaults = {"used_svc": ServiceDefaults(display_name="Used Svc")}
        ApiCallLog.objects.create(service="used_svc", success=True, calls=7)
        with patch("urbanlens.dashboard.services.core.rate_limiter.all_service_defaults", return_value=defaults):
            response = self.client.get(reverse("site_admin_stats_api"))

        self.assertContains(response, "Used Svc")
        self.assertContains(response, '<td style="text-align:right">7</td>', html=False)

    def test_the_report_shows_no_dollar_figure(self) -> None:
        defaults = {"used_svc": ServiceDefaults(display_name="Used Svc")}
        ApiCallLog.objects.create(service="used_svc", success=True)
        with patch("urbanlens.dashboard.services.core.rate_limiter.all_service_defaults", return_value=defaults):
            response = self.client.get(reverse("site_admin_stats_api"))

        self.assertNotContains(response, "$")

    def test_requires_site_admin_permission_before_and_after_promotion(self) -> None:
        """Gated by ``dashboard.view_site_admin`` - not automatic for any logged-in user."""
        user = baker.make(User)
        self.client.force_login(user)

        response = self.client.get(reverse("site_admin_stats_api"))
        self.assertEqual(response.status_code, 403)

        add_user_to_site_admin_group(user)
        response = self.client.get(reverse("site_admin_stats_api"))
        self.assertEqual(response.status_code, 200)
