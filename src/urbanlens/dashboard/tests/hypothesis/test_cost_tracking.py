"""Tests for cost tracking: component/operating cost models, service math, and admin/public views."""

from __future__ import annotations

from datetime import timedelta
from decimal import Decimal

from django.urls import reverse
from django.utils import timezone
from model_bakery import baker

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.models.api_call_log import ApiCallLog
from urbanlens.dashboard.models.costs.model import CostComponent, OperatingCost
from urbanlens.dashboard.models.site_settings import SiteSettings
from urbanlens.dashboard.services.admin.cost_tracking import (
    active_user_count,
    average_monthly_expense,
    cost_per_user,
    effective_monthly_cost,
    monthly_cost_series,
    total_recorded_expenses,
)


class CostComponentModelTests(TestCase):
    """Amortization math and retirement semantics."""

    def test_monthly_amortized_cost_divides_evenly(self) -> None:
        component = CostComponent.objects.create(name="Hard Drives", replacement_cost=Decimal("1200.00"), deprecation_years=Decimal(10))
        self.assertEqual(component.monthly_amortized_cost, Decimal("10.00"))

    def test_no_retired_at_means_active(self) -> None:
        component = CostComponent.objects.create(name="Server", replacement_cost=Decimal(500), deprecation_years=Decimal(5))
        self.assertTrue(component.is_active)

    def test_retired_at_means_inactive(self) -> None:
        component = CostComponent.objects.create(
            name="Old Server",
            replacement_cost=Decimal(500),
            deprecation_years=Decimal(5),
            retired_at=timezone.now(),
        )
        self.assertFalse(component.is_active)

    def test_elapsed_months_uses_30_day_approximation(self) -> None:
        component = CostComponent.objects.create(name="NAS", replacement_cost=Decimal(600), deprecation_years=Decimal(5))
        CostComponent.objects.filter(pk=component.pk).update(created=timezone.now() - timedelta(days=60))
        component.refresh_from_db()
        self.assertEqual(component.elapsed_months(timezone.now()), Decimal(2))

    def test_elapsed_months_stops_at_retirement(self) -> None:
        now = timezone.now()
        component = CostComponent.objects.create(
            name="Retired NAS",
            replacement_cost=Decimal(600),
            deprecation_years=Decimal(5),
            retired_at=now - timedelta(days=30),
        )
        CostComponent.objects.filter(pk=component.pk).update(created=now - timedelta(days=90))
        component.refresh_from_db()
        # Retired 30 days ago, created 90 days ago: 60 days / 30 = 2 months, regardless of as_of.
        self.assertEqual(component.elapsed_months(now), Decimal(2))


class OperatingCostModelTests(TestCase):
    """Retirement semantics mirror CostComponent."""

    def test_no_retired_at_means_active(self) -> None:
        cost = OperatingCost.objects.create(name="Electricity", monthly_cost=Decimal(100))
        self.assertTrue(cost.is_active)

    def test_retired_at_means_inactive(self) -> None:
        cost = OperatingCost.objects.create(name="Old Hosting", monthly_cost=Decimal(50), retired_at=timezone.now())
        self.assertFalse(cost.is_active)


class CostTrackingServiceTests(TestCase):
    """The pure calculation functions in services/admin/cost_tracking.py."""

    def test_effective_monthly_cost_sums_active_items_only(self) -> None:
        CostComponent.objects.create(name="Hard Drives", replacement_cost=Decimal(1200), deprecation_years=Decimal(10))  # $10/mo
        CostComponent.objects.create(
            name="Retired Drive",
            replacement_cost=Decimal(1200),
            deprecation_years=Decimal(10),
            retired_at=timezone.now() - timedelta(days=1),
        )
        OperatingCost.objects.create(name="Electricity", monthly_cost=Decimal(100))
        OperatingCost.objects.create(name="Old Hosting", monthly_cost=Decimal(50), retired_at=timezone.now() - timedelta(days=1))

        breakdown = effective_monthly_cost()
        self.assertEqual(breakdown.hardware, Decimal("10.00"))
        self.assertEqual(breakdown.operating, Decimal(100))
        self.assertEqual(breakdown.api, Decimal(0))
        self.assertEqual(breakdown.total, Decimal("110.00"))

    def test_effective_monthly_cost_includes_recent_api_spend(self) -> None:
        as_of = timezone.now()
        entry = baker.make(ApiCallLog, cost_estimate=Decimal("5.00"))
        ApiCallLog.objects.filter(pk=entry.pk).update(created=as_of - timedelta(days=5))
        old_entry = baker.make(ApiCallLog, cost_estimate=Decimal("99.00"))
        ApiCallLog.objects.filter(pk=old_entry.pk).update(created=as_of - timedelta(days=45))

        breakdown = effective_monthly_cost(as_of)
        self.assertEqual(breakdown.api, Decimal("5.00"))

    def test_total_recorded_expenses_counts_replacement_cost_once(self) -> None:
        now = timezone.now()
        component = CostComponent.objects.create(name="Hard Drives", replacement_cost=Decimal(1200), deprecation_years=Decimal(10), retired_at=now)
        CostComponent.objects.filter(pk=component.pk).update(created=now - timedelta(days=365))

        total = total_recorded_expenses(now)
        self.assertEqual(total, Decimal(1200))

    def test_total_recorded_expenses_accumulates_operating_cost_over_time(self) -> None:
        now = timezone.now()
        cost = OperatingCost.objects.create(name="Electricity", monthly_cost=Decimal(100))
        OperatingCost.objects.filter(pk=cost.pk).update(created=now - timedelta(days=90))

        total = total_recorded_expenses(now)
        self.assertEqual(total, Decimal(300))  # 3 months * $100

    def test_average_monthly_expense_is_none_with_no_data(self) -> None:
        self.assertIsNone(average_monthly_expense())

    def test_average_monthly_expense_divides_total_by_elapsed_months(self) -> None:
        now = timezone.now()
        cost = OperatingCost.objects.create(name="Electricity", monthly_cost=Decimal(100))
        OperatingCost.objects.filter(pk=cost.pk).update(created=now - timedelta(days=60))

        # $200 total (2 months) / 2 elapsed months = $100/mo
        self.assertEqual(average_monthly_expense(now), Decimal(100))

    def test_active_user_count_excludes_inactive_users(self) -> None:
        baker.make("auth.User", is_active=True, _quantity=2)
        baker.make("auth.User", is_active=False)
        self.assertEqual(active_user_count(), 2)

    def test_cost_per_user_is_none_with_no_users(self) -> None:
        self.assertIsNone(cost_per_user())

    def test_cost_per_user_divides_effective_cost_by_active_users(self) -> None:
        baker.make("auth.User", is_active=True, _quantity=2)
        OperatingCost.objects.create(name="Electricity", monthly_cost=Decimal(100))

        self.assertEqual(cost_per_user(), Decimal(50))

    def test_monthly_cost_series_shape(self) -> None:
        series = monthly_cost_series(months=3)
        self.assertEqual(len(series["labels"]), 3)
        self.assertEqual(len(series["hardware"]), 3)
        self.assertEqual(len(series["operating"]), 3)
        self.assertEqual(len(series["api"]), 3)

    def test_monthly_cost_series_reflects_retirement_boundary(self) -> None:
        now = timezone.now()
        cost = OperatingCost.objects.create(name="Electricity", monthly_cost=Decimal(100))
        OperatingCost.objects.filter(pk=cost.pk).update(created=now - timedelta(days=400))

        series = monthly_cost_series(months=4, as_of=now)
        # Created well before the window and never retired: active throughout.
        self.assertTrue(all(value == 100.0 for value in series["operating"]))

        cost.refresh_from_db()
        cost.retired_at = now - timedelta(days=200)  # retired well before the 4-month (~120 day) window starts
        cost.save(update_fields=["retired_at"])

        series_after_retirement = monthly_cost_series(months=4, as_of=now)
        # Retired long before the window opened: inactive throughout.
        self.assertTrue(all(value == 0.0 for value in series_after_retirement["operating"]))


class SiteAdminCostsViewTests(TestCase):
    """Only site admins may manage cost tracking."""

    def setUp(self) -> None:
        baker.make("auth.User")  # first user is auto-promoted to bootstrap site admin
        self.user = baker.make("auth.User")
        self.admin_user = baker.make("auth.User", is_superuser=True, is_staff=True)

    def test_anonymous_is_redirected(self) -> None:
        response = self.client.get(reverse("site_admin_costs"))
        self.assertEqual(response.status_code, 302)

    def test_non_admin_is_refused(self) -> None:
        self.client.force_login(self.user)
        response = self.client.get(reverse("site_admin_costs"))
        self.assertIn(response.status_code, (302, 403))

    def test_admin_page_renders(self) -> None:
        self.client.force_login(self.admin_user)
        CostComponent.objects.create(name="Hard Drives", replacement_cost=Decimal(1200), deprecation_years=Decimal(10))

        response = self.client.get(reverse("site_admin_costs"))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Hard Drives")

    def test_admin_can_create_a_component(self) -> None:
        self.client.force_login(self.admin_user)
        response = self.client.post(
            reverse("site_admin_cost_component_create"),
            {"name": "Hard Drives", "replacement_cost": "1000", "deprecation_years": "10", "order": "0", "is_active": "on"},
        )
        self.assertEqual(response.status_code, 200)
        component = CostComponent.objects.get(name="Hard Drives")
        self.assertEqual(component.replacement_cost, Decimal("1000.00"))
        self.assertEqual(component.deprecation_years, Decimal("10.0"))

    def test_invalid_component_cost_is_reported_not_saved(self) -> None:
        self.client.force_login(self.admin_user)
        response = self.client.post(
            reverse("site_admin_cost_component_create"),
            {"name": "Bad", "replacement_cost": "not-a-number", "deprecation_years": "10"},
        )
        self.assertEqual(response.status_code, 400)
        self.assertFalse(CostComponent.objects.filter(name="Bad").exists())

    def test_admin_can_edit_and_retire_a_component(self) -> None:
        self.client.force_login(self.admin_user)
        component = CostComponent.objects.create(name="Original", replacement_cost=Decimal(1000), deprecation_years=Decimal(10))

        self.client.post(
            reverse("site_admin_cost_component_edit", kwargs={"component_id": component.pk}),
            {"name": "Renamed", "replacement_cost": "2000", "deprecation_years": "5", "order": "1"},
        )
        component.refresh_from_db()
        self.assertEqual(component.name, "Renamed")
        self.assertFalse(component.is_active)  # is_active omitted from POST data = unchecked

    def test_admin_can_delete_a_component(self) -> None:
        self.client.force_login(self.admin_user)
        component = CostComponent.objects.create(name="To Delete", replacement_cost=Decimal(1000), deprecation_years=Decimal(10))

        self.client.delete(reverse("site_admin_cost_component_edit", kwargs={"component_id": component.pk}))
        self.assertFalse(CostComponent.objects.filter(pk=component.pk).exists())

    def test_admin_can_create_an_operating_cost(self) -> None:
        self.client.force_login(self.admin_user)
        response = self.client.post(
            reverse("site_admin_operating_cost_create"),
            {"name": "Electricity", "monthly_cost": "100", "order": "0", "is_active": "on"},
        )
        self.assertEqual(response.status_code, 200)
        self.assertTrue(OperatingCost.objects.filter(name="Electricity", monthly_cost=Decimal("100.00")).exists())

    def test_admin_can_edit_and_delete_an_operating_cost(self) -> None:
        self.client.force_login(self.admin_user)
        cost = OperatingCost.objects.create(name="Original", monthly_cost=Decimal(50))

        self.client.post(
            reverse("site_admin_operating_cost_edit", kwargs={"cost_id": cost.pk}),
            {"name": "Renamed", "monthly_cost": "75", "order": "0", "is_active": "on"},
        )
        cost.refresh_from_db()
        self.assertEqual(cost.name, "Renamed")
        self.assertEqual(cost.monthly_cost, Decimal("75.00"))

        self.client.delete(reverse("site_admin_operating_cost_edit", kwargs={"cost_id": cost.pk}))
        self.assertFalse(OperatingCost.objects.filter(pk=cost.pk).exists())

    def test_admin_can_toggle_public_page(self) -> None:
        self.client.force_login(self.admin_user)
        self.assertFalse(SiteSettings.get_current().public_costs_page_enabled)

        self.client.post(reverse("site_admin_costs_toggle_public"))
        self.assertTrue(SiteSettings.get_current().public_costs_page_enabled)

        self.client.post(reverse("site_admin_costs_toggle_public"))
        self.assertFalse(SiteSettings.get_current().public_costs_page_enabled)


class PublicCostsViewTests(TestCase):
    """The public /costs/ page is gated behind the admin toggle."""

    def test_page_404s_when_disabled(self) -> None:
        response = self.client.get(reverse("costs"))
        self.assertEqual(response.status_code, 404)

    def test_page_renders_combined_totals_when_enabled(self) -> None:
        settings_obj = SiteSettings.get_current()
        settings_obj.public_costs_page_enabled = True
        settings_obj.save(update_fields=["public_costs_page_enabled"])

        baker.make("auth.User", is_active=True)
        OperatingCost.objects.create(name="Electricity", monthly_cost=Decimal(100))

        response = self.client.get(reverse("costs"))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "100.00")

    def test_footer_link_hidden_when_disabled(self) -> None:
        response = self.client.get(reverse("about"))
        self.assertNotContains(response, "Running Costs")

    def test_footer_link_shown_when_enabled(self) -> None:
        settings_obj = SiteSettings.get_current()
        settings_obj.public_costs_page_enabled = True
        settings_obj.save(update_fields=["public_costs_page_enabled"])

        response = self.client.get(reverse("about"))
        self.assertContains(response, "Running Costs")
