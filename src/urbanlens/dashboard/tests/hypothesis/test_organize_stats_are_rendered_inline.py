"""The Organize page renders label stats in its first paint, rather than backfilling them.

Deferring the stats bought a cheaper first paint in theory only: ``stats_pending``
swaps each number for a spinner, so the deferred render emitted the same ~414KB
of card markup as the real one and the backfill then rendered all of it a second
time. Measured at 120 labels (X25): the stats cost ~16ms (~11ms of queryset,
~5ms of priming) against ~100ms to render the cards, so the deferral spent a
whole second render plus a round trip to save ~16ms.

Tab deferral is a different mechanism and stays: an inactive tab renders no cards
at all, which is the saving that actually pays for itself.
"""

from __future__ import annotations

from django.contrib.auth.models import User
from django.urls import reverse
from model_bakery import baker

from urbanlens.core.tests.labels import ensure_label
from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.models.labels.meta import KIND_TAG
from urbanlens.dashboard.models.pin.model import Pin
from urbanlens.dashboard.models.profile.model import Profile


class OrganizeStatsRenderInlineTests(TestCase):
    """The active tab's cards carry their real counts on the first paint."""

    def setUp(self) -> None:
        super().setUp()
        baker.make(User)  # first user is auto-promoted to site admin
        self.user = baker.make(User)
        self.profile = Profile.objects.get(user=self.user)
        self.client.force_login(self.user)

        self.label = ensure_label(name="Abandoned Factory", kind=KIND_TAG, profile=self.profile)
        pin = baker.make(Pin, profile=self.profile)
        pin.labels.add(self.label)

    def _page(self) -> str:
        response = self.client.get(reverse("organize.index"), {"tab": "tags"})
        self.assertEqual(response.status_code, 200)
        return response.content.decode()

    def test_the_first_paint_carries_the_real_pin_count(self) -> None:
        content = self._page()
        self.assertIn(self.label.name, content)
        self.assertIn('title="Pins"', content)

    def test_the_first_paint_shows_no_stat_spinners(self) -> None:
        """The other half of the above: a card rendered with real counts must not
        also be rendered with the placeholder it replaced."""
        self.assertNotIn("tag-stat-loading", self._page())

    def test_the_active_tab_does_not_schedule_a_stats_backfill(self) -> None:
        """The whole point: the active tab's rows are already correct, so nothing
        should re-fetch and re-render them."""
        rows_url = reverse("label.rows", kwargs={"label_kind": "tag"})
        self.assertNotIn(f'hx-get="{rows_url}" hx-trigger="revealed"', self._page())

    def test_an_inactive_tab_still_defers_its_rows(self) -> None:
        """The negative half: removing the stats backfill must not remove tab
        deferral, which is a different and still-worthwhile saving."""
        content = self._page()
        categories_url = reverse("label.rows", kwargs={"label_kind": "category"})
        self.assertIn(f'hx-get="{categories_url}" hx-trigger="revealed"', content)
        self.assertIn("organize-section-loading", content)

    def test_the_rows_endpoint_still_returns_the_real_pin_count(self) -> None:
        """The reveal path an inactive tab uses is unchanged."""
        response = self.client.get(reverse("label.rows", kwargs={"label_kind": "tag"}))
        content = response.content.decode()
        self.assertIn(self.label.name, content)
        self.assertIn('title="Pins"', content)
        self.assertNotIn("tag-stat-loading", content)
