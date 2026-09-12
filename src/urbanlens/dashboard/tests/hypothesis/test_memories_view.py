"""Tests for the Memories page view."""

from __future__ import annotations

from django.contrib.auth.models import User
from django.urls import reverse
from model_bakery import baker

from urbanlens.core.tests.labels import ensure_label
from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.models.images.model import Image
from urbanlens.dashboard.models.location.model import Location
from urbanlens.dashboard.models.pin.model import Pin


class MemoriesViewEmptyStateTests(TestCase):
    """The Memories page should not render data UI when there is no data."""

    def setUp(self) -> None:
        super().setUp()
        self.user: User = baker.make(User)
        self.client.force_login(self.user)

    def test_empty_profile_sees_empty_state_instead_of_memories_ui(self) -> None:
        response = self.client.get(reverse("memories.view"))

        self.assertEqual(response.status_code, 200)
        self.assertFalse(response.context["has_memory_data"])
        self.assertContains(response, "No memory data yet")
        self.assertNotContains(response, "memories-hero-stats")
        self.assertNotContains(response, "memories-controls")
        self.assertNotContains(response, "memories-map")
        self.assertNotContains(response, "memories-timeline")
        self.assertNotContains(response, reverse("memories.visits"))

    def test_profile_with_memory_data_sees_memories_ui(self) -> None:
        baker.make(Image, profile=self.user.profile)

        response = self.client.get(reverse("memories.view"))

        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.context["has_memory_data"])
        self.assertContains(response, "memories-hero-stats")
        self.assertContains(response, "memories-controls")
        self.assertContains(response, "memories-map")
        self.assertContains(response, "memories-timeline")


class MemoriesViewLabelOnlyVisitTests(TestCase):
    """A pin marked "Visited" via the label alone (no PinVisit, no last_visited record) must count as memory data and produce visible content on the Memories page - not a blank page and not the "No memory data yet" empty state, since Pin.objects.visited_without_record() already says there is something here worth showing (a place to log a date for).

    Regression test: has_memory_data was computed strictly from PinVisit/Route/Image/Trip counts, so this exact
    profile shape (nothing but a label-only-visited pin) got has_memory_data=False *and* a non-empty
    unlogged_visits - a combination index.html's old if/inner-if/else never actually rendered anything for (see
    the broken empty-state block)."""

    def setUp(self) -> None:
        super().setUp()
        self.user: User = baker.make(User)
        self.client.force_login(self.user)
        self.location: Location = baker.make("dashboard.Location", latitude=41.7, longitude=-73.9)
        self.pin = baker.make(Pin, profile=self.user.profile, location=self.location, name="Overlook Ruins")
        label = ensure_label(profile=self.user.profile, kind="status", name="Visited")
        self.pin.labels.add(label)

    def test_has_memory_data_is_true(self) -> None:
        response = self.client.get(reverse("memories.view"))
        self.assertTrue(response.context["has_memory_data"])

    def test_the_page_is_not_blank(self) -> None:
        response = self.client.get(reverse("memories.view"))

        self.assertEqual(response.status_code, 200)
        self.assertNotContains(response, "No memory data yet")
        self.assertContains(response, "memories-unlogged")
        self.assertContains(response, "Overlook Ruins")

    def test_hero_stats_are_not_swallowed_by_a_zero_pinvisit_count(self) -> None:
        """Ensures the fix folds label-only visits into has_memory_data rather
        than merely papering over the symptom in the template."""
        from urbanlens.dashboard.controllers.memories import _compute_hero_stats

        _hero_stats, has_memory_data = _compute_hero_stats(self.user.profile)
        self.assertTrue(has_memory_data)


class MemoriesMapDefaultLayerTests(TestCase):
    """The Memories map must start on the same base layer/dark-mode the user has configured for the main map, instead of always falling back to whatever window.MapLayers.create() defaults to when no options are passed - see map/index.html's own defaultBase/darkMode/storageKey wiring, which this page's map init now mirrors exactly."""

    def setUp(self) -> None:
        super().setUp()
        self.user: User = baker.make(User)
        self.client.force_login(self.user)

    def test_default_map_view_context_reflects_profile_setting(self) -> None:
        baker.make(Image, profile=self.user.profile)
        self.user.profile.default_map_view = "topographic"
        self.user.profile.save(update_fields=["default_map_view"])

        response = self.client.get(reverse("memories.view"))

        self.assertEqual(response.context["default_map_view"], "topographic")
        self.assertIn("defaultBase: 'topographic'", response.content.decode())

    def test_map_dark_mode_context_reflects_profile_setting(self) -> None:
        baker.make(Image, profile=self.user.profile)
        self.user.profile.map_dark_mode = "dark"
        self.user.profile.save(update_fields=["map_dark_mode"])

        response = self.client.get(reverse("memories.view"))

        self.assertEqual(response.context["map_dark_mode"], "dark")
        self.assertIn("darkMode: 'dark'", response.content.decode())

    def test_storage_key_matches_the_main_maps_format(self) -> None:
        baker.make(Image, profile=self.user.profile)

        response = self.client.get(reverse("memories.view"))

        self.assertIn(f"ul_layers_v1_{self.user.profile.uuid}", response.content.decode())
