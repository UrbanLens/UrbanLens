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
from urbanlens.dashboard.services.api_keys import generate_api_key
from urbanlens.dashboard.services.profile_settings import SETTINGS_FIELDS, SettingsValidationError, apply_settings_patch, read_settings


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
        with mock.patch("urbanlens.dashboard.services.profile_settings.user_has_feature", return_value=False):
            response = self.client.patch(self.url, {"ai_enabled": True}, content_type="application/json", **_bearer(self.raw_key))
        self.assertEqual(response.status_code, 400)
        self.assertIn("ai_enabled", response.json()["fields"])

    def test_places_field_rejected_when_feature_is_off(self) -> None:
        with mock.patch("urbanlens.dashboard.services.profile_settings.user_has_feature", return_value=False):
            response = self.client.patch(self.url, {"places_google_enabled": False}, content_type="application/json", **_bearer(self.raw_key))
        self.assertEqual(response.status_code, 400)
        self.assertIn("places_google_enabled", response.json()["fields"])

    def test_rejected_patch_writes_nothing_at_all(self) -> None:
        """A partially-invalid patch is refused whole - no half-applied state."""
        original = self.profile.theme_mode
        with mock.patch("urbanlens.dashboard.services.profile_settings.user_has_feature", return_value=False):
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
        with mock.patch("urbanlens.dashboard.services.profile_settings.user_has_feature", return_value=False):
            response = self.client.patch(self.url, {"theme_mode": ThemeChoice.LIGHT}, content_type="application/json", **_bearer(self.raw_key))
        self.assertEqual(response.status_code, 200)


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

    def test_apply_rejects_a_storage_dimension_above_entitlement(self) -> None:
        with mock.patch("urbanlens.dashboard.services.profile_settings.allowed_user_dimension_values", return_value={1080}):
            with self.assertRaises(SettingsValidationError) as ctx:
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
