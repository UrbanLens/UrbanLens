"""Tests for the logged-in homepage dashboard.

The profile page's private-activity section moved to a new homepage
(``/dashboard/home/``, the authenticated landing page) and was rebuilt as a
customizable widget dashboard: no more "only visible to you" framing, an
empty subnav matching other pages, and per-user widget selection/ordering
persisted via ``Profile.home_widget_layout`` (see services.home.home_widgets).
"""

from __future__ import annotations

import json
import re
import tempfile
from typing import TYPE_CHECKING

from django.contrib.auth.models import User
from django.core.files.base import ContentFile
from django.db import connection
from django.test import override_settings
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from model_bakery import baker

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.models.images.model import Image, MediaKind
from urbanlens.dashboard.models.profile.model import Profile
from urbanlens.dashboard.services.home.home_widgets import HOME_WIDGETS, effective_widget_layout, home_dashboard_context

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


#: The widget renders ``img.image.url``, so the fixture needs a real file - and
#: writing it anywhere but a throwaway root would leave litter in MEDIA_ROOT.
_PHOTO_MEDIA_ROOT = tempfile.mkdtemp(prefix="urbanlens-home-photos-")


@override_settings(MEDIA_ROOT=_PHOTO_MEDIA_ROOT)
class RecentPhotosAccessibleNameTests(TestCase):
    """Every photo tile carries a name a screen reader can announce.

    axe reports a missing or blank one as ``image-alt``/``button-name``, both
    critical, and both have been real here: the button lost its name when the
    thumbnail 404'd (fixed by moving the label onto the button), and the ``alt``
    is only non-empty because of a ``|default:`` that truthiness alone does not
    make safe. A caption of whitespace is truthy, so it wins the default and
    lands in ``alt`` - and axe treats a whitespace-only ``alt`` as absent.

    Rendered through the real view rather than the template in isolation: the
    widget only appears when ``home_recent_photos`` is non-empty, so a scan of a
    freshly provisioned account never reaches this markup at all. That is why
    the accessibility suite could not confirm the fix on its own.
    """

    def setUp(self) -> None:
        self.user = baker.make(User)
        self.profile = self.user.profile
        self.client.force_login(self.user)

    def _photo_tile_images(self, caption: str | None) -> list[str]:
        """Render the homepage with one photo and return its tile ``alt`` values."""
        photo = baker.make(Image, profile=self.profile, media_type=MediaKind.PHOTO, caption=caption)
        photo.image.save("tile.jpg", ContentFile(b"jpeg-bytes"), save=True)
        response = self.client.get(reverse("home.view"))
        self.assertEqual(response.status_code, 200)
        strip = re.search(r'<ul class="home-photo-strip.*?</ul>', response.content.decode(), re.DOTALL)
        self.assertIsNotNone(strip, "the recent-photos widget did not render, so this asserts nothing")
        assert strip is not None
        return re.findall(r'<img[^>]*\salt="([^"]*)"', strip.group(0))

    def test_a_captioned_photo_is_announced_by_its_caption(self) -> None:
        self.assertEqual(self._photo_tile_images("Rooftop at dusk"), ["Rooftop at dusk"])

    def test_an_uncaptioned_photo_still_has_a_name(self) -> None:
        self.assertEqual(self._photo_tile_images(""), ["Photo"])

    def test_a_whitespace_caption_does_not_become_a_blank_name(self) -> None:
        """A caption of spaces is truthy, so ``|default:`` does not replace it."""
        for alt in self._photo_tile_images("   "):
            self.assertTrue(alt.strip(), "alt is whitespace only, which axe reports as image-alt")

    def test_a_photo_row_with_no_file_does_not_take_the_page_down(self) -> None:
        """The widget renders ``img.image.url``, which raises when the field is blank.

        Such rows are a known condition, not a hypothetical - the wiki gallery
        endpoint carries an explicit ``exclude(image="")`` for them. Here the
        result is worse than a missing tile: ``ValueError: The 'image' attribute
        has no file associated with it`` escapes the template and the whole
        homepage 500s.
        """
        baker.make(Image, profile=self.profile, media_type=MediaKind.PHOTO, image="", caption="no file")

        self.assertEqual(self.client.get(reverse("home.view")).status_code, 200)

    def test_display_caption_treats_blank_and_missing_alike(self) -> None:
        """The single place the rule lives, so ~30 template tags do not each need it."""
        for stored, expected in ((None, ""), ("", ""), ("   ", ""), (" Rooftop ", "Rooftop")):
            with self.subTest(stored=stored):
                self.assertEqual(baker.prepare(Image, caption=stored).display_caption, expected)


class EffectiveWidgetLayoutTests(TestCase):
    """services.home.home_widgets.effective_widget_layout()."""

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


class DisabledWidgetsCostNothingTests(TestCase):
    """Most homepage entries are lazy querysets, so a disabled widget is free.

    Two were not. The ten counts behind `home_stats` execute as the context dict
    is built, and `home_recent_comments` is forced by the `sorted()` that merges
    pin and trip comments. A user who turned both widgets off still paid for a
    dozen queries on every homepage load.
    """

    def setUp(self) -> None:
        super().setUp()
        baker.make("auth.User")  # absorbs the bootstrap site-admin promotion
        self.profile = baker.make("auth.User").profile

    def _layout(self, *keys: str) -> None:
        Profile.objects.filter(pk=self.profile.pk).update(home_widget_layout=list(keys))
        self.profile.refresh_from_db()

    def _queries(self) -> list[dict]:
        with CaptureQueriesContext(connection) as captured:
            home_dashboard_context(self.profile)
        return list(captured.captured_queries)

    def test_stats_are_built_when_the_widget_is_on(self) -> None:
        self._layout("stats")

        context = home_dashboard_context(self.profile)

        self.assertEqual(len(context["home_stats"]), 8)

    def test_stats_are_not_built_when_the_widget_is_off(self) -> None:
        self._layout("recent_photos")

        context = home_dashboard_context(self.profile)

        self.assertEqual(context["home_stats"], [])

    def test_turning_the_two_eager_widgets_off_costs_fewer_queries(self) -> None:
        self._layout("stats", "recent_comments")
        with_both = len(self._queries())

        self._layout("recent_photos")
        without = len(self._queries())

        self.assertLess(
            without,
            with_both - 8,
            f"{with_both} queries with both widgets, {without} without - the counts are still running",
        )

    def test_recent_comments_are_built_when_the_widget_is_on(self) -> None:
        self._layout("recent_comments")

        self.assertIn("home_recent_comments", home_dashboard_context(self.profile))

    def test_an_uncustomised_layout_still_builds_everything(self) -> None:
        """No saved layout means every widget is on - the default homepage."""
        self._layout()

        context = home_dashboard_context(self.profile)

        self.assertEqual(len(context["home_stats"]), 8)
