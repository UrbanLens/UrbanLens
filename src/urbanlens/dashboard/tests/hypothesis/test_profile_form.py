"""Tests for profile-related form validators and forms.

Pure-function tests use unittest.TestCase (no DB).
ModelForm tests use HypothesisTestCase / django.test.TestCase (DB).
"""
from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

from hypothesis import given, settings
from hypothesis import strategies as st
from model_bakery import baker

from django import forms as django_forms

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.forms.profile_form import (
    DiscordHandleForm,
    ProfileForm,
    validate_birth_date,
    validate_started_exploring,
)
from urbanlens.dashboard.forms.settings_form import MarkupDefaultsForm, PrivacySettingsForm
from urbanlens.dashboard.models.profile.model import VisibilityChoice


_hyp = settings(max_examples=100, deadline=None)
_hyp_db = settings(max_examples=30, deadline=None)

_MIN_AGE_YEARS = 13


def _today() -> date:
    return datetime.now(tz=UTC).date()


def _past_date(years_ago: int) -> date:
    today = _today()
    try:
        return today.replace(year=today.year - years_ago)
    except ValueError:
        return today.replace(year=today.year - years_ago, day=28)


# ── validate_birth_date ───────────────────────────────────────────────────────

class ValidateBirthDateTests(TestCase):
    """validate_birth_date returns None for valid dates and an error string otherwise."""

    def test_none_input_returns_none(self) -> None:
        self.assertIsNone(validate_birth_date(None))

    def test_valid_adult_date_returns_none(self) -> None:
        self.assertIsNone(validate_birth_date(_past_date(20)))

    def test_valid_elderly_date_returns_none(self) -> None:
        self.assertIsNone(validate_birth_date(date(1940, 1, 1)))

    def test_future_date_returns_error(self) -> None:
        future = _today() + timedelta(days=1)
        error = validate_birth_date(future)
        self.assertIsNotNone(error)
        assert error is not None
        self.assertIn("future", error.lower())

    def test_today_returns_age_error(self) -> None:
        error = validate_birth_date(_today())
        self.assertIsNotNone(error)
        assert error is not None
        self.assertIn(str(_MIN_AGE_YEARS), error)

    def test_twelve_years_ago_returns_age_error(self) -> None:
        too_young = _past_date(_MIN_AGE_YEARS - 1)
        error = validate_birth_date(too_young)
        self.assertIsNotNone(error)
        assert error is not None
        self.assertIn(str(_MIN_AGE_YEARS), error)

    def test_exactly_min_age_years_ago_returns_none(self) -> None:
        min_age_date = _past_date(_MIN_AGE_YEARS)
        self.assertIsNone(validate_birth_date(min_age_date))

    def test_far_past_date_returns_none(self) -> None:
        self.assertIsNone(validate_birth_date(date(1900, 6, 15)))

    @given(st.dates(min_value=date(1900, 1, 1), max_value=date(2100, 12, 31)))
    @_hyp
    def test_returns_string_or_none(self, value: date) -> None:
        result = validate_birth_date(value)
        self.assertIn(type(result), (str, type(None)))

    @given(st.dates(min_value=date(1900, 1, 1)))
    @_hyp
    def test_valid_birth_date_in_far_past_returns_none(self, value: date) -> None:
        # Dates > 13 years before today must always be valid.
        cutoff = _past_date(_MIN_AGE_YEARS)
        if value <= cutoff:
            self.assertIsNone(validate_birth_date(value))

    @given(st.dates(min_value=date(2025, 1, 1), max_value=date(2100, 12, 31)))
    @_hyp
    def test_future_dates_always_return_error(self, value: date) -> None:
        today = _today()
        if value > today:
            error = validate_birth_date(value)
            self.assertIsNotNone(error)
            assert error is not None
            self.assertIn("future", error.lower())


# ── validate_started_exploring ────────────────────────────────────────────────

class ValidateStartedExploringTests(TestCase):
    """validate_started_exploring returns None for past/present dates and an error for future."""

    def test_none_input_returns_none(self) -> None:
        self.assertIsNone(validate_started_exploring(None))

    def test_past_date_returns_none(self) -> None:
        self.assertIsNone(validate_started_exploring(date(2010, 1, 1)))

    def test_today_returns_none(self) -> None:
        self.assertIsNone(validate_started_exploring(_today()))

    def test_future_date_returns_error(self) -> None:
        future = _today() + timedelta(days=1)
        error = validate_started_exploring(future)
        self.assertIsNotNone(error)
        assert error is not None
        self.assertIn("future", error.lower())

    def test_far_future_returns_error(self) -> None:
        error = validate_started_exploring(date(2050, 1, 1))
        self.assertIsNotNone(error)

    @given(st.dates(min_value=date(1900, 1, 1)))
    @_hyp
    def test_past_dates_always_return_none(self, value: date) -> None:
        today = _today()
        if value <= today:
            self.assertIsNone(validate_started_exploring(value))

    @given(st.dates(min_value=date(2025, 1, 1), max_value=date(2100, 12, 31)))
    @_hyp
    def test_future_dates_always_return_error(self, value: date) -> None:
        today = _today()
        if value > today:
            result = validate_started_exploring(value)
            self.assertIsNotNone(result)


# ── MarkupDefaultsForm ────────────────────────────────────────────────────────

class MarkupDefaultsFormTests(TestCase):
    """MarkupDefaultsForm clean methods apply defaults and strip whitespace."""

    def _profile(self):
        return baker.make("auth.User").profile

    def _submit(self, **data) -> MarkupDefaultsForm:
        profile = self._profile()
        form = MarkupDefaultsForm(data=data, instance=profile)
        form.is_valid()
        return form

    def test_blank_fill_color_makes_form_invalid(self) -> None:
        # CharField(required=True) rejects empty strings at field-level validation,
        # so clean_markup_fill_color's default fallback is unreachable for truly blank input.
        form = self._submit(markup_fill_color="", markup_fill_opacity=80, markup_border_opacity=50)
        self.assertFalse(form.is_valid())
        self.assertIn("markup_fill_color", form.errors)

    def test_whitespace_fill_color_makes_form_invalid(self) -> None:
        # CharField strips whitespace then rejects the resulting empty string.
        form = self._submit(markup_fill_color="   ", markup_fill_opacity=80, markup_border_opacity=50)
        self.assertFalse(form.is_valid())
        self.assertIn("markup_fill_color", form.errors)

    def test_valid_fill_color_is_returned_stripped(self) -> None:
        form = self._submit(markup_fill_color=" #aabbcc ", markup_fill_opacity=80, markup_border_opacity=50)
        self.assertEqual(form.cleaned_data["markup_fill_color"], "#aabbcc")

    def test_none_border_color_returns_empty_string(self) -> None:
        form = self._submit(markup_fill_color="#ff0000", markup_fill_opacity=50, markup_border_color=None, markup_border_opacity=50)
        self.assertEqual(form.cleaned_data.get("markup_border_color", ""), "")

    def test_whitespace_border_color_is_stripped_to_empty(self) -> None:
        form = self._submit(markup_fill_color="#ff0000", markup_fill_opacity=50, markup_border_color="  ", markup_border_opacity=50)
        self.assertEqual(form.cleaned_data["markup_border_color"], "")

    def test_valid_border_color_is_stripped(self) -> None:
        form = self._submit(markup_fill_color="#ff0000", markup_fill_opacity=50, markup_border_color=" #123456 ", markup_border_opacity=50)
        self.assertEqual(form.cleaned_data["markup_border_color"], "#123456")

    def test_opacity_0_is_valid(self) -> None:
        form = self._submit(markup_fill_color="#ff0000", markup_fill_opacity=0, markup_border_opacity=0)
        self.assertTrue(form.is_valid(), form.errors)

    def test_opacity_100_is_valid(self) -> None:
        form = self._submit(markup_fill_color="#ff0000", markup_fill_opacity=100, markup_border_opacity=100)
        self.assertTrue(form.is_valid(), form.errors)

    def test_opacity_above_100_is_invalid(self) -> None:
        form = self._submit(markup_fill_color="#ff0000", markup_fill_opacity=101, markup_border_opacity=50)
        self.assertFalse(form.is_valid())
        self.assertIn("markup_fill_opacity", form.errors)

    def test_opacity_below_0_is_invalid(self) -> None:
        form = self._submit(markup_fill_color="#ff0000", markup_fill_opacity=-1, markup_border_opacity=50)
        self.assertFalse(form.is_valid())
        self.assertIn("markup_fill_opacity", form.errors)

    @given(opacity=st.integers(min_value=0, max_value=100))
    @_hyp_db
    def test_any_valid_opacity_is_accepted(self, opacity: int) -> None:
        form = self._submit(markup_fill_color="#ff0000", markup_fill_opacity=opacity, markup_border_opacity=opacity)
        self.assertTrue(form.is_valid(), form.errors)


# ── PrivacySettingsForm ───────────────────────────────────────────────────────

class PrivacySettingsFormTests(TestCase):
    """PrivacySettingsForm excludes FRIENDS from friend_request_visibility."""

    def _profile(self):
        return baker.make("auth.User").profile

    def _submit(self, **data) -> PrivacySettingsForm:
        profile = self._profile()
        form = PrivacySettingsForm(data=data, instance=profile)
        form.is_valid()
        return form

    def _default_data(self, **overrides) -> dict:
        return {
            "profile_visibility": VisibilityChoice.ANYONE,
            "comment_visibility": VisibilityChoice.ANYONE,
            "friend_request_visibility": VisibilityChoice.ANYONE,
            "photo_upload_visibility": VisibilityChoice.ANYONE,
            "viewer_photo_filter": VisibilityChoice.ANYONE,
            "trip_pin_location_visibility": VisibilityChoice.ANYONE,
            **overrides,
        }

    def test_friends_is_excluded_from_request_visibility_choices(self) -> None:
        form = PrivacySettingsForm()
        field = form.fields["friend_request_visibility"]
        assert isinstance(field, django_forms.ChoiceField)
        request_choice_values = [k for k, _ in field.choices]
        self.assertNotIn(VisibilityChoice.FRIENDS, request_choice_values)

    def test_friends_is_allowed_in_profile_visibility(self) -> None:
        form = self._submit(**self._default_data(profile_visibility=VisibilityChoice.FRIENDS))
        self.assertNotIn("profile_visibility", form.errors)

    def test_friends_rejected_in_friend_request_visibility(self) -> None:
        form = self._submit(**self._default_data(friend_request_visibility=VisibilityChoice.FRIENDS))
        self.assertFalse(form.is_valid())
        self.assertIn("friend_request_visibility", form.errors)

    def test_anyone_is_valid_for_all_fields(self) -> None:
        form = self._submit(**self._default_data())
        self.assertTrue(form.is_valid(), form.errors)

    def test_no_one_is_valid_for_all_fields(self) -> None:
        form = self._submit(**self._default_data(
            profile_visibility=VisibilityChoice.NO_ONE,
            comment_visibility=VisibilityChoice.NO_ONE,
            friend_request_visibility=VisibilityChoice.NO_ONE,
        ))
        self.assertTrue(form.is_valid(), form.errors)

    def test_trip_pin_visibility_defaults_to_anyone(self) -> None:
        form = self._submit(**self._default_data())
        self.assertTrue(form.is_valid(), form.errors)
        self.assertEqual(form.cleaned_data["trip_pin_location_visibility"], VisibilityChoice.ANYONE)

    @given(choice=st.sampled_from(VisibilityChoice.values))
    @_hyp_db
    def test_trip_pin_visibility_accepts_all_choices(self, choice: str) -> None:
        form = self._submit(**self._default_data(trip_pin_location_visibility=choice))
        self.assertTrue(form.is_valid(), form.errors)
        self.assertEqual(form.cleaned_data["trip_pin_location_visibility"], choice)

    @given(choice=st.sampled_from([c for c in VisibilityChoice.values if c != VisibilityChoice.FRIENDS]))
    @_hyp_db
    def test_non_friends_choices_are_valid_for_request_visibility(self, choice: str) -> None:
        form = self._submit(**self._default_data(friend_request_visibility=choice))
        self.assertNotIn("friend_request_visibility", form.errors)


# ── ProfileForm ───────────────────────────────────────────────────────────────

class ProfileFormTests(TestCase):
    """ProfileForm.clean_birth_date and clean_started_exploring raise ValidationError for bad values."""

    def _profile(self):
        return baker.make("auth.User").profile

    def _submit(self, **data):
        profile = self._profile()
        form = ProfileForm(data=data, instance=profile)
        form.is_valid()
        return form

    # ── clean_birth_date ──────────────────────────────────────────────────────

    def test_blank_birth_date_is_valid(self) -> None:
        form = self._submit(birth_date="")
        self.assertNotIn("birth_date", form.errors)

    def test_valid_adult_birth_date_passes_clean(self) -> None:
        form = self._submit(birth_date="1990-06-15")
        self.assertNotIn("birth_date", form.errors)
        self.assertEqual(form.cleaned_data["birth_date"], date(1990, 6, 15))

    def test_future_birth_date_raises_validation_error(self) -> None:
        future = (_today() + timedelta(days=10)).isoformat()
        form = self._submit(birth_date=future)
        self.assertFalse(form.is_valid())
        self.assertIn("birth_date", form.errors)

    def test_too_young_birth_date_raises_validation_error(self) -> None:
        # 5 years ago - under the 13-year minimum
        too_young = (_today() - timedelta(days=365 * 5)).isoformat()
        form = self._submit(birth_date=too_young)
        self.assertFalse(form.is_valid())
        self.assertIn("birth_date", form.errors)

    def test_exactly_min_age_birth_date_passes_clean(self) -> None:
        today = _today()
        try:
            min_age_date = today.replace(year=today.year - _MIN_AGE_YEARS)
        except ValueError:
            min_age_date = today.replace(year=today.year - _MIN_AGE_YEARS, day=28)
        form = self._submit(birth_date=min_age_date.isoformat())
        self.assertNotIn("birth_date", form.errors)

    # ── clean_started_exploring ───────────────────────────────────────────────

    def test_blank_started_exploring_is_valid(self) -> None:
        form = self._submit(started_exploring="")
        self.assertNotIn("started_exploring", form.errors)

    def test_past_started_exploring_passes_clean(self) -> None:
        form = self._submit(started_exploring="2010-03-01")
        self.assertNotIn("started_exploring", form.errors)
        self.assertEqual(form.cleaned_data["started_exploring"], date(2010, 3, 1))

    def test_today_started_exploring_passes_clean(self) -> None:
        form = self._submit(started_exploring=_today().isoformat())
        self.assertNotIn("started_exploring", form.errors)

    def test_future_started_exploring_raises_validation_error(self) -> None:
        future = (_today() + timedelta(days=1)).isoformat()
        form = self._submit(started_exploring=future)
        self.assertFalse(form.is_valid())
        self.assertIn("started_exploring", form.errors)

    @given(
        birth=st.dates(min_value=date(1900, 1, 1), max_value=date(2013, 1, 1)),
        exploring=st.dates(min_value=date(1900, 1, 1), max_value=date(2026, 6, 20)),
    )
    @_hyp_db
    def test_valid_past_dates_produce_valid_form(self, birth, exploring) -> None:
        today = _today()
        cutoff = _past_date(_MIN_AGE_YEARS)
        # Only submit dates that should actually pass validation.
        if birth > cutoff or exploring > today:
            return
        form = self._submit(birth_date=birth.isoformat(), started_exploring=exploring.isoformat())
        self.assertNotIn("birth_date", form.errors)
        self.assertNotIn("started_exploring", form.errors)


# ── DiscordHandleForm ─────────────────────────────────────────────────────────

class DiscordHandleFormTests(TestCase):
    """DiscordHandleForm accepts an optional Discord username."""

    def test_empty_discord_is_valid(self) -> None:
        form = DiscordHandleForm(data={"discord": ""})
        self.assertTrue(form.is_valid(), form.errors)

    def test_valid_discord_handle_is_accepted(self) -> None:
        form = DiscordHandleForm(data={"discord": "urbanlens_explorer"})
        self.assertTrue(form.is_valid(), form.errors)
        self.assertEqual(form.cleaned_data["discord"], "urbanlens_explorer")

    def test_discord_is_not_required(self) -> None:
        form = DiscordHandleForm(data={})
        self.assertTrue(form.is_valid(), form.errors)

    def test_discord_too_long_is_invalid(self) -> None:
        form = DiscordHandleForm(data={"discord": "x" * 101})
        self.assertFalse(form.is_valid())
        self.assertIn("discord", form.errors)

    def test_discord_max_length_is_valid(self) -> None:
        form = DiscordHandleForm(data={"discord": "x" * 100})
        self.assertTrue(form.is_valid(), form.errors)

    @given(handle=st.text(
        alphabet=st.sampled_from("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789._#-"),
        min_size=2,
        max_size=100,
    ))
    @_hyp
    def test_any_valid_discord_handle_is_accepted(self, handle) -> None:
        form = DiscordHandleForm(data={"discord": handle})
        self.assertTrue(form.is_valid(), form.errors)
