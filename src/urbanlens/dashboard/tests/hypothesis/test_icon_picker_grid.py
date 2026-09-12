"""The icon catalogue is fetched once, not rendered into every picker."""

from __future__ import annotations

from django.contrib.auth.models import Group, User
from django.template.loader import render_to_string
from django.urls import reverse
from model_bakery import baker

from urbanlens.core.tests.render_scaling import RenderTimeScalingMixin
from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.models.achievements.model import Achievement
from urbanlens.dashboard.models.labels.meta import ICON_CATEGORIES
from urbanlens.dashboard.services.admin.site_admin import SITE_ADMIN_GROUP_NAME
from urbanlens.dashboard.services.core.icon_grid import icon_grid_html, icon_grid_version


def _site_admin() -> User:
    admin = baker.make(User, username="zzicon-admin", is_superuser=True)
    group, _ = Group.objects.get_or_create(name=SITE_ADMIN_GROUP_NAME)
    admin.groups.add(group)
    return admin


class AchievementAdminRowCostTests(RenderTimeScalingMixin, TestCase):
    """One more award must not cost a fraction of the whole page to render."""

    def setUp(self) -> None:
        super().setUp()
        baker.make(User)  # absorbs the bootstrap site-admin promotion
        self.client.force_login(_site_admin())

    def seed_rows(self, count: int) -> None:
        """Create *count* more achievements, each of which renders a picker.

        Args:
            count: How many rows to add.
        """
        existing = Achievement.objects.count()
        for index in range(count):
            baker.make(
                Achievement,
                name=f"Award {existing + index}",
                metric="pins_created",
                threshold=existing + index + 1,
            )

    def test_an_extra_award_does_not_cost_an_icon_grid(self) -> None:
        self.assert_row_cost_bounded(reverse("site_admin_achievements"))


class IconPickerGridEndpointTests(TestCase):
    def setUp(self) -> None:
        super().setUp()
        baker.make(User)  # absorbs the bootstrap site-admin promotion
        self.user = baker.make(User, username="zzicon-user")
        self.url = reverse("ui.icon_picker_grid")

    def test_it_requires_a_login(self) -> None:
        response = self.client.get(self.url)

        self.assertIn(response.status_code, (302, 403))

    def test_it_serves_every_icon_in_the_catalogue(self) -> None:
        """The whole point of the fetch: nothing may be dropped in the move."""
        self.client.force_login(self.user)

        body = self.client.get(self.url).content.decode()

        expected = [icon for _label, pairs in ICON_CATEGORIES.values() for icon, _ in pairs]
        missing = [icon for icon in expected if f'data-icon="{icon}"' not in body]
        self.assertEqual(missing, [], f"{len(missing)} of {len(expected)} icons are absent from the shared grid")

    def test_the_response_carries_no_picker_id(self) -> None:
        """One response serves every picker on the page, so it cannot name one.

        The per-item handler resolves the id from the enclosing dropdown; a hardcoded id here would send every
        pick to whichever picker rendered first."""
        self.client.force_login(self.user)

        body = self.client.get(self.url).content.decode()

        self.assertIn("closest('.icon-picker-dropdown').dataset.picker", body)
        self.assertNotIn("icon-value-", body)
        self.assertNotIn("icon-grid-", body)
        self.assertNotIn("icon-tabs-", body)

    def test_the_response_carries_both_fragments(self) -> None:
        """The tab strip rides along with the catalogue it filters, so a picker
        costs one request rather than two."""
        self.client.force_login(self.user)

        body = self.client.get(self.url).content.decode()

        self.assertIn("data-icon-picker-tabs", body)
        self.assertIn("data-icon-picker-items", body)
        self.assertIn('class="icon-tab"', body)

    def test_a_matching_version_is_cached_immutably(self) -> None:
        self.client.force_login(self.user)

        response = self.client.get(self.url, {"v": icon_grid_version()})

        self.assertIn("immutable", response["Cache-Control"])
        self.assertIn("private", response["Cache-Control"])

    def test_a_stale_version_is_not_cached_immutably(self) -> None:
        """A URL written by an older page must not pin the browser to old icons."""
        self.client.force_login(self.user)

        response = self.client.get(self.url, {"v": "not-the-current-hash"})

        self.assertNotIn("immutable", response["Cache-Control"])

    def test_an_unchanged_etag_gets_a_304(self) -> None:
        self.client.force_login(self.user)

        response = self.client.get(self.url, headers={"if-none-match": f'"{icon_grid_version()}"'})

        self.assertEqual(response.status_code, 304)

    def test_the_version_tracks_the_content(self) -> None:
        """Otherwise the immutable cache above serves the previous catalogue."""
        self.assertEqual(icon_grid_version(), icon_grid_version())

        import hashlib

        self.assertEqual(icon_grid_version(), hashlib.sha256(icon_grid_html().encode("utf-8")).hexdigest()[:16])


class IconPickerPartialTests(TestCase):
    """The partial each page includes must stay small, whatever the catalogue costs."""

    def _render(self) -> str:
        return render_to_string(
            "dashboard/partials/ui/_icon_picker.html",
            {"picker_id": "probe", "field_name": "icon", "current_icon": "", "icon_categories": ICON_CATEGORIES},
        )

    def test_the_partial_does_not_inline_the_catalogue(self) -> None:
        markup = self._render()

        icons = [icon for _label, pairs in ICON_CATEGORIES.values() for icon, _ in pairs]
        inlined = [icon for icon in icons if f'data-icon="{icon}"' in markup]
        self.assertEqual(inlined, [], "the picker partial is rendering catalogue icons again")

    def test_the_partial_is_a_small_fraction_of_the_catalogue(self) -> None:
        """A byte budget rather than a shape assertion: the defect was size, and a future edit can put the size back without restoring the loops.

        A ratio rather than a constant so it keeps meaning something as the catalogue grows."""
        markup = self._render()

        self.assertLess(
            len(markup.encode("utf-8")),
            len(icon_grid_html().encode("utf-8")) // 20,
            "the picker partial has grown back toward the size of the catalogue it stopped inlining",
        )

    def test_the_partial_does_not_inline_the_category_tabs(self) -> None:
        """28 tabs per picker was the second-largest thing this partial rendered.

        "All" stays: it is the tab that starts active, which is per-picker state
        rather than catalogue.
        """
        markup = self._render()

        self.assertEqual(markup.count('class="icon-tab'), 1)
        self.assertIn('id="icon-tabs-probe"', markup)

    def test_the_partial_points_at_the_versioned_endpoint(self) -> None:
        markup = self._render()

        self.assertIn(f'data-grid-url="{reverse("ui.icon_picker_grid")}?v={icon_grid_version()}"', markup)

    def test_the_none_button_survives_in_the_partial(self) -> None:
        """It is per-picker state, not catalogue, and a picker with no grid yet
        still has to offer "no icon"."""
        markup = self._render()

        self.assertIn("icon-picker-none", markup)
