"""Tests for the consent-style interaction-preference fields on Profile.

Covers:
- Profile.preference_display / Profile.interaction_preferences (model logic:
  unset vs. answered, and the "other" free-text fallback).
- ProfileForm accepts/rejects preference choices and enforces the "other"/
  additional_preferences length caps.
- ProfileFieldUpdateView's autosave path for each preference field and its
  "_other" companion.
- Profile page rendering: the section is omitted entirely when nothing has
  been answered, and shown (with the right text) when it has.
"""

from __future__ import annotations

from django.contrib.auth.models import User
from django.urls import reverse
from model_bakery import baker

from hypothesis import given, settings, strategies as st
from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.forms.profile_form import ProfileForm
from urbanlens.dashboard.models.profile.meta import (
    ExploringWithOthersPreference,
    FriendRequestPreference,
    MeetupPreference,
    PhotoSharingPreference,
    PhotoTaggingPreference,
    PhotoTakingPreference,
    PhotoUsagePreference,
)
from urbanlens.dashboard.services.core.text_limits import MAX_ADDITIONAL_PREFERENCES_LENGTH, MAX_PREFERENCE_OTHER_LENGTH

_hyp_db = settings(max_examples=15, deadline=None)

#: Each preference field paired with its TextChoices class, mirroring
#: ProfileFieldUpdateView._PROFILE_PREFERENCE_CHOICES - shared by several
#: tests below that exercise every field the same way.
_PREFERENCE_CHOICE_CLASSES = {
    "photo_taking_preference": PhotoTakingPreference,
    "photo_sharing_preference": PhotoSharingPreference,
    "photo_tagging_preference": PhotoTaggingPreference,
    "photo_usage_preference": PhotoUsagePreference,
    "friend_request_preference": FriendRequestPreference,
    "meetup_preference": MeetupPreference,
    "exploring_with_others_preference": ExploringWithOthersPreference,
}


# -- Profile.preference_display / interaction_preferences ----------------------


class PreferenceDisplayTests(TestCase):
    """Profile.preference_display and the interaction_preferences property."""

    def setUp(self) -> None:
        self.user = baker.make(User)
        self.profile = self.user.profile

    def test_unset_preference_returns_empty_string(self) -> None:
        self.assertEqual(self.profile.preference_display("photo_taking_preference"), "")

    def test_set_preference_returns_display_label(self) -> None:
        self.profile.photo_taking_preference = PhotoTakingPreference.ASK_FIRST
        self.assertEqual(self.profile.preference_display("photo_taking_preference"), "Please ask first")

    def test_other_with_text_returns_the_custom_text(self) -> None:
        self.profile.meetup_preference = MeetupPreference.OTHER
        self.profile.meetup_preference_other = "Only at organized group meetups."
        self.assertEqual(self.profile.preference_display("meetup_preference"), "Only at organized group meetups.")

    def test_other_without_text_falls_back_to_label(self) -> None:
        self.profile.meetup_preference = MeetupPreference.OTHER
        self.assertEqual(self.profile.preference_display("meetup_preference"), "Other (please specify below)")

    def test_interaction_preferences_empty_when_nothing_answered(self) -> None:
        self.assertEqual(self.profile.interaction_preferences, [])

    def test_interaction_preferences_omits_unanswered_fields(self) -> None:
        self.profile.photo_taking_preference = PhotoTakingPreference.YES
        prefs = self.profile.interaction_preferences
        self.assertEqual(len(prefs), 1)
        self.assertEqual(prefs[0]["field"], "photo_taking_preference")
        self.assertEqual(prefs[0]["label"], "Taking Photos of Me")
        self.assertEqual(prefs[0]["value"], "Yes, please.")

    def test_interaction_preferences_reflects_other_text(self) -> None:
        self.profile.friend_request_preference = FriendRequestPreference.OTHER
        self.profile.friend_request_preference_other = "Ask me on Discord first."
        prefs = self.profile.interaction_preferences
        self.assertEqual(prefs[0]["value"], "Ask me on Discord first.")

    @given(st.sampled_from(list(_PREFERENCE_CHOICE_CLASSES.items())))
    @_hyp_db
    def test_every_non_other_choice_displays_its_label(self, field_and_cls) -> None:
        field, choices_cls = field_and_cls
        for member in choices_cls:
            if member == choices_cls.OTHER:
                continue
            setattr(self.profile, field, member)
            self.assertEqual(self.profile.preference_display(field), member.label)


# -- ProfileForm -----------------------------------------------------------


class ProfileFormPreferenceTests(TestCase):
    """ProfileForm validates the preference choice fields and their length caps."""

    def _profile(self):
        return baker.make("auth.User").profile

    def _submit(self, **data):
        profile = self._profile()
        form = ProfileForm(data=data, instance=profile)
        form.is_valid()
        return form

    def test_all_preferences_blank_is_valid(self) -> None:
        form = self._submit()
        self.assertNotIn("photo_taking_preference", form.errors)
        self.assertNotIn("meetup_preference", form.errors)

    def test_valid_choice_is_accepted(self) -> None:
        form = self._submit(photo_taking_preference=PhotoTakingPreference.ASK_FIRST)
        self.assertNotIn("photo_taking_preference", form.errors)

    def test_invalid_choice_is_rejected(self) -> None:
        form = self._submit(photo_taking_preference="not-a-real-choice")
        self.assertIn("photo_taking_preference", form.errors)

    def test_other_text_is_accepted_alongside_other_choice(self) -> None:
        form = self._submit(
            meetup_preference=MeetupPreference.OTHER,
            meetup_preference_other="Only small group meetups.",
        )
        self.assertNotIn("meetup_preference_other", form.errors)
        self.assertEqual(form.cleaned_data["meetup_preference_other"], "Only small group meetups.")

    def test_other_text_over_max_length_is_rejected(self) -> None:
        form = self._submit(meetup_preference_other="x" * (MAX_PREFERENCE_OTHER_LENGTH + 1))
        self.assertIn("meetup_preference_other", form.errors)

    def test_other_text_at_max_length_is_valid(self) -> None:
        form = self._submit(meetup_preference_other="x" * MAX_PREFERENCE_OTHER_LENGTH)
        self.assertNotIn("meetup_preference_other", form.errors)

    def test_additional_preferences_over_max_length_is_rejected(self) -> None:
        form = self._submit(additional_preferences="x" * (MAX_ADDITIONAL_PREFERENCES_LENGTH + 1))
        self.assertIn("additional_preferences", form.errors)

    def test_additional_preferences_at_max_length_is_valid(self) -> None:
        form = self._submit(additional_preferences="x" * MAX_ADDITIONAL_PREFERENCES_LENGTH)
        self.assertNotIn("additional_preferences", form.errors)

    @given(st.sampled_from(list(_PREFERENCE_CHOICE_CLASSES.items())))
    @_hyp_db
    def test_every_defined_choice_is_accepted_by_its_field(self, field_and_cls) -> None:
        field, choices_cls = field_and_cls
        for member in choices_cls:
            form = self._submit(**{field: member})
            self.assertNotIn(field, form.errors)


# -- ProfileFieldUpdateView autosave -----------------------------------------


class ProfileFieldUpdatePreferenceTests(TestCase):
    """ProfileFieldUpdateView's autosave path for preference fields."""

    def setUp(self) -> None:
        self.user = baker.make(User)
        self.profile = self.user.profile
        self.client.force_login(self.user)

    def _post(self, field: str, value: str):
        return self.client.post(reverse("profile.field.update"), {"field": field, "value": value})

    @given(st.sampled_from(list(_PREFERENCE_CHOICE_CLASSES.items())))
    @_hyp_db
    def test_saves_a_valid_choice(self, field_and_cls) -> None:
        field, choices_cls = field_and_cls
        member = next(iter(choices_cls))
        response = self._post(field, member.value)
        self.assertEqual(response.status_code, 200)
        self.profile.refresh_from_db()
        self.assertEqual(getattr(self.profile, field), member.value)

    def test_rejects_an_invalid_choice(self) -> None:
        response = self._post("photo_taking_preference", "definitely-not-valid")
        self.assertEqual(response.status_code, 400)
        self.profile.refresh_from_db()
        self.assertEqual(self.profile.photo_taking_preference, "")

    def test_clearing_a_preference_is_allowed(self) -> None:
        self.profile.photo_sharing_preference = PhotoSharingPreference.BLUR_FACE
        self.profile.save(update_fields=["photo_sharing_preference"])
        response = self._post("photo_sharing_preference", "")
        self.assertEqual(response.status_code, 200)
        self.profile.refresh_from_db()
        self.assertEqual(self.profile.photo_sharing_preference, "")

    def test_saves_the_other_companion_free_text(self) -> None:
        response = self._post("exploring_with_others_preference_other", "Depends on the location.")
        self.assertEqual(response.status_code, 200)
        self.profile.refresh_from_db()
        self.assertEqual(self.profile.exploring_with_others_preference_other, "Depends on the location.")

    def test_saves_additional_preferences(self) -> None:
        response = self._post("additional_preferences", "I bring my own gear, please don't touch it.")
        self.assertEqual(response.status_code, 200)
        self.profile.refresh_from_db()
        self.assertEqual(self.profile.additional_preferences, "I bring my own gear, please don't touch it.")

    def test_requires_login(self) -> None:
        self.client.logout()
        response = self._post("photo_taking_preference", PhotoTakingPreference.YES)
        self.assertEqual(response.status_code, 302)


# -- Profile page rendering ---------------------------------------------------


class ProfilePreferencesRenderingTests(TestCase):
    """The profile page's Interaction Preferences section: shown only when answered."""

    def setUp(self) -> None:
        self.user = baker.make(User)
        self.profile = self.user.profile
        self.client.force_login(self.user)

    def test_section_omitted_when_nothing_answered(self) -> None:
        response = self.client.get(reverse("profile.view"))
        self.assertNotContains(response, "Interaction Preferences")

    def test_section_shown_with_one_preference_answered(self) -> None:
        self.profile.photo_taking_preference = PhotoTakingPreference.YES
        self.profile.save(update_fields=["photo_taking_preference"])
        response = self.client.get(reverse("profile.view"))
        self.assertContains(response, "Interaction Preferences")
        self.assertContains(response, "Taking Photos of Me")
        self.assertContains(response, "Yes, please.")

    def test_section_shown_with_only_additional_preferences_set(self) -> None:
        self.profile.additional_preferences = "No flash photography, please."
        self.profile.save(update_fields=["additional_preferences"])
        response = self.client.get(reverse("profile.view"))
        self.assertContains(response, "Interaction Preferences")
        self.assertContains(response, "No flash photography, please.")

    def test_other_choice_shows_custom_text_not_the_generic_label(self) -> None:
        self.profile.meetup_preference = MeetupPreference.OTHER
        self.profile.meetup_preference_other = "Only at conventions."
        self.profile.save(update_fields=["meetup_preference", "meetup_preference_other"])
        response = self.client.get(reverse("profile.view"))
        self.assertContains(response, "Only at conventions.")
        self.assertNotContains(response, "Other (please specify below)")

    def test_edit_link_shown_on_own_profile(self) -> None:
        self.profile.photo_taking_preference = PhotoTakingPreference.YES
        self.profile.save(update_fields=["photo_taking_preference"])
        response = self.client.get(reverse("profile.view"))
        self.assertContains(response, "Edit your preferences")
