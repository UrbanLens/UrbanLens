"""Tests for the external API's account-settings sync endpoint.

The endpoint has to hold three lines at once: it must not become a back door
around the site's own gating (feature entitlements, the community kill switch),
it must be genuinely partial so a one-toggle sync never clobbers concurrent web
edits, and it must report what the profile *actually ended up as* rather than
echoing the submission - because ``Profile.save()`` rewrites community-gated
fields underneath it.
"""

from __future__ import annotations

from unittest import mock

from django.contrib.auth.models import User
from django.urls import reverse
from model_bakery import baker

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.models.account.model import ApiKey, ApiKeyScope
from urbanlens.dashboard.models.profile.meta import SyncAliasesDirection, ThemeChoice, VisibilityChoice
from urbanlens.dashboard.models.profile.model import Profile
from urbanlens.dashboard.services.auth.api_keys import generate_api_key
from urbanlens.dashboard.services.profile.profile_settings import SETTINGS_FIELDS, SettingsValidationError, apply_settings_patch, read_settings


def _bearer(raw_key: str) -> dict:
    return {"HTTP_AUTHORIZATION": f"Bearer {raw_key}"}


class _SettingsApiTestCase(TestCase):
    """Shared setup: a user with a key scoped for reading and writing settings."""

    scopes: list[str] = [ApiKeyScope.SETTINGS_READ.value, ApiKeyScope.SETTINGS_WRITE.value]

    def setUp(self) -> None:
        baker.make(User)  # first user is auto-promoted to bootstrap site admin
        self.user = baker.make(User)
        self.profile = Profile.objects.get(user=self.user)
        key, self.raw_key = generate_api_key(self.user, "Test")
        # scopes is editable=False, so it is set directly rather than through a
        # form. The default grant deliberately excludes settings:* - see
        # _default_api_key_scopes.
        ApiKey.objects.filter(pk=key.pk).update(scopes=self.scopes)
        self.url = reverse("external_api:settings")


class SettingsScopeTests(_SettingsApiTestCase):
    """Each method honors only the scope it declares."""

    def test_get_requires_settings_read(self) -> None:
        ApiKey.objects.filter(user=self.user).update(scopes=[ApiKeyScope.PINS_READ.value])
        response = self.client.get(self.url, **_bearer(self.raw_key))
        self.assertEqual(response.status_code, 403)

    def test_patch_requires_settings_write(self) -> None:
        """A read-only grant cannot change anything."""
        ApiKey.objects.filter(user=self.user).update(scopes=[ApiKeyScope.SETTINGS_READ.value])
        response = self.client.patch(self.url, {"theme_mode": ThemeChoice.LIGHT}, content_type="application/json", **_bearer(self.raw_key))
        self.assertEqual(response.status_code, 403)

    def test_unauthenticated_request_is_rejected(self) -> None:
        self.assertIn(self.client.get(self.url).status_code, (401, 403))

    def test_default_api_key_grant_cannot_reach_settings(self) -> None:
        """A key issued today has no settings access at all - the opt-in rule."""
        _key, raw = generate_api_key(self.user, "Default grant")
        self.assertEqual(self.client.get(self.url, **_bearer(raw)).status_code, 403)


class SettingsReadTests(_SettingsApiTestCase):
    """GET returns the whole allowlist plus computed context."""

    def test_get_returns_every_allowlisted_field(self) -> None:
        response = self.client.get(self.url, **_bearer(self.raw_key))
        self.assertEqual(response.status_code, 200)
        for field in SETTINGS_FIELDS:
            with self.subTest(field=field):
                self.assertIn(field, response.json())

    def test_get_includes_computed_context(self) -> None:
        """A client can render the settings UI without a second round-trip."""
        payload = self.client.get(self.url, **_bearer(self.raw_key)).json()
        for key in ("updated", "effective_distance_units", "features", "allowed_image_dimensions", "allowed_video_heights"):
            with self.subTest(key=key):
                self.assertIn(key, payload)
        self.assertIn("ai", payload["features"])
        self.assertIn("places", payload["features"])

    def test_get_leaks_nothing_outside_the_allowlist(self) -> None:
        """Profile carries location history and subscription linkage - none of it here."""
        payload = self.client.get(self.url, **_bearer(self.raw_key)).json()
        allowed = set(SETTINGS_FIELDS) | {"updated", "effective_distance_units", "features", "allowed_image_dimensions", "allowed_video_heights"}
        self.assertEqual(set(payload) - allowed, set())


class SettingsPatchTests(_SettingsApiTestCase):
    """PATCH round-trips, stays partial, and validates."""

    def test_patch_round_trips(self) -> None:
        response = self.client.patch(self.url, {"theme_mode": ThemeChoice.LIGHT}, content_type="application/json", **_bearer(self.raw_key))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["theme_mode"], ThemeChoice.LIGHT)
        self.profile.refresh_from_db()
        self.assertEqual(self.profile.theme_mode, ThemeChoice.LIGHT)

    def test_patch_is_partial(self) -> None:
        """An unsubmitted field is left alone, not reset to its default."""
        Profile.objects.filter(pk=self.profile.pk).update(track_routes=False, map_default_zoom=7)
        self.client.patch(self.url, {"theme_mode": ThemeChoice.LIGHT}, content_type="application/json", **_bearer(self.raw_key))
        self.profile.refresh_from_db()
        self.assertFalse(self.profile.track_routes)
        self.assertEqual(self.profile.map_default_zoom, 7)

    def test_patch_rejects_an_invalid_choice(self) -> None:
        response = self.client.patch(self.url, {"theme_mode": "chartreuse"}, content_type="application/json", **_bearer(self.raw_key))
        self.assertEqual(response.status_code, 400)

    def test_patch_ignores_a_field_outside_the_allowlist(self) -> None:
        """An unknown key is dropped by the serializer, never written to the model."""
        response = self.client.patch(self.url, {"is_superuser": True, "theme_mode": ThemeChoice.LIGHT}, content_type="application/json", **_bearer(self.raw_key))
        self.assertEqual(response.status_code, 200)
        self.user.refresh_from_db()
        self.assertFalse(self.user.is_superuser)

    def test_patch_accepts_null_for_a_nullable_field(self) -> None:
        """Null is a real value here - it resets distance units to "infer"."""
        Profile.objects.filter(pk=self.profile.pk).update(distance_units="mi")
        response = self.client.patch(self.url, {"distance_units": None}, content_type="application/json", **_bearer(self.raw_key))
        self.assertEqual(response.status_code, 200)
        self.profile.refresh_from_db()
        self.assertIsNone(self.profile.distance_units)

    def test_empty_patch_is_a_no_op(self) -> None:
        response = self.client.patch(self.url, {}, content_type="application/json", **_bearer(self.raw_key))
        self.assertEqual(response.status_code, 200)


class SettingsCommunityGatingTests(_SettingsApiTestCase):
    """Profile.save()'s coercion is reported back, not fought."""

    def test_disabling_community_coerces_visibilities_and_reports_them(self) -> None:
        """The response shows the coerced values, not what the client submitted."""
        response = self.client.patch(
            self.url,
            {"community_enabled": False, "profile_visibility": VisibilityChoice.ANYONE},
            content_type="application/json",
            **_bearer(self.raw_key),
        )
        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertFalse(payload["community_enabled"])
        self.assertEqual(payload["profile_visibility"], VisibilityChoice.NO_ONE)
        self.profile.refresh_from_db()
        self.assertEqual(self.profile.profile_visibility, VisibilityChoice.NO_ONE)

    def test_disabling_community_turns_off_wiki_sync(self) -> None:
        response = self.client.patch(
            self.url,
            {"community_enabled": False, "sync_rating_to_wiki": True, "sync_aliases": SyncAliasesDirection.BOTH},
            content_type="application/json",
            **_bearer(self.raw_key),
        )
        payload = response.json()
        self.assertFalse(payload["sync_rating_to_wiki"])
        self.assertEqual(payload["sync_aliases"], SyncAliasesDirection.OFF)

    def test_coercion_is_not_reported_as_an_error(self) -> None:
        """A client asking for something the model overrides still gets a 200."""
        Profile.objects.filter(pk=self.profile.pk).update(community_enabled=False)
        response = self.client.patch(self.url, {"contact_visibility": VisibilityChoice.ANYONE}, content_type="application/json", **_bearer(self.raw_key))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["contact_visibility"], VisibilityChoice.NO_ONE)


class SettingsFeatureGatingTests(_SettingsApiTestCase):
    """A gated field submitted while its feature is off is a 400, not a silent drop."""

    def test_ai_field_rejected_when_feature_is_off(self) -> None:
        with mock.patch("urbanlens.dashboard.services.profile.profile_settings.user_has_feature", return_value=False):
            response = self.client.patch(self.url, {"ai_enabled": True}, content_type="application/json", **_bearer(self.raw_key))
        self.assertEqual(response.status_code, 400)
        self.assertIn("ai_enabled", response.json()["fields"])

    def test_places_field_rejected_when_feature_is_off(self) -> None:
        with mock.patch("urbanlens.dashboard.services.profile.profile_settings.user_has_feature", return_value=False):
            response = self.client.patch(self.url, {"places_google_enabled": False}, content_type="application/json", **_bearer(self.raw_key))
        self.assertEqual(response.status_code, 400)
        self.assertIn("places_google_enabled", response.json()["fields"])

    def test_rejected_patch_writes_nothing_at_all(self) -> None:
        """A partially-invalid patch is refused whole - no half-applied state."""
        original = self.profile.theme_mode
        with mock.patch("urbanlens.dashboard.services.profile.profile_settings.user_has_feature", return_value=False):
            response = self.client.patch(
                self.url,
                {"ai_enabled": True, "theme_mode": ThemeChoice.LIGHT},
                content_type="application/json",
                **_bearer(self.raw_key),
            )
        self.assertEqual(response.status_code, 400)
        self.profile.refresh_from_db()
        self.assertEqual(self.profile.theme_mode, original)

    def test_ungated_fields_are_unaffected_by_a_missing_feature(self) -> None:
        with mock.patch("urbanlens.dashboard.services.profile.profile_settings.user_has_feature", return_value=False):
            response = self.client.patch(self.url, {"theme_mode": ThemeChoice.LIGHT}, content_type="application/json", **_bearer(self.raw_key))
        self.assertEqual(response.status_code, 200)


class SettingsNameAndContactTests(_SettingsApiTestCase):
    """``first_name``/``last_name`` and the six contact methods, via /settings/.

    These live on ``User`` (name) and ``Profile`` (contact) respectively, but
    both are meant to look like an ordinary settings field to a client - see
    ``services.profile.profile_settings``'s docstring on why they're allowlisted here
    rather than left to ``PATCH /profiles/{slug}/``.
    """

    def test_patch_writes_first_and_last_name_to_the_user(self) -> None:
        response = self.client.patch(
            self.url,
            {"first_name": "Ada", "last_name": "Lovelace"},
            content_type="application/json",
            **_bearer(self.raw_key),
        )
        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload["first_name"], "Ada")
        self.assertEqual(payload["last_name"], "Lovelace")
        self.user.refresh_from_db()
        self.assertEqual(self.user.first_name, "Ada")
        self.assertEqual(self.user.last_name, "Lovelace")

    def test_patch_name_only_touches_user_not_profile_update_fields(self) -> None:
        """A name-only patch must not blow up on an empty Profile update_fields list."""
        Profile.objects.filter(pk=self.profile.pk).update(map_default_zoom=7)
        response = self.client.patch(self.url, {"first_name": "Grace"}, content_type="application/json", **_bearer(self.raw_key))
        self.assertEqual(response.status_code, 200)
        self.profile.refresh_from_db()
        self.assertEqual(self.profile.map_default_zoom, 7)

    def test_patch_writes_contact_methods_to_the_profile(self) -> None:
        response = self.client.patch(
            self.url,
            {"phone_number": "+15551234567", "signal_username": "ada.99", "matrix_handle": "@ada:matrix.org"},
            content_type="application/json",
            **_bearer(self.raw_key),
        )
        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload["phone_number"], "+15551234567")
        self.assertEqual(payload["signal_username"], "ada.99")
        self.assertEqual(payload["matrix_handle"], "@ada:matrix.org")
        self.profile.refresh_from_db()
        self.assertEqual(self.profile.phone_number, "+15551234567")

    def test_patch_rejects_a_discord_username_outside_the_allowed_charset(self) -> None:
        response = self.client.patch(self.url, {"discord_username": "no spaces!"}, content_type="application/json", **_bearer(self.raw_key))
        self.assertEqual(response.status_code, 400)
        self.assertIn("discord_username", response.json()["fields"])
        self.profile.refresh_from_db()
        self.assertEqual(self.profile.discord_username, "")

    def test_patch_accepts_a_valid_discord_username(self) -> None:
        response = self.client.patch(self.url, {"discord_username": "ada.lovelace"}, content_type="application/json", **_bearer(self.raw_key))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["discord_username"], "ada.lovelace")

    def test_patch_clears_a_contact_method_with_an_empty_string(self) -> None:
        Profile.objects.filter(pk=self.profile.pk).update(phone_number="+15551234567")
        response = self.client.patch(self.url, {"phone_number": ""}, content_type="application/json", **_bearer(self.raw_key))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["phone_number"], "")
        self.profile.refresh_from_db()
        self.assertEqual(self.profile.phone_number, "")

    def test_email_and_username_remain_unwritable(self) -> None:
        """Login identity stays off the allowlist even though name/contact are now on it.

        Neither is a declared ``SettingsPatchSerializer`` field, so - like any
        other undeclared key - it is silently dropped rather than rejected;
        see ``test_patch_ignores_a_field_outside_the_allowlist`` for the same
        contract against ``is_superuser``.
        """
        original_email = self.user.email
        original_username = self.user.username
        response = self.client.patch(
            self.url,
            {"email": "new@example.com", "username": "newname", "first_name": "Ada"},
            content_type="application/json",
            **_bearer(self.raw_key),
        )
        self.assertEqual(response.status_code, 200)
        self.user.refresh_from_db()
        self.assertEqual(self.user.email, original_email)
        self.assertEqual(self.user.username, original_username)
        self.assertEqual(self.user.first_name, "Ada")


class SettingsServiceTests(TestCase):
    """Direct tests of the service, below the HTTP layer."""

    def setUp(self) -> None:
        baker.make(User)
        self.user = baker.make(User)
        self.profile = Profile.objects.get(user=self.user)

    def test_apply_returns_touched_field_names(self) -> None:
        touched = apply_settings_patch(self.profile, {"theme_mode": ThemeChoice.LIGHT}, user=self.user)
        self.assertEqual(touched, ["theme_mode"])

    def test_apply_rejects_an_unknown_field(self) -> None:
        with self.assertRaises(SettingsValidationError) as ctx:
            apply_settings_patch(self.profile, {"not_a_setting": 1}, user=self.user)
        self.assertIn("not_a_setting", ctx.exception.errors)

    def test_apply_saves_first_name_directly_to_the_user(self) -> None:
        """Unlike Profile fields, name changes are saved immediately, not left for the caller."""
        touched = apply_settings_patch(self.profile, {"first_name": "Ada"}, user=self.user)
        self.assertEqual(touched, [])
        self.user.refresh_from_db()
        self.assertEqual(self.user.first_name, "Ada")

    def test_apply_does_not_save_user_when_no_name_field_is_submitted(self) -> None:
        with mock.patch.object(User, "save") as mock_save:
            apply_settings_patch(self.profile, {"theme_mode": ThemeChoice.LIGHT}, user=self.user)
        mock_save.assert_not_called()

    def test_apply_rejects_a_discord_username_outside_the_allowed_charset(self) -> None:
        with self.assertRaises(SettingsValidationError) as ctx:
            apply_settings_patch(self.profile, {"discord_username": "!!!"}, user=self.user)
        self.assertIn("discord_username", ctx.exception.errors)

    def test_apply_rejects_a_storage_dimension_above_entitlement(self) -> None:
        with mock.patch("urbanlens.dashboard.services.profile.profile_settings.allowed_user_dimension_values", return_value={1080}), self.assertRaises(SettingsValidationError) as ctx:
            apply_settings_patch(self.profile, {"image_downscale_max_dimension": 999999}, user=self.user)
        self.assertIn("image_downscale_max_dimension", ctx.exception.errors)

    def test_apply_allows_null_storage_dimension(self) -> None:
        """Null means "no downscaling preference" and is always permitted."""
        touched = apply_settings_patch(self.profile, {"image_downscale_max_dimension": None}, user=self.user)
        self.assertEqual(touched, ["image_downscale_max_dimension"])

    def test_read_settings_covers_the_whole_allowlist(self) -> None:
        payload = read_settings(self.profile, user=self.user)
        for field in SETTINGS_FIELDS:
            with self.subTest(field=field):
                self.assertIn(field, payload)

    def test_every_allowlisted_field_exists_on_the_model(self) -> None:
        """A typo in SETTINGS_FIELDS would otherwise surface only at request time."""
        for field in SETTINGS_FIELDS:
            with self.subTest(field=field):
                self.assertTrue(hasattr(self.profile, field), f"Profile has no field {field!r}")
