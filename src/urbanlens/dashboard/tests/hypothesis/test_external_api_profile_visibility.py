"""External API: profile visibility must refuse with 404, never a telling 403.

Exercises every ``VisibilityChoice`` value exhaustively rather than sampling
with Hypothesis: the enum has seven members, so the full space is smaller than
a sampled run would be, and this repo's ``TestCase`` does not mix ``@given``
with ``self.client`` (the client keeps state across generated examples).
Hypothesis is used below only for the pure-logic property, which needs no
client.
"""

from __future__ import annotations

from django.contrib.auth.models import User
from django.urls import reverse
from hypothesis import given, settings as hypothesis_settings, strategies as st
from model_bakery import baker

from urbanlens.core.tests.testcase import SimpleTestCase, TestCase
from urbanlens.dashboard.models.account.model import ApiKeyScope
from urbanlens.dashboard.models.profile.meta import VisibilityChoice
from urbanlens.dashboard.models.profile.model import _COMMUNITY_GATED_VISIBILITY_FIELDS, Profile
from urbanlens.dashboard.services.auth.api_keys import generate_api_key

#: The values that let a stranger (no friendship, no pin/friend/trip in common)
#: through ``Profile.visibility_permits``. Everything else must 404.
_STRANGER_PERMITTED = {VisibilityChoice.ANYONE}

#: The full public-presentation surface ``ProfileUpdateSerializer`` accepts -
#: bio/area/started_exploring plus the interaction-preference fields, which
#: are shown on the profile page the same way. Shared by the two tests below
#: that pin this exact set, so it only needs updating in one place.
_PROFILE_PRESENTATION_FIELDS = {
    "bio",
    "area",
    "started_exploring",
    "photo_taking_preference",
    "photo_taking_preference_other",
    "photo_sharing_preference",
    "photo_sharing_preference_other",
    "photo_tagging_preference",
    "photo_tagging_preference_other",
    "photo_usage_preference",
    "photo_usage_preference_other",
    "friend_request_preference",
    "friend_request_preference_other",
    "meetup_preference",
    "meetup_preference_other",
    "exploring_with_others_preference",
    "exploring_with_others_preference_other",
    "additional_preferences",
}


def _bearer(raw_key: str) -> dict:
    """Build the Authorization header kwargs for the test client.

    Args:
        raw_key: The plaintext API key.

    Returns:
        Kwargs to splat into a test-client call.
    """
    return {"HTTP_AUTHORIZATION": f"Bearer {raw_key}"}


class GatedVisibilityFieldListTests(TestCase):
    """The gated-field tuple is the single source of truth for the API surface."""

    def test_the_real_field_count_is_twelve(self) -> None:
        """Pins the count an earlier requirements doc got wrong (it claimed nine).

        If a thirteenth gated field is added this test fails loudly, which is
        the intended prompt to confirm the new field really should be exposed
        rather than silently shipping it.
        """
        self.assertEqual(len(_COMMUNITY_GATED_VISIBILITY_FIELDS), 12)

    def test_every_gated_field_exists_on_the_model(self) -> None:
        profile_fields = {field.name for field in Profile._meta.get_fields()}
        for name in _COMMUNITY_GATED_VISIBILITY_FIELDS:
            with self.subTest(field=name):
                self.assertIn(name, profile_fields)

    def test_visibility_serializer_covers_exactly_the_gated_fields(self) -> None:
        from urbanlens.dashboard.external_api.serializers import ProfileVisibilitySerializer

        self.assertEqual(set(ProfileVisibilitySerializer().fields), set(_COMMUNITY_GATED_VISIBILITY_FIELDS))

    @given(st.sampled_from([value for value, _label in VisibilityChoice.choices]))
    @hypothesis_settings(deadline=None)
    def test_visibility_values_are_lowercase_snake_case(self, value: str) -> None:
        """Unlike FriendshipStatus, these are lowercase - do not conflate the two."""
        self.assertEqual(value, value.lower())


class ProfileDetailVisibilityTests(TestCase):
    """A profile the caller may not see must 404, and never expose `visibility`."""

    def setUp(self) -> None:
        baker.make(User)
        self.user = baker.make(User)
        self.profile = Profile.objects.get(user=self.user)
        self.profile.slug = "caller"
        self.profile.save()

        self.target = Profile.objects.get(user=baker.make(User))
        self.target.slug = "target"
        self.target.save()

        api_key, self.raw_key = generate_api_key(self.user, "Test")
        api_key.scopes = [
            ApiKeyScope.PROFILE_READ.value,
            ApiKeyScope.SOCIAL_READ.value,
            ApiKeyScope.SOCIAL_WRITE.value,
        ]
        api_key.save(update_fields=["scopes"])

    def _get(self, slug: str):
        """GET one profile by slug.

        Args:
            slug: The profile slug to fetch.

        Returns:
            The HTTP response.
        """
        return self.client.get(
            reverse("external_api:profiles.detail", kwargs={"profile_slug": slug}), **_bearer(self.raw_key)
        )

    def test_non_permitted_visibility_always_404_never_403(self) -> None:
        """Across every VisibilityChoice, a stranger gets 200 or 404 - never 403."""
        for value, _label in VisibilityChoice.choices:
            with self.subTest(visibility=value):
                self.target.profile_visibility = value
                self.target.save()

                response = self._get("target")

                self.assertNotEqual(response.status_code, 403, "a 403 confirms the profile exists")
                expected = 200 if value in _STRANGER_PERMITTED else 404
                self.assertEqual(response.status_code, expected)

    def test_hidden_profile_is_byte_identical_to_a_nonexistent_one(self) -> None:
        self.target.profile_visibility = VisibilityChoice.NO_ONE
        self.target.save()

        hidden = self._get("target")
        missing = self._get("no-such-profile-anywhere")

        self.assertEqual(hidden.status_code, missing.status_code)
        self.assertEqual(hidden.content, missing.content)

    def test_visibility_block_is_absent_for_another_profile(self) -> None:
        """Your privacy configuration is itself private."""
        self.target.profile_visibility = VisibilityChoice.ANYONE
        self.target.save()

        response = self._get("target")

        self.assertEqual(response.status_code, 200)
        self.assertIsNone(response.json()["visibility"])
        self.assertFalse(response.json()["is_self"])

    def test_visibility_block_is_present_for_your_own_profile(self) -> None:
        response = self._get("caller")

        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertTrue(body["is_self"])
        self.assertEqual(set(body["visibility"]), set(_COMMUNITY_GATED_VISIBILITY_FIELDS))

    def test_contact_omitted_when_contact_visibility_refuses(self) -> None:
        self.target.profile_visibility = VisibilityChoice.ANYONE
        self.target.contact_visibility = VisibilityChoice.NO_ONE
        self.target.phone_number = "+15550001111"
        self.target.save()

        response = self._get("target")

        self.assertEqual(response.status_code, 200)
        self.assertIsNone(response.json()["contact"])

    def test_contact_included_when_contact_visibility_permits(self) -> None:
        self.target.profile_visibility = VisibilityChoice.ANYONE
        self.target.contact_visibility = VisibilityChoice.ANYONE
        self.target.phone_number = "+15550001111"
        self.target.save()

        response = self._get("target")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["contact"]["phone_number"], "+15550001111")

    def test_patching_another_profile_is_404_not_403(self) -> None:
        self.target.profile_visibility = VisibilityChoice.ANYONE
        self.target.save()

        response = self.client.patch(
            reverse("external_api:profiles.detail", kwargs={"profile_slug": "target"}),
            {"bio": "hacked"},
            content_type="application/json",
            **_bearer(self.raw_key),
        )

        self.assertEqual(response.status_code, 404)
        self.target.refresh_from_db()
        self.assertNotEqual(self.target.bio, "hacked")

    def test_patching_your_own_profile_works(self) -> None:
        response = self.client.patch(
            reverse("external_api:profiles.detail", kwargs={"profile_slug": "caller"}),
            {"bio": "urbex enjoyer", "area": "Rust Belt"},
            content_type="application/json",
            **_bearer(self.raw_key),
        )

        self.assertEqual(response.status_code, 200)
        self.profile.refresh_from_db()
        self.assertEqual(self.profile.bio, "urbex enjoyer")
        self.assertEqual(self.profile.area, "Rust Belt")

    def test_community_off_coerces_visibility_via_profile_save(self) -> None:
        """The model's own gating must apply, not be reimplemented in the view.

        Exercised through ``PATCH /settings/``, which is where the visibility
        fields live. It used to run through ``PATCH /profiles/{slug}/``, and
        moving it is the point rather than an inconvenience: that endpoint is
        gated on ``social:write`` - the scope an app asks for to send friend
        requests - so while it accepted ``profile_visibility`` a credential
        that could only message people could also switch the account's privacy
        settings to ``everyone``. The coercion behaviour this test was written
        for is unchanged; only the door it comes through is.
        """
        settings_key, settings_raw = generate_api_key(self.user, "Settings client")
        # PATCH requires both scopes - the response is always the full settings
        # document (see AccountSettingsView), which a write-only credential has
        # no business reading.
        settings_key.scopes = [ApiKeyScope.SETTINGS_WRITE.value, ApiKeyScope.SETTINGS_READ.value]
        settings_key.save(update_fields=["scopes"])

        response = self.client.patch(
            reverse("external_api:settings"),
            {"community_enabled": False, "profile_visibility": VisibilityChoice.ANYONE},
            content_type="application/json",
            **_bearer(settings_raw),
        )

        self.assertEqual(response.status_code, 200, response.content)
        self.profile.refresh_from_db()
        for name in _COMMUNITY_GATED_VISIBILITY_FIELDS:
            with self.subTest(field=name):
                self.assertEqual(getattr(self.profile, name), VisibilityChoice.NO_ONE)

    def test_visibility_fields_are_not_writable_through_the_profile_endpoint(self) -> None:
        """A ``social:write`` credential must not be able to rewrite privacy settings.

        The complement of the test above: the fields moved to ``/settings/``
        (behind ``settings:write``) must not still be honoured here. DRF
        ignores unknown keys, so the failure this guards against is silent - a
        200 whose body looks fine while the write did land.
        """
        from urbanlens.dashboard.external_api.serializers import ProfileUpdateSerializer

        self.profile.profile_visibility = VisibilityChoice.NO_ONE
        self.profile.save()

        response = self.client.patch(
            reverse("external_api:profiles.detail", kwargs={"profile_slug": "caller"}),
            {"profile_visibility": VisibilityChoice.ANYONE, "community_enabled": False},
            content_type="application/json",
            **_bearer(self.raw_key),
        )

        self.assertEqual(response.status_code, 200, response.content)
        self.profile.refresh_from_db()
        self.assertEqual(self.profile.profile_visibility, VisibilityChoice.NO_ONE)
        self.assertTrue(self.profile.community_enabled)
        self.assertEqual(set(ProfileUpdateSerializer().fields), _PROFILE_PRESENTATION_FIELDS)

    def test_avatar_is_not_writable(self) -> None:
        """Avatar upload is deliberately out of scope for this surface."""
        from urbanlens.dashboard.external_api.serializers import ProfileUpdateSerializer

        self.assertNotIn("avatar", ProfileUpdateSerializer().fields)


class ProfileSettingsOverlapTests(SimpleTestCase):
    """The profile-write and settings-write surfaces must stay disjoint.

    Named by ``ProfileUpdateSerializer``'s own docstring, which points here for
    the guarantee. A field writable through both endpoints would have two write
    paths to keep in agreement forever *and* two different scopes guarding it -
    and since ``PATCH /profiles/{slug}/`` is the weaker of the two
    (``social:write``), any overlap silently downgrades the protection on
    whatever it covers.
    """

    def test_no_field_is_writable_through_both_endpoints(self) -> None:
        from urbanlens.dashboard.external_api.serializers import ProfileUpdateSerializer, SettingsPatchSerializer

        overlap = set(ProfileUpdateSerializer().fields) & set(SettingsPatchSerializer().fields)
        self.assertEqual(overlap, set())

    def test_the_profile_surface_is_exactly_the_presentation_fields(self) -> None:
        """Pins the narrowed set, so an unreviewed field cannot creep back unnoticed."""
        from urbanlens.dashboard.external_api.serializers import ProfileUpdateSerializer

        self.assertEqual(set(ProfileUpdateSerializer().fields), _PROFILE_PRESENTATION_FIELDS)

    def test_every_gated_visibility_field_is_writable_only_through_settings(self) -> None:
        """The privacy fields the narrowing moved must still have a home."""
        from urbanlens.dashboard.external_api.serializers import SettingsPatchSerializer

        settings_fields = set(SettingsPatchSerializer().fields)
        for name in _COMMUNITY_GATED_VISIBILITY_FIELDS:
            with self.subTest(field=name):
                self.assertIn(name, settings_fields)


class ProfileNotesTests(TestCase):
    """Notes are always the caller's own and invisible to their subject."""

    def setUp(self) -> None:
        baker.make(User)
        self.user = baker.make(User)
        self.profile = Profile.objects.get(user=self.user)
        self.subject = Profile.objects.get(user=baker.make(User))
        self.subject.slug = "subject"
        self.subject.profile_visibility = VisibilityChoice.ANYONE
        self.subject.save()

        api_key, self.raw_key = generate_api_key(self.user, "Test")
        api_key.scopes = [ApiKeyScope.SOCIAL_READ.value, ApiKeyScope.SOCIAL_WRITE.value]
        api_key.save(update_fields=["scopes"])
        self.url = reverse("external_api:profiles.notes", kwargs={"profile_slug": "subject"})

    def test_multiple_notes_per_subject_are_allowed(self) -> None:
        for text in ("first", "second"):
            response = self.client.post(
                self.url, {"content": text}, content_type="application/json", **_bearer(self.raw_key)
            )
            self.assertEqual(response.status_code, 201)

        listing = self.client.get(self.url, **_bearer(self.raw_key))
        self.assertEqual(len(listing.json()), 2)

    def test_subject_cannot_see_notes_written_about_them(self) -> None:
        self.client.post(self.url, {"content": "suspicious"}, content_type="application/json", **_bearer(self.raw_key))

        subject_key, subject_raw = generate_api_key(self.subject.user, "Test")
        subject_key.scopes = [ApiKeyScope.SOCIAL_READ.value]
        subject_key.save(update_fields=["scopes"])

        self.profile.slug = "author"
        self.profile.profile_visibility = VisibilityChoice.ANYONE
        self.profile.save()

        # The subject asking about the author sees the subject's own notes
        # about the author - which is nothing - not the author's notes.
        response = self.client.get(
            reverse("external_api:profiles.notes", kwargs={"profile_slug": "author"}),
            **_bearer(subject_raw),
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), [])

    def test_another_users_note_uuid_reads_as_missing(self) -> None:
        create = self.client.post(
            self.url, {"content": "mine"}, content_type="application/json", **_bearer(self.raw_key)
        )
        note_uuid = create.json()["uuid"]

        stranger = baker.make(User)
        stranger_key, stranger_raw = generate_api_key(stranger, "Test")
        stranger_key.scopes = [ApiKeyScope.SOCIAL_WRITE.value]
        stranger_key.save(update_fields=["scopes"])

        response = self.client.delete(
            reverse("external_api:profiles.notes.detail", kwargs={"profile_slug": "subject", "note_uuid": note_uuid}),
            **_bearer(stranger_raw),
        )

        self.assertEqual(response.status_code, 404)
