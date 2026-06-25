"""Tests for contact information fields and their visibility control.

Covers:
- Profile contact field defaults
- ContactMethodsForm validation and DB persistence
- PrivacySettingsForm contact_visibility persistence
- Profile.can_view_contact_info() for each VisibilityChoice
"""
from __future__ import annotations

from hypothesis import HealthCheck, given, settings, strategies as st
from model_bakery import baker

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.forms.settings_form import ContactMethodsForm, PrivacySettingsForm
from urbanlens.dashboard.models.friendship.meta import FriendshipStatus, FriendshipType, Permission
from urbanlens.dashboard.models.friendship.model import Friendship
from urbanlens.dashboard.models.location.model import Location
from urbanlens.dashboard.models.profile.model import Profile, VisibilityChoice

_db_settings = settings(
    max_examples=30,
    deadline=None,
    suppress_health_check=[HealthCheck.too_slow, HealthCheck.filter_too_much],
)

_visibility_choices = st.sampled_from(list(VisibilityChoice.values))


def _profile() -> Profile:
    return baker.make("auth.User").profile


def _make_accepted_friendship(a: Profile, b: Profile) -> Friendship:
    return Friendship.objects.create(
        from_profile=a,
        to_profile=b,
        status=FriendshipStatus.ACCEPTED,
        relationship_type=FriendshipType.FRIEND,
        permissions=Permission.VIEW_PROFILE,
    )


# ── Contact field defaults ────────────────────────────────────────────────────


class ContactFieldDefaultsTests(TestCase):
    """Every contact field must default to an empty string; visibility to 'friends'."""

    def setUp(self) -> None:
        super().setUp()
        self.profile = _profile()

    def test_phone_number_defaults_to_empty(self) -> None:
        self.assertEqual(self.profile.phone_number, "")

    def test_signal_username_defaults_to_empty(self) -> None:
        self.assertEqual(self.profile.signal_username, "")

    def test_discord_username_defaults_to_empty(self) -> None:
        self.assertEqual(self.profile.discord_username, "")

    def test_whatsapp_number_defaults_to_empty(self) -> None:
        self.assertEqual(self.profile.whatsapp_number, "")

    def test_telegram_username_defaults_to_empty(self) -> None:
        self.assertEqual(self.profile.telegram_username, "")

    def test_matrix_handle_defaults_to_empty(self) -> None:
        self.assertEqual(self.profile.matrix_handle, "")

    def test_contact_visibility_defaults_to_friends(self) -> None:
        self.assertEqual(self.profile.contact_visibility, VisibilityChoice.FRIENDS)


# ── ContactMethodsForm validation ─────────────────────────────────────────────


class ContactMethodsFormValidationTests(TestCase):
    """All contact fields are optional."""

    def _profile(self) -> Profile:
        return _profile()

    def test_empty_form_is_valid(self) -> None:
        profile = self._profile()
        form = ContactMethodsForm(data={}, instance=profile)
        self.assertTrue(form.is_valid(), form.errors)

    def test_all_fields_filled_is_valid(self) -> None:
        profile = self._profile()
        form = ContactMethodsForm(
            data={
                "phone_number": "+1 555 000 0000",
                "signal_username": "alice",
                "discord_username": "alice#1234",
                "whatsapp_number": "+1 555 000 0001",
                "telegram_username": "@alice",
                "matrix_handle": "@alice:matrix.org",
            },
            instance=profile,
        )
        self.assertTrue(form.is_valid(), form.errors)

    def test_phone_number_too_long_is_invalid(self) -> None:
        profile = self._profile()
        form = ContactMethodsForm(
            data={"phone_number": "1" * 31},
            instance=profile,
        )
        self.assertFalse(form.is_valid())
        self.assertIn("phone_number", form.errors)


class PrivacySettingsFormContactVisibilityTests(TestCase):
    """contact_visibility is saved via PrivacySettingsForm."""

    def _privacy_data(self, **overrides) -> dict:
        return {
            "profile_visibility": VisibilityChoice.ANYONE,
            "comment_visibility": VisibilityChoice.ANYONE,
            "friend_request_visibility": VisibilityChoice.ANYONE,
            "photo_upload_visibility": VisibilityChoice.ANYONE,
            "viewer_photo_filter": VisibilityChoice.ANYONE,
            "trip_pin_location_visibility": VisibilityChoice.ANYONE,
            "contact_visibility": VisibilityChoice.FRIENDS,
            **overrides,
        }

    def test_form_without_visibility_is_invalid(self) -> None:
        profile = _profile()
        data = self._privacy_data()
        del data["contact_visibility"]
        form = PrivacySettingsForm(data=data, instance=profile)
        self.assertFalse(form.is_valid())
        self.assertIn("contact_visibility", form.errors)

    def test_invalid_visibility_choice_rejected(self) -> None:
        profile = _profile()
        form = PrivacySettingsForm(
            data=self._privacy_data(contact_visibility="everyone_on_earth"),
            instance=profile,
        )
        self.assertFalse(form.is_valid())
        self.assertIn("contact_visibility", form.errors)

    @given(_visibility_choices)
    @_db_settings
    def test_every_visibility_choice_is_accepted(self, choice: str) -> None:
        profile = _profile()
        form = PrivacySettingsForm(
            data=self._privacy_data(contact_visibility=choice),
            instance=profile,
        )
        self.assertTrue(form.is_valid(), form.errors)


class ContactMethodsFormSaveTests(TestCase):
    """ContactMethodsForm.save() must persist all fields to the Profile."""

    def test_save_phone_number_persists(self) -> None:
        profile = _profile()
        form = ContactMethodsForm(
            data={"phone_number": "+1 555 123 4567"},
            instance=profile,
        )
        self.assertTrue(form.is_valid(), form.errors)
        form.save()
        profile.refresh_from_db()
        self.assertEqual(profile.phone_number, "+1 555 123 4567")

    def test_save_visibility_persists(self) -> None:
        profile = _profile()
        form = PrivacySettingsForm(
            data={
                "profile_visibility": VisibilityChoice.ANYONE,
                "comment_visibility": VisibilityChoice.ANYONE,
                "friend_request_visibility": VisibilityChoice.ANYONE,
                "photo_upload_visibility": VisibilityChoice.ANYONE,
                "viewer_photo_filter": VisibilityChoice.ANYONE,
                "trip_pin_location_visibility": VisibilityChoice.ANYONE,
                "contact_visibility": VisibilityChoice.NO_ONE,
            },
            instance=profile,
        )
        self.assertTrue(form.is_valid(), form.errors)
        form.save()
        profile.refresh_from_db()
        self.assertEqual(profile.contact_visibility, VisibilityChoice.NO_ONE)

    def test_save_all_fields_persist(self) -> None:
        profile = _profile()
        payload = {
            "phone_number": "+44 20 0000 0000",
            "signal_username": "urbanexplorer",
            "discord_username": "urbex#9999",
            "whatsapp_number": "+44 20 0000 0001",
            "telegram_username": "@urbanexplorer",
            "matrix_handle": "@user:matrix.org",
        }
        form = ContactMethodsForm(data=payload, instance=profile)
        self.assertTrue(form.is_valid(), form.errors)
        form.save()
        profile.refresh_from_db()
        self.assertEqual(profile.signal_username, "urbanexplorer")
        self.assertEqual(profile.discord_username, "urbex#9999")
        self.assertEqual(profile.telegram_username, "@urbanexplorer")
        self.assertEqual(profile.matrix_handle, "@user:matrix.org")


# ── Profile.can_view_contact_info ─────────────────────────────────────────────


class CanViewContactInfoAnyoneTests(TestCase):
    """ANYONE visibility allows all viewers including anonymous."""

    def setUp(self) -> None:
        super().setUp()
        self.profile = _profile()
        Profile.objects.filter(pk=self.profile.pk).update(contact_visibility=VisibilityChoice.ANYONE)
        self.profile.refresh_from_db()

    def test_anonymous_viewer_can_see(self) -> None:
        self.assertTrue(self.profile.can_view_contact_info(None))

    def test_self_can_see(self) -> None:
        self.assertTrue(self.profile.can_view_contact_info(self.profile))

    def test_stranger_can_see(self) -> None:
        stranger = _profile()
        self.assertTrue(self.profile.can_view_contact_info(stranger))


class CanViewContactInfoNoOneTests(TestCase):
    """NO_ONE visibility blocks everyone except the profile owner."""

    def setUp(self) -> None:
        super().setUp()
        self.profile = _profile()
        Profile.objects.filter(pk=self.profile.pk).update(contact_visibility=VisibilityChoice.NO_ONE)
        self.profile.refresh_from_db()

    def test_anonymous_viewer_cannot_see(self) -> None:
        self.assertFalse(self.profile.can_view_contact_info(None))

    def test_stranger_cannot_see(self) -> None:
        stranger = _profile()
        self.assertFalse(self.profile.can_view_contact_info(stranger))

    def test_self_can_always_see_own_contact_info(self) -> None:
        self.assertTrue(self.profile.can_view_contact_info(self.profile))


class CanViewContactInfoFriendsTests(TestCase):
    """FRIENDS visibility: accepted friends can see, strangers and anon cannot."""

    def setUp(self) -> None:
        super().setUp()
        self.profile = _profile()
        Profile.objects.filter(pk=self.profile.pk).update(contact_visibility=VisibilityChoice.FRIENDS)
        self.profile.refresh_from_db()

    def test_anonymous_viewer_cannot_see(self) -> None:
        self.assertFalse(self.profile.can_view_contact_info(None))

    def test_stranger_cannot_see(self) -> None:
        stranger = _profile()
        self.assertFalse(self.profile.can_view_contact_info(stranger))

    def test_accepted_friend_can_see(self) -> None:
        friend = _profile()
        _make_accepted_friendship(self.profile, friend)
        self.assertTrue(self.profile.can_view_contact_info(friend))

    def test_reverse_accepted_friend_can_see(self) -> None:
        friend = _profile()
        _make_accepted_friendship(friend, self.profile)
        self.assertTrue(self.profile.can_view_contact_info(friend))

    def test_requested_but_not_accepted_friendship_cannot_see(self) -> None:
        pending = _profile()
        Friendship.objects.create(
            from_profile=self.profile,
            to_profile=pending,
            status=FriendshipStatus.REQUESTED,
            relationship_type=FriendshipType.FRIEND,
            permissions=Permission.VIEW_PROFILE,
        )
        self.assertFalse(self.profile.can_view_contact_info(pending))


class CanViewContactInfoCommonPinTests(TestCase):
    """COMMON_PIN visibility: viewers sharing a pinned location can see."""

    def setUp(self) -> None:
        super().setUp()
        self.profile = _profile()
        Profile.objects.filter(pk=self.profile.pk).update(contact_visibility=VisibilityChoice.COMMON_PIN)
        self.profile.refresh_from_db()

    def test_viewer_with_no_pins_cannot_see(self) -> None:
        viewer = _profile()
        self.assertFalse(self.profile.can_view_contact_info(viewer))

    def test_viewer_sharing_a_location_can_see(self) -> None:
        shared_location = baker.make(Location, latitude="40.000000", longitude="-74.000000")
        viewer = _profile()
        baker.make("dashboard.Pin", profile=self.profile, location=shared_location, latitude=None, longitude=None)
        baker.make("dashboard.Pin", profile=viewer, location=shared_location, latitude=None, longitude=None)
        self.assertTrue(self.profile.can_view_contact_info(viewer))

    def test_viewer_with_different_pins_cannot_see(self) -> None:
        loc_a = baker.make(Location, latitude="40.000000", longitude="-74.000000")
        loc_b = baker.make(Location, latitude="51.500000", longitude="-0.120000")
        viewer = _profile()
        baker.make("dashboard.Pin", profile=self.profile, location=loc_a, latitude=None, longitude=None)
        baker.make("dashboard.Pin", profile=viewer, location=loc_b, latitude=None, longitude=None)
        self.assertFalse(self.profile.can_view_contact_info(viewer))

    def test_anonymous_cannot_see_regardless_of_pins(self) -> None:
        self.assertFalse(self.profile.can_view_contact_info(None))


class CanViewContactInfoSelfTests(TestCase):
    """Profile owners can always see their own contact info regardless of visibility."""

    @given(_visibility_choices)
    @_db_settings
    def test_self_can_always_view(self, visibility: str) -> None:
        profile = _profile()
        Profile.objects.filter(pk=profile.pk).update(contact_visibility=visibility)
        profile.refresh_from_db()
        self.assertTrue(profile.can_view_contact_info(profile))
