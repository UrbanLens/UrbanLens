"""Tests for the Memories page view."""
from __future__ import annotations

from django.contrib.auth.models import User
from django.urls import reverse
from model_bakery import baker

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.models.images.model import Image


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


class MemoriesMapDefaultLayerTests(TestCase):
    """The Memories map must start on the same base layer/dark-mode the user has
    configured for the main map, instead of always falling back to whatever
    window.MapLayers.create() defaults to when no options are passed - see
    map/index.html's own defaultBase/darkMode/storageKey wiring, which this
    page's map init now mirrors exactly."""

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
