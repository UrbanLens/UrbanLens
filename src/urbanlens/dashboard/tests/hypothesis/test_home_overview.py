"""Tests for the logged-in homepage dashboard.

The profile page's private-activity section moved to a new homepage
(``/dashboard/home/``, the authenticated landing page) and was rebuilt as a
customizable widget dashboard: no more "only visible to you" framing, an
empty subnav matching other pages, and per-user widget selection/ordering
persisted via ``Profile.home_widget_layout`` (see services.home_widgets).
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

from django.contrib.auth.models import User
from django.urls import reverse
from model_bakery import baker

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.services.home_widgets import HOME_WIDGETS, effective_widget_layout

if TYPE_CHECKING:
    from urbanlens.dashboard.models.pin.model import Pin


class HomeOverviewPageTests(TestCase):
    """The homepage renders a customizable dashboard for the signed-in user."""

    def setUp(self) -> None:
        self.user = baker.make(User)
        self.profile = self.user.profile
        self.client.force_login(self.user)

    def test_no_longer_frames_content_as_private(self) -> None:
        """The old amber "private zone" wrapper/chips are gone entirely -
        this is just the user's own dashboard, not a walled-off secret area."""
        response = self.client.get(reverse("home.view"))
        self.assertEqual(response.status_code, 200)
        self.assertNotContains(response, "My Private Activity")
        self.assertNotContains(response, "Only visible to you")

    def test_renders_the_widgets_grid(self) -> None:
        response = self.client.get(reverse("home.view"))
        self.assertContains(response, "home-widgets-grid")

    def test_has_an_empty_subnav_matching_other_pages(self) -> None:
        response = self.client.get(reverse("home.view"))
        self.assertContains(response, "ul-page-subnav")

    def test_has_a_customize_button_and_dialog(self) -> None:
        response = self.client.get(reverse("home.view"))
        self.assertContains(response, "home-customize-dialog")
        self.assertContains(response, "Customize")

    def test_hero_greets_the_user(self) -> None:
        response = self.client.get(reverse("home.view"))
        self.assertContains(response, "Welcome back")

    def test_anonymous_users_are_redirected_to_login(self) -> None:
        self.client.logout()
        response = self.client.get(reverse("home.view"))
        self.assertEqual(response.status_code, 302)
        self.assertIn("login", response["Location"])

    def test_site_root_redirects_authenticated_users_to_the_homepage(self) -> None:
        response = self.client.get(reverse("home"))
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response["Location"], reverse("home.view"))

    def test_nav_bar_home_link_is_active_on_the_homepage(self) -> None:
        response = self.client.get(reverse("home.view"))
        self.assertContains(response, ">Home</a>")

    def test_recently_created_pins_widget_shows_own_pins(self) -> None:
        pin: Pin = baker.make("dashboard.Pin", profile=self.profile, name="Old Asylum")
        self.assertEqual(pin.name, "Old Asylum")
        response = self.client.get(reverse("home.view"))
        self.assertContains(response, "Recently created pins")
        self.assertContains(response, "Old Asylum")


class EffectiveWidgetLayoutTests(TestCase):
    """services.home_widgets.effective_widget_layout()."""

    def setUp(self) -> None:
        self.profile = baker.make(User).profile

    def test_never_customized_profile_gets_every_widget_enabled(self) -> None:
        layout = effective_widget_layout(self.profile)
        self.assertEqual(len(layout), len(HOME_WIDGETS))
        self.assertTrue(all(entry["enabled"] for entry in layout))
        self.assertEqual([entry["widget"].key for entry in layout], [w.key for w in HOME_WIDGETS])

    def test_saved_order_is_respected_and_disabled_widgets_trail(self) -> None:
        self.profile.home_widget_layout = ["recent_trips", "stats"]
        self.profile.save()

        layout = effective_widget_layout(self.profile)
        enabled = [entry["widget"].key for entry in layout if entry["enabled"]]
        disabled = [entry["widget"].key for entry in layout if not entry["enabled"]]

        self.assertEqual(enabled, ["recent_trips", "stats"])
        self.assertEqual(len(enabled) + len(disabled), len(HOME_WIDGETS))
        self.assertNotIn("recent_trips", disabled)
        self.assertNotIn("stats", disabled)

    def test_unknown_saved_keys_are_dropped(self) -> None:
        self.profile.home_widget_layout = ["stats", "not_a_real_widget"]
        self.profile.save()

        layout = effective_widget_layout(self.profile)
        self.assertEqual([entry["widget"].key for entry in layout if entry["enabled"]], ["stats"])

    def test_duplicate_saved_keys_are_deduplicated(self) -> None:
        self.profile.home_widget_layout = ["stats", "stats", "recent_pins"]
        self.profile.save()

        layout = effective_widget_layout(self.profile)
        enabled = [entry["widget"].key for entry in layout if entry["enabled"]]
        self.assertEqual(enabled, ["stats", "recent_pins"])


class HomeWidgetLayoutSaveViewTests(TestCase):
    """POST /dashboard/home/widgets/ - persists the customize dialog's choice."""

    def setUp(self) -> None:
        self.user = baker.make(User)
        self.profile = self.user.profile
        self.client.force_login(self.user)

    def _post(self, enabled_keys: list[str]):
        return self.client.post(
            reverse("home.widgets.save"),
            data=json.dumps({"enabled_keys": enabled_keys}),
            content_type="application/json",
        )

    def test_saves_a_valid_ordered_subset(self) -> None:
        response = self._post(["upcoming_trips", "stats"])
        self.assertEqual(response.status_code, 200)
        self.profile.refresh_from_db()
        self.assertEqual(self.profile.home_widget_layout, ["upcoming_trips", "stats"])

    def test_unknown_keys_are_dropped_before_saving(self) -> None:
        self._post(["stats", "not_a_real_widget"])
        self.profile.refresh_from_db()
        self.assertEqual(self.profile.home_widget_layout, ["stats"])

    def test_response_reflects_the_saved_keys(self) -> None:
        response = self._post(["recent_pins", "stats", "recent_pins"])
        self.assertEqual(response.json()["enabled_keys"], ["recent_pins", "stats"])

    def test_disabling_a_widget_removes_it_from_the_next_render(self) -> None:
        baker.make("dashboard.Pin", profile=self.profile, name="Old Asylum")
        self._post(["stats"])  # recent_pins omitted -> disabled

        response = self.client.get(reverse("home.view"))
        self.assertNotContains(response, "Old Asylum")

    def test_anonymous_users_cannot_save(self) -> None:
        self.client.logout()
        response = self._post(["stats"])
        self.assertEqual(response.status_code, 302)

    def test_saving_never_leaks_into_another_users_layout(self) -> None:
        other = baker.make(User).profile
        self._post(["stats"])
        other.refresh_from_db()
        self.assertEqual(other.home_widget_layout, [])
