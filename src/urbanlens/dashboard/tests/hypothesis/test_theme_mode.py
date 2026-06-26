"""Tests for the system color theme feature.

Covers:
- ThemeChoice enum values and membership
- Profile.theme_mode default and field persistence
- StyleSettingsForm validation and save behaviour
"""
from __future__ import annotations

from hypothesis import HealthCheck, given, settings, strategies as st
from model_bakery import baker

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.forms.settings_form import StyleSettingsForm
from urbanlens.dashboard.models.profile.model import GuidanceLevel, Profile, ThemeChoice

_db_settings = settings(
    max_examples=30,
    deadline=None,
    suppress_health_check=[HealthCheck.too_slow, HealthCheck.filter_too_much],
)

_theme_choices = st.sampled_from(list(ThemeChoice.values))


def _profile() -> Profile:
    return baker.make("auth.User").profile


# ── ThemeChoice enum ──────────────────────────────────────────────────────────


class ThemeChoiceEnumTests(TestCase):
    """ThemeChoice must contain exactly the three expected values."""

    def test_system_is_a_valid_choice(self) -> None:
        self.assertIn("system", ThemeChoice.values)

    def test_light_is_a_valid_choice(self) -> None:
        self.assertIn("light", ThemeChoice.values)

    def test_dark_is_a_valid_choice(self) -> None:
        self.assertIn("dark", ThemeChoice.values)

    def test_exactly_three_choices(self) -> None:
        self.assertEqual(len(ThemeChoice.values), 3)

    def test_system_label_mentions_os(self) -> None:
        label = dict(ThemeChoice.choices)["system"]
        self.assertIn("OS", label)


# ── Profile.theme_mode field ──────────────────────────────────────────────────


class ProfileThemeModeDefaultTests(TestCase):
    """New profiles must default to the 'system' theme."""

    def test_new_profile_defaults_to_system(self) -> None:
        profile = _profile()
        self.assertEqual(profile.theme_mode, ThemeChoice.SYSTEM)

    def test_theme_mode_field_exists_on_profile(self) -> None:
        profile = _profile()
        self.assertTrue(hasattr(profile, "theme_mode"))

    def test_profile_no_longer_has_dark_mode_attribute(self) -> None:
        profile = _profile()
        self.assertFalse(hasattr(profile, "dark_mode"))


class ProfileThemeModePeristenceTests(TestCase):
    """theme_mode must round-trip through the database for each valid choice."""

    def _set_theme(self, profile: Profile, theme: str) -> Profile:
        Profile.objects.filter(pk=profile.pk).update(theme_mode=theme)
        profile.refresh_from_db()
        return profile

    def test_save_dark_persists(self) -> None:
        profile = self._set_theme(_profile(), ThemeChoice.DARK)
        self.assertEqual(profile.theme_mode, ThemeChoice.DARK)

    def test_save_light_persists(self) -> None:
        profile = self._set_theme(_profile(), ThemeChoice.LIGHT)
        self.assertEqual(profile.theme_mode, ThemeChoice.LIGHT)

    def test_save_system_persists(self) -> None:
        profile = self._set_theme(_profile(), ThemeChoice.SYSTEM)
        self.assertEqual(profile.theme_mode, ThemeChoice.SYSTEM)

    @given(_theme_choices)
    @_db_settings
    def test_any_valid_theme_round_trips(self, theme: str) -> None:
        profile = self._set_theme(_profile(), theme)
        self.assertEqual(profile.theme_mode, theme)


# ── StyleSettingsForm ─────────────────────────────────────────────────────────


class StyleSettingsFormValidationTests(TestCase):
    """StyleSettingsForm must accept every ThemeChoice and reject invalid strings."""

    def _submit(self, theme: str) -> StyleSettingsForm:
        profile = _profile()
        return StyleSettingsForm(
            data={
                "theme_mode": theme,
                "map_dark_mode": ThemeChoice.SYSTEM,
                "guidance_level": GuidanceLevel.ALL,
            },
            instance=profile,
        )

    def test_system_is_valid(self) -> None:
        form = self._submit(ThemeChoice.SYSTEM)
        self.assertTrue(form.is_valid(), form.errors)

    def test_light_is_valid(self) -> None:
        form = self._submit(ThemeChoice.LIGHT)
        self.assertTrue(form.is_valid(), form.errors)

    def test_dark_is_valid(self) -> None:
        form = self._submit(ThemeChoice.DARK)
        self.assertTrue(form.is_valid(), form.errors)

    def test_invalid_choice_is_rejected(self) -> None:
        form = self._submit("rainbow")
        self.assertFalse(form.is_valid())
        self.assertIn("theme_mode", form.errors)

    def test_blank_choice_is_rejected(self) -> None:
        form = self._submit("")
        self.assertFalse(form.is_valid())
        self.assertIn("theme_mode", form.errors)

    @given(_theme_choices)
    @_db_settings
    def test_every_theme_choice_is_valid(self, theme: str) -> None:
        form = self._submit(theme)
        self.assertTrue(form.is_valid(), form.errors)


class StyleSettingsFormSaveTests(TestCase):
    """StyleSettingsForm.save() must persist the theme_mode to the Profile."""

    def _save_theme(self, profile: Profile, theme: str) -> Profile:
        form = StyleSettingsForm(
            data={"theme_mode": theme, "map_dark_mode": ThemeChoice.SYSTEM, "guidance_level": GuidanceLevel.ALL},
            instance=profile,
        )
        self.assertTrue(form.is_valid(), form.errors)
        form.save()
        profile.refresh_from_db()
        return profile

    def test_saving_dark_persists_to_db(self) -> None:
        profile = self._save_theme(_profile(), ThemeChoice.DARK)
        self.assertEqual(profile.theme_mode, ThemeChoice.DARK)

    def test_saving_light_persists_to_db(self) -> None:
        profile = self._save_theme(_profile(), ThemeChoice.LIGHT)
        self.assertEqual(profile.theme_mode, ThemeChoice.LIGHT)

    def test_saving_system_persists_to_db(self) -> None:
        profile = self._save_theme(_profile(), ThemeChoice.SYSTEM)
        self.assertEqual(profile.theme_mode, ThemeChoice.SYSTEM)

    def test_save_overwrites_previous_value(self) -> None:
        profile = _profile()
        Profile.objects.filter(pk=profile.pk).update(theme_mode=ThemeChoice.DARK)
        profile = self._save_theme(profile, ThemeChoice.LIGHT)
        self.assertEqual(profile.theme_mode, ThemeChoice.LIGHT)
