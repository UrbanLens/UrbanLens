"""The public costs page runs whole-table aggregates for anonymous callers.

N21 H48. `/costs/` is a `TemplateView` behind nothing but a SiteSettings flag,
and each GET calls seven cost-tracking helpers. `active_user_count` joins every
user against every pin they own and groups, `monthly_cost_series` reconstructs
twelve months, and `cost_per_user`/`cost_per_supporter` each call
`effective_monthly_cost` again - so one anonymous request runs the expensive
aggregate more than once, and nothing stops a crawler running it continuously.

Every figure on the page is a trailing thirty-day or monthly aggregate, so
caching it costs freshness nobody can perceive. The window is a real setting
rather than a literal: the right number depends on how much the figures move,
which is a judgement to revisit rather than one to bury in a decorator.
"""

from __future__ import annotations

from decimal import Decimal

from django.conf import settings
from django.core.cache import cache
from django.db import connection
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from model_bakery import baker

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.models.costs.model import OperatingCost
from urbanlens.dashboard.models.site_settings import SiteSettings

TTL_SETTING = "PUBLIC_COSTS_PAGE_CACHE_SECONDS"


class _CostsPageCase(TestCase):
    def setUp(self) -> None:
        super().setUp()
        cache.clear()
        site = SiteSettings.get_current()
        site.public_costs_page_enabled = True
        site.save(update_fields=["public_costs_page_enabled"])

        OperatingCost.objects.create(name="Electricity", monthly_cost=Decimal(100))
        for _ in range(3):
            baker.make("auth.User", is_active=True)

    def _get(self):
        return self.client.get(reverse("costs"))


class TheSettingExistsTests(_CostsPageCase):
    """`override_settings` will happily invent a name production never reads."""

    def test_the_cache_window_is_a_real_setting(self) -> None:
        self.assertTrue(hasattr(settings, TTL_SETTING), f"nothing reads {TTL_SETTING}")
        self.assertGreater(getattr(settings, TTL_SETTING), 0)


class TheAggregatesRunOnceTests(_CostsPageCase):
    """A second anonymous caller must not pay for the same figures again."""

    def test_a_repeat_view_does_not_rerun_the_aggregates(self) -> None:
        self.assertEqual(self._get().status_code, 200)

        with CaptureQueriesContext(connection) as second:
            self.assertEqual(self._get().status_code, 200)

        self.assertLessEqual(
            len(second.captured_queries),
            2,
            f"the second view still ran {len(second.captured_queries)} queries; the aggregates are not cached",
        )

    def test_the_first_view_really_does_the_work(self) -> None:
        """The anti-vacuity half: a page that queried nothing would pass the test above."""
        with CaptureQueriesContext(connection) as first:
            self.assertEqual(self._get().status_code, 200)

        self.assertGreater(len(first.captured_queries), 2)


class ThePageStillWorksTests(_CostsPageCase):
    """Caching must not turn the page into a blank or a 404."""

    def test_the_figures_still_render(self) -> None:
        self.assertContains(self._get(), "100.00")

    def test_a_figure_that_changes_is_picked_up_after_the_window(self) -> None:
        """Cached is not frozen: the window is the only thing holding the old number."""
        self.assertContains(self._get(), "100.00")

        OperatingCost.objects.create(name="Water", monthly_cost=Decimal(50))
        cache.clear()

        self.assertContains(self._get(), "150.00")

    def test_the_gate_is_still_enforced_and_not_served_from_cache(self) -> None:
        """A cached page must never outlive the toggle that exposes it."""
        self.assertEqual(self._get().status_code, 200)

        site = SiteSettings.get_current()
        site.public_costs_page_enabled = False
        site.save(update_fields=["public_costs_page_enabled"])

        self.assertEqual(self._get().status_code, 404)
