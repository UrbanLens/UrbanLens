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

    def test_profile_with_memory_data_sees_memories_ui(self) -> None:
        baker.make(Image, profile=self.user.profile)

        response = self.client.get(reverse("memories.view"))

        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.context["has_memory_data"])
        self.assertContains(response, "memories-hero-stats")
        self.assertContains(response, "memories-controls")
        self.assertContains(response, "memories-map")
        self.assertContains(response, "memories-timeline")
