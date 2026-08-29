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
from urbanlens.dashboard.services.admin.site_admin import add_user_to_site_admin_group
from urbanlens.dashboard.services.core.rate_limiter import ServiceDefaults, _RateLimitedSession


class ServiceDefaultsCostPerCallTests(SimpleTestCase):
    def test_defaults_to_none(self) -> None:
        self.assertIsNone(ServiceDefaults(display_name="Test").cost_per_call)

    def test_can_be_set(self) -> None:
        defaults = ServiceDefaults(display_name="Test", cost_per_call=Decimal("0.01"))
        self.assertEqual(defaults.cost_per_call, Decimal("0.01"))

    def test_google_geocoding_has_a_configured_cost(self) -> None:
        """Derived from that entry's own $200-credit/~40,000-calls note - a
        regression guard against silently losing the one seeded real value."""
        from urbanlens.dashboard.services.core.rate_limiter import SERVICE_REGISTRY

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
            patch("urbanlens.dashboard.services.core.rate_limiter.check_rate_limit", return_value=True),
            patch("urbanlens.dashboard.services.core.rate_limiter.service_is_enabled", return_value=True),
            patch("urbanlens.dashboard.services.core.rate_limiter.all_service_defaults", return_value=defaults),
        ):
            session.get("https://example.com/api")

        entry = ApiCallLog.objects.get(service="priced_svc")
        self.assertEqual(entry.cost_estimate, Decimal("0.05"))

    def test_successful_call_to_an_unpriced_service_logs_no_cost(self) -> None:
        defaults = {"free_svc": ServiceDefaults(display_name="Free")}
        session = self._mock_session("free_svc", ok=True)
        with (
            patch("urbanlens.dashboard.services.core.rate_limiter.check_rate_limit", return_value=True),
            patch("urbanlens.dashboard.services.core.rate_limiter.service_is_enabled", return_value=True),
            patch("urbanlens.dashboard.services.core.rate_limiter.all_service_defaults", return_value=defaults),
        ):
            session.get("https://example.com/api")

        entry = ApiCallLog.objects.get(service="free_svc")
        self.assertIsNone(entry.cost_estimate)

    def test_failed_response_from_a_priced_service_logs_no_cost(self) -> None:
        """A non-2xx response isn't necessarily billed - don't overstate spend."""
        defaults = {"priced_svc": ServiceDefaults(display_name="Priced", cost_per_call=Decimal("0.05"))}
        session = self._mock_session("priced_svc", ok=False)
        with (
            patch("urbanlens.dashboard.services.core.rate_limiter.check_rate_limit", return_value=True),
            patch("urbanlens.dashboard.services.core.rate_limiter.service_is_enabled", return_value=True),
            patch("urbanlens.dashboard.services.core.rate_limiter.all_service_defaults", return_value=defaults),
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
        with patch("urbanlens.dashboard.services.core.rate_limiter.all_service_defaults", return_value=defaults):
            response = self.client.get(reverse("site_admin_stats_api"))

        self.assertContains(response, "A Plugin Service")

    def test_priced_service_shows_its_cost(self) -> None:
        defaults = {"priced_svc": ServiceDefaults(display_name="Priced Svc", cost_per_call=Decimal("0.10"))}
        ApiCallLog.objects.create(service="priced_svc", success=True, cost_estimate=Decimal("0.10"))
        with patch("urbanlens.dashboard.services.core.rate_limiter.all_service_defaults", return_value=defaults):
            response = self.client.get(reverse("site_admin_stats_api"))

        self.assertContains(response, "$0.10")

    def test_unpriced_service_shows_not_priced(self) -> None:
        defaults = {"free_svc": ServiceDefaults(display_name="Free Svc")}
        with patch("urbanlens.dashboard.services.core.rate_limiter.all_service_defaults", return_value=defaults):
            response = self.client.get(reverse("site_admin_stats_api"))

        self.assertContains(response, "not")
        self.assertContains(response, "priced")

    def test_requires_site_admin_permission_before_and_after_promotion(self) -> None:
        """Gated by ``dashboard.view_site_admin`` - not automatic for any logged-in user."""
        user = baker.make(User)
        self.client.force_login(user)

        response = self.client.get(reverse("site_admin_stats_api"))
        self.assertEqual(response.status_code, 403)

        add_user_to_site_admin_group(user)
        response = self.client.get(reverse("site_admin_stats_api"))
        self.assertEqual(response.status_code, 200)


class CostsPageTests(TestCase):
    """The public costs page.

    It is gated behind ``SiteSettings.public_costs_page_enabled`` (off by
    default) and 404s until an admin turns it on - so these enable it
    explicitly rather than assuming the default. The per-service API spend
    assertions that used to live here moved with the feature: the public page
    now shows only the aggregate monthly total, and the breakdown belongs to
    the site-admin cost page (see ApiSpendSummaryTests below).
    """

    @staticmethod
    def _enable_public_page() -> None:
        from urbanlens.dashboard.models.site_settings import SiteSettings

        site_settings = SiteSettings.get_current()
        site_settings.public_costs_page_enabled = True
        site_settings.save(update_fields=["public_costs_page_enabled"])

    def test_anonymous_user_can_view_the_page_once_enabled(self) -> None:
        self._enable_public_page()
        response = self.client.get(reverse("costs"))
        self.assertEqual(response.status_code, 200)

    def test_the_page_is_absent_until_an_admin_enables_it(self) -> None:
        """Off by default - and 404, not 403, so it doesn't advertise itself."""
        response = self.client.get(reverse("costs"))
        self.assertEqual(response.status_code, 404)


class ApiSpendSummaryTests(TestCase):
    """``api_spend_summary_30d`` - the trailing-30-day external API spend.

    Asserted against the service directly rather than through a page: it is
    shared by the site-admin cost page and anything else reporting spend, and
    these assertions are about the computation, not about rendering. They were
    previously made against the public costs page's context, which no longer
    carries either key.
    """

    def test_priced_service_usage_is_reflected_in_the_total(self) -> None:
        from urbanlens.dashboard.services.admin.cost_tracking import api_spend_summary_30d

        defaults = {"priced_svc": ServiceDefaults(display_name="Priced Svc", cost_per_call=Decimal("0.25"))}
        ApiCallLog.objects.create(service="priced_svc", success=True, cost_estimate=Decimal("0.25"))
        ApiCallLog.objects.create(service="priced_svc", success=True, cost_estimate=Decimal("0.25"))

        with patch("urbanlens.dashboard.services.core.rate_limiter.all_service_defaults", return_value=defaults):
            summary = api_spend_summary_30d()

        self.assertEqual(summary["total_cost_30d"], Decimal("0.50"))
        self.assertIn("Priced Svc", [row["display_name"] for row in summary["priced_services"]])

    def test_unpriced_services_are_counted_but_not_listed(self) -> None:
        from urbanlens.dashboard.services.admin.cost_tracking import api_spend_summary_30d

        defaults = {
            "priced_svc": ServiceDefaults(display_name="Priced Svc", cost_per_call=Decimal("0.25")),
            "free_svc_1": ServiceDefaults(display_name="Free Svc 1"),
            "free_svc_2": ServiceDefaults(display_name="Free Svc 2"),
        }
        ApiCallLog.objects.create(service="priced_svc", success=True, cost_estimate=Decimal("0.25"))

        with patch("urbanlens.dashboard.services.core.rate_limiter.all_service_defaults", return_value=defaults):
            summary = api_spend_summary_30d()

        self.assertEqual(summary["unpriced_service_count"], 2)
        self.assertNotIn("Free Svc 1", [row["display_name"] for row in summary["priced_services"]])

    def test_a_service_with_no_flat_rate_but_recorded_cost_still_counts_as_priced(self) -> None:
        """Per-call AI pricing writes ``cost_estimate`` directly with no ``ServiceDefaults.cost_per_call``
        ever set - the exact regression this function's docstring documents: keying "priced" off the
        flat rate instead of whether cost was actually recorded silently dropped this spend entirely."""
        from urbanlens.dashboard.services.admin.cost_tracking import api_spend_summary_30d

        defaults = {"ai_vision": ServiceDefaults(display_name="AI Vision")}  # no cost_per_call configured
        ApiCallLog.objects.create(service="ai_vision", success=True, cost_estimate=Decimal("1.23"))

        with patch("urbanlens.dashboard.services.core.rate_limiter.all_service_defaults", return_value=defaults):
            summary = api_spend_summary_30d()

        self.assertEqual(summary["total_cost_30d"], Decimal("1.23"))
        self.assertEqual(summary["unpriced_service_count"], 0)
        self.assertIn("AI Vision", [row["display_name"] for row in summary["priced_services"]])
