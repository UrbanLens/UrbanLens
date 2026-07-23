"""Tests for the pin detail page's drag-to-resize map height preference.

Covers PinController.set_map_height (save/clamp/validate) and the pin
details page's rendering of the saved height as an inline style.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

from django.contrib.auth.models import User
from django.test import Client
from django.urls import reverse
from model_bakery import baker

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.models.profile.model import Profile

if TYPE_CHECKING:
    from urbanlens.dashboard.models.pin.model import Pin


class SetMapHeightViewTests(TestCase):
    def setUp(self) -> None:
        super().setUp()
        self.user = baker.make(User)
        self.profile = Profile.objects.get(user=self.user)
        self.client = Client()
        self.client.force_login(self.user)

    def _post(self, height):
        return self.client.post(
            reverse("pin.map_height"),
            data=json.dumps({"height": height}),
            content_type="application/json",
        )

    def test_saves_a_valid_height(self) -> None:
        response = self._post(650)
        self.assertEqual(response.status_code, 200)
        self.profile.refresh_from_db()
        self.assertEqual(self.profile.pin_detail_map_height, 650)

    def test_clamps_below_the_minimum(self) -> None:
        response = self._post(50)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["height"], 320)
        self.profile.refresh_from_db()
        self.assertEqual(self.profile.pin_detail_map_height, 320)

    def test_clamps_above_the_maximum(self) -> None:
        response = self._post(9999)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["height"], 1200)
        self.profile.refresh_from_db()
        self.assertEqual(self.profile.pin_detail_map_height, 1200)

    def test_non_numeric_height_returns_400(self) -> None:
        response = self._post("not-a-number")
        self.assertEqual(response.status_code, 400)
        self.profile.refresh_from_db()
        self.assertIsNone(self.profile.pin_detail_map_height)

    def test_missing_height_returns_400(self) -> None:
        response = self.client.post(reverse("pin.map_height"), data=json.dumps({}), content_type="application/json")
        self.assertEqual(response.status_code, 400)

    def test_unauthenticated_request_redirects_to_login(self) -> None:
        client = Client()
        response = client.post(reverse("pin.map_height"), data=json.dumps({"height": 600}), content_type="application/json")
        self.assertIn(response.status_code, (301, 302))


class PinDetailMapHeightRenderTests(TestCase):
    def setUp(self) -> None:
        super().setUp()
        baker.make(User)  # first user is auto-promoted to bootstrap site admin
        self.user = baker.make(User)
        self.profile = Profile.objects.get(user=self.user)
        self.pin: Pin = baker.make_recipe("dashboard.pin", profile=self.profile)
        self.pin.location.route = "Test St"
        self.pin.location.save(update_fields=["route"])
        self.client = Client()
        self.client.force_login(self.user)

    def test_no_saved_height_omits_the_inline_style(self) -> None:
        response = self.client.get(reverse("pin.details", args=[self.pin.slug]))
        content = response.content.decode()
        self.assertIn('id="pin-detail-map-wrapper"', content)
        self.assertNotRegex(content, r'id="pin-detail-map-wrapper"[^>]*style=')

    def test_saved_height_renders_as_an_inline_style(self) -> None:
        self.profile.pin_detail_map_height = 800
        self.profile.save(update_fields=["pin_detail_map_height"])
        response = self.client.get(reverse("pin.details", args=[self.pin.slug]))
        self.assertContains(response, 'style="height: 800px"')

    def test_resize_handle_is_present(self) -> None:
        response = self.client.get(reverse("pin.details", args=[self.pin.slug]))
        self.assertContains(response, 'id="pin-detail-map-resize-handle"')
