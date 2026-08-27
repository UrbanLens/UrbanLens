"""The Organize page defers pin-count stats to a follow-up HTMX request.

Computing pin_count/location_count (a correlated subquery per label) and, for
any label with children, `tag_total_pins`'s full descendant BFS was slow
enough on a large label set to noticeably delay the Organize page's first
paint. The initial page load now renders label cards via
`LabelQuerySet.with_hierarchy()` (no stat annotations) with a loading
placeholder in place of the numbers, and each tab's rows re-fetch themselves
through the existing `label.rows` endpoint (which still uses
`with_pin_counts()`) once shown - see `organize_label_panel.html`'s
`hx-trigger="revealed"`.
"""

from __future__ import annotations

from django.contrib.auth.models import User
from django.urls import reverse
from model_bakery import baker

from urbanlens.core.tests.labels import ensure_label
from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.models.labels.model import KIND_TAG, Label
from urbanlens.dashboard.models.pin.model import Pin
from urbanlens.dashboard.models.profile.model import Profile


class OrganizeStatsDeferralTests(TestCase):
    """The initial Organize page paint omits pin counts; label.rows backfills them."""

    def setUp(self) -> None:
        super().setUp()
        baker.make(User)  # first user is auto-promoted to site admin
        self.user = baker.make(User)
        self.profile = Profile.objects.get(user=self.user)
        self.client.force_login(self.user)

        self.label = ensure_label(name="Abandoned Factory", kind=KIND_TAG, profile=self.profile)
        pin = baker.make(Pin, profile=self.profile)
        pin.labels.add(self.label)

    def test_initial_page_load_omits_the_real_pin_count(self) -> None:
        response = self.client.get(reverse("organize.index"), {"tab": "tags"})
        self.assertEqual(response.status_code, 200)
        content = response.content.decode()
        self.assertIn(self.label.name, content)
        self.assertIn("tag-stat-loading", content)
        # The real count (1 pin) must not leak into the deferred render.
        self.assertNotIn('title="Pins"', content)

    def test_initial_page_load_wires_up_the_htmx_backfill(self) -> None:
        response = self.client.get(reverse("organize.index"), {"tab": "tags"})
        content = response.content.decode()
        rows_url = reverse("label.rows", kwargs={"label_kind": "tag"})
        self.assertIn(f'hx-get="{rows_url}"', content)
        self.assertIn('hx-trigger="revealed"', content)
        self.assertIn('hx-target="#tag-rows"', content)

    def test_label_rows_endpoint_returns_the_real_pin_count(self) -> None:
        response = self.client.get(reverse("label.rows", kwargs={"label_kind": "tag"}))
        content = response.content.decode()
        self.assertIn(self.label.name, content)
        self.assertIn('title="Pins"', content)
        self.assertNotIn("tag-stat-loading", content)
