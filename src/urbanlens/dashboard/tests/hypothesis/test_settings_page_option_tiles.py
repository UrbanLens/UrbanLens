"""Smoke test for the settings page's generalized option-tile picker style.

Regression coverage for generalizing .settings-theme-option (previously only
used by the Color Theme / Map Dark Mode pickers) into .settings-option-tile,
now shared by every group of this shape on the page (Color Theme, Map Dark
Mode, Guidance level, Default Map View, Starting Point, Cluster Radius mode) -
retiring the old .settings-theme-option/.settings-theme-row and
.settings-map-view-option/.settings-map-view-row class names entirely.
"""

from __future__ import annotations

from django.contrib.auth.models import User
from django.urls import reverse
from model_bakery import baker

from urbanlens.core.tests.testcase import TestCase


class SettingsPageOptionTileTests(TestCase):
    def setUp(self) -> None:
        self.user = baker.make(User)
        self.client.force_login(self.user)

    def test_page_renders_successfully(self) -> None:
        response = self.client.get(reverse("settings.view"))
        self.assertEqual(response.status_code, 200)

    def test_uses_the_generalized_option_tile_class(self) -> None:
        response = self.client.get(reverse("settings.view"))
        self.assertContains(response, "settings-option-tile")
        self.assertContains(response, "settings-option-row")

    def test_old_class_names_are_gone(self) -> None:
        response = self.client.get(reverse("settings.view"))
        self.assertNotContains(response, "settings-theme-option")
        self.assertNotContains(response, "settings-theme-row")
        self.assertNotContains(response, "settings-map-view-option")
        self.assertNotContains(response, "settings-map-view-row")
