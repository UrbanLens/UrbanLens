"""Tests for the Settings > Shortcuts section: HotkeySettingsForm's JSON
cleaning/validation, POST /settings/ section=hotkeys end to end, and that a
saved override reaches the page as window.UL_HOTKEYS.
"""

from __future__ import annotations

import json

from django.contrib.auth.models import User
from django.urls import reverse
from model_bakery import baker

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.forms.settings_form import HotkeySettingsForm


class HotkeySettingsFormCleaningTests(TestCase):
    """clean_keyboard_shortcuts drops anything it doesn't recognize rather than rejecting the save."""

    def setUp(self) -> None:
        self.profile = baker.make(User).profile

    def _clean(self, raw: str) -> dict:
        form = HotkeySettingsForm({"keyboard_shortcuts": raw}, instance=self.profile)
        self.assertTrue(form.is_valid(), form.errors)
        return form.cleaned_data["keyboard_shortcuts"]

    def test_a_valid_override_survives(self) -> None:
        self.assertEqual(self._clean(json.dumps({"undo": "ctrl+alt+z"})), {"undo": "ctrl+alt+z"})

    def test_multiple_valid_overrides_survive(self) -> None:
        self.assertEqual(
            self._clean(json.dumps({"undo": "ctrl+alt+z", "toggleFullscreen": "g"})),
            {"undo": "ctrl+alt+z", "toggleFullscreen": "g"},
        )

    def test_openassistant_combos_survive(self) -> None:
        # The default binding's own key ("?") and its shifted form - the
        # regex was widened for exactly these two, not arbitrary punctuation.
        self.assertEqual(self._clean(json.dumps({"openAssistant": "?"})), {"openAssistant": "?"})
        self.assertEqual(self._clean(json.dumps({"openAssistant": "shift+?"})), {"openAssistant": "shift+?"})

    def test_an_unknown_action_id_is_dropped(self) -> None:
        self.assertEqual(self._clean(json.dumps({"undo": "ctrl+alt+z", "notARealAction": "x"})), {"undo": "ctrl+alt+z"})

    def test_a_malformed_combo_is_dropped(self) -> None:
        self.assertEqual(self._clean(json.dumps({"undo": "<script>alert(1)</script>"})), {})

    def test_a_non_string_combo_is_dropped(self) -> None:
        self.assertEqual(self._clean(json.dumps({"undo": 123})), {})

    def test_malformed_json_yields_no_overrides(self) -> None:
        self.assertEqual(self._clean("not json"), {})

    def test_a_json_array_yields_no_overrides(self) -> None:
        self.assertEqual(self._clean(json.dumps(["undo", "ctrl+z"])), {})

    def test_empty_input_yields_no_overrides(self) -> None:
        self.assertEqual(self._clean(""), {})


class HotkeySettingsPostTests(TestCase):
    """POST /settings/ section=hotkeys end to end."""

    def setUp(self) -> None:
        self.user = baker.make(User)
        self.profile = self.user.profile
        self.client.force_login(self.user)

    def test_settings_page_renders_the_section(self) -> None:
        response = self.client.get(reverse("settings.view"))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'id="hotkeys-settings-section"')
        self.assertContains(response, "id_keyboard_shortcuts")

    def test_posting_the_section_saves_it(self) -> None:
        response = self.client.post(
            reverse("settings.view"),
            {"section": "hotkeys", "keyboard_shortcuts": json.dumps({"undo": "ctrl+alt+z"})},
        )
        self.assertEqual(response.status_code, 302)
        self.profile.refresh_from_db()
        self.assertEqual(self.profile.keyboard_shortcuts, {"undo": "ctrl+alt+z"})

    def test_a_saved_override_is_injected_as_window_ul_hotkeys(self) -> None:
        self.profile.keyboard_shortcuts = {"undo": "ctrl+alt+z"}
        self.profile.save(update_fields=["keyboard_shortcuts"])

        response = self.client.get(reverse("settings.view"))

        self.assertContains(response, "UL_HOTKEYS")
        self.assertContains(response, "ctrl+alt+z")

    def test_no_override_yet_injects_an_empty_mapping(self) -> None:
        response = self.client.get(reverse("settings.view"))

        self.assertContains(response, "window.UL_HOTKEYS = JSON.parse")
