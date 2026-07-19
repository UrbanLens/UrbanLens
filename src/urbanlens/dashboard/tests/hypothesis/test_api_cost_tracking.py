"""Tests for UL-52/53: per-call API cost estimates and their reporting.

Covers the full plumbing added for this ticket: ServiceDefaults.cost_per_call,
ApiCallLog.cost_estimate, _RateLimitedSession._do_request() populating it on
success only, ApiCallLogQuerySet.summary_by_service()'s total_cost
aggregation, the site-admin API usage report now covering plugin-declared
services (not just SERVICE_REGISTRY) plus its new cost column, and the
public costs page.
"""

from __future__ import annotations

from decimal import Decimal
from unittest.mock import Mock, patch

from django.contrib.auth.models import User
from django.urls import reverse
from model_bakery import baker

from urbanlens.core.tests.testcase import SimpleTestCase, TestCase
from urbanlens.dashboard.models.api_call_log.model import ApiCallLog
from urbanlens.dashboard.services.rate_limiter import ServiceDefaults, _RateLimitedSession
from urbanlens.dashboard.services.site_admin import add_user_to_site_admin_group


class ServiceDefaultsCostPerCallTests(SimpleTestCase):
    def test_defaults_to_none(self) -> None:
        self.assertIsNone(ServiceDefaults(display_name="Test").cost_per_call)

    def test_can_be_set(self) -> None:
        defaults = ServiceDefaults(display_name="Test", cost_per_call=Decimal("0.01"))
        self.assertEqual(defaults.cost_per_call, Decimal("0.01"))

    def test_google_geocoding_has_a_configured_cost(self) -> None:
        """Derived from that entry's own $200-credit/~40,000-calls note - a
        regression guard against silently losing the one seeded real value."""
        from urbanlens.dashboard.services.rate_limiter import SERVICE_REGISTRY

        self.assertEqual(SERVICE_REGISTRY["google_geocoding"].cost_per_call, Decimal("0.005"))


class SummaryByServiceTotalCostTests(TestCase):
    def test_sums_cost_estimate_across_calls(self) -> None:
        ApiCallLog.objects.create(service="priced_svc", success=True, cost_estimate=Decimal("0.01"))
        ApiCallLog.objects.create(service="priced_svc", success=True, cost_estimate=Decimal("0.02"))

        summary = {row["service"]: row for row in ApiCallLog.objects.summary_by_service()}

        self.assertEqual(summary["priced_svc"]["total_cost"], Decimal("0.03"))

    def test_unpriced_service_has_no_total_cost(self) -> None:
        ApiCallLog.objects.create(service="free_svc", success=True)

        summary = {row["service"]: row for row in ApiCallLog.objects.summary_by_service()}

        self.assertIsNone(summary["free_svc"]["total_cost"])


class DoRequestCostEstimateTests(TestCase):
    """_RateLimitedSession._do_request() populates cost_estimate on success only."""

    def _mock_session(self, service_key: str, *, ok: bool) -> _RateLimitedSession:
        session = _RateLimitedSession(service_key)
        session._session = Mock()
        session._session.request.return_value = Mock(ok=ok)
        return session

    def test_successful_call_to_a_priced_service_logs_its_cost(self) -> None:
        defaults = {"priced_svc": ServiceDefaults(display_name="Priced", cost_per_call=Decimal("0.05"))}
        session = self._mock_session("priced_svc", ok=True)
        with (
            patch("urbanlens.dashboard.services.rate_limiter.check_rate_limit", return_value=True),
            patch("urbanlens.dashboard.services.rate_limiter.service_is_enabled", return_value=True),
            patch("urbanlens.dashboard.services.rate_limiter.all_service_defaults", return_value=defaults),
        ):
            session.get("https://example.com/api")

        entry = ApiCallLog.objects.get(service="priced_svc")
        self.assertEqual(entry.cost_estimate, Decimal("0.05"))

    def test_successful_call_to_an_unpriced_service_logs_no_cost(self) -> None:
        defaults = {"free_svc": ServiceDefaults(display_name="Free")}
        session = self._mock_session("free_svc", ok=True)
        with (
            patch("urbanlens.dashboard.services.rate_limiter.check_rate_limit", return_value=True),
            patch("urbanlens.dashboard.services.rate_limiter.service_is_enabled", return_value=True),
            patch("urbanlens.dashboard.services.rate_limiter.all_service_defaults", return_value=defaults),
        ):
            session.get("https://example.com/api")

        entry = ApiCallLog.objects.get(service="free_svc")
        self.assertIsNone(entry.cost_estimate)

    def test_failed_response_from_a_priced_service_logs_no_cost(self) -> None:
        """A non-2xx response isn't necessarily billed - don't overstate spend."""
        defaults = {"priced_svc": ServiceDefaults(display_name="Priced", cost_per_call=Decimal("0.05"))}
        session = self._mock_session("priced_svc", ok=False)
        with (
            patch("urbanlens.dashboard.services.rate_limiter.check_rate_limit", return_value=True),
            patch("urbanlens.dashboard.services.rate_limiter.service_is_enabled", return_value=True),
            patch("urbanlens.dashboard.services.rate_limiter.all_service_defaults", return_value=defaults),
        ):
            session.get("https://example.com/api")

        entry = ApiCallLog.objects.get(service="priced_svc")
        self.assertIsNone(entry.cost_estimate)


class SiteAdminApiUsageIncludesPluginsTests(TestCase):
    """SiteAdminStatsApiUsagePartialView used to only iterate SERVICE_REGISTRY,
    silently omitting every plugin-declared service (the great majority of
    this app's integrations)."""

    def setUp(self) -> None:
        super().setUp()
        self.admin = baker.make(User)
        add_user_to_site_admin_group(self.admin)
        self.client.force_login(self.admin)

    def test_plugin_only_service_appears_in_the_report(self) -> None:
        defaults = {"a_plugin_service": ServiceDefaults(display_name="A Plugin Service")}
        with patch("urbanlens.dashboard.services.rate_limiter.all_service_defaults", return_value=defaults):
            response = self.client.get(reverse("site_admin_stats_api"))

        self.assertContains(response, "A Plugin Service")

    def test_priced_service_shows_its_cost(self) -> None:
        defaults = {"priced_svc": ServiceDefaults(display_name="Priced Svc", cost_per_call=Decimal("0.10"))}
        ApiCallLog.objects.create(service="priced_svc", success=True, cost_estimate=Decimal("0.10"))
        with patch("urbanlens.dashboard.services.rate_limiter.all_service_defaults", return_value=defaults):
            response = self.client.get(reverse("site_admin_stats_api"))

        self.assertContains(response, "$0.10")

    def test_unpriced_service_shows_not_priced(self) -> None:
        defaults = {"free_svc": ServiceDefaults(display_name="Free Svc")}
        with patch("urbanlens.dashboard.services.rate_limiter.all_service_defaults", return_value=defaults):
            response = self.client.get(reverse("site_admin_stats_api"))

        self.assertContains(response, "not")
        self.assertContains(response, "priced")


class CostsPageTests(TestCase):
    """The public costs page (no login required)."""

    def test_anonymous_user_can_view_the_page(self) -> None:
        response = self.client.get(reverse("costs"))
        self.assertEqual(response.status_code, 200)

    def test_priced_service_usage_is_reflected_in_the_total(self) -> None:
        defaults = {"priced_svc": ServiceDefaults(display_name="Priced Svc", cost_per_call=Decimal("0.25"))}
        ApiCallLog.objects.create(service="priced_svc", success=True, cost_estimate=Decimal("0.25"))
        ApiCallLog.objects.create(service="priced_svc", success=True, cost_estimate=Decimal("0.25"))

        with patch("urbanlens.dashboard.services.rate_limiter.all_service_defaults", return_value=defaults):
            response = self.client.get(reverse("costs"))

        self.assertEqual(response.context["total_cost_30d"], Decimal("0.50"))
        self.assertContains(response, "Priced Svc")

    def test_unpriced_services_are_counted_but_not_listed(self) -> None:
        defaults = {
            "priced_svc": ServiceDefaults(display_name="Priced Svc", cost_per_call=Decimal("0.25")),
            "free_svc_1": ServiceDefaults(display_name="Free Svc 1"),
            "free_svc_2": ServiceDefaults(display_name="Free Svc 2"),
        }
        ApiCallLog.objects.create(service="priced_svc", success=True, cost_estimate=Decimal("0.25"))

        with patch("urbanlens.dashboard.services.rate_limiter.all_service_defaults", return_value=defaults):
            response = self.client.get(reverse("costs"))

        self.assertEqual(response.context["unpriced_service_count"], 2)
        self.assertNotContains(response, "Free Svc 1")
