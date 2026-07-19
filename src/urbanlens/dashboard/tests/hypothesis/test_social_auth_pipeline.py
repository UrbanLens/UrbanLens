"""Tests for the custom python-social-auth pipeline steps.

The 2FA detour step already has its own file (test_social_auth_sso_2fa.py);
these cover the remaining steps, which the coverage report showed almost
entirely unexercised despite being privacy-relevant: last-name suppression
for new SSO accounts, the avatar fetch's never-overwrite guarantee, Discord
handle sync, and SSO username generation (including its sanitization edges).
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import patch

from django.contrib.auth.models import User
from model_bakery import baker

from urbanlens.core.tests.testcase import SimpleTestCase, TestCase
from urbanlens.dashboard.services.social_auth.pipeline import (
    _sanitize_sso_username,
    fetch_and_save_avatar,
    generate_sso_username,
    mark_new_user_onboarding,
    save_discord_social_link,
    suppress_last_name_for_new_users,
)


def _backend(name: str = "google-oauth2") -> SimpleNamespace:
    return SimpleNamespace(name=name)


class SuppressLastNameTests(TestCase):
    """New SSO accounts get their provider-supplied last name stripped; existing accounts don't."""

    def test_new_user_last_name_is_cleared(self) -> None:
        user = baker.make(User, first_name="Ada", last_name="Lovelace")
        suppress_last_name_for_new_users(_backend(), user, {}, is_new=True)
        user.refresh_from_db()
        self.assertEqual(user.first_name, "Ada")
        self.assertEqual(user.last_name, "")

    def test_existing_user_last_name_is_preserved(self) -> None:
        """A user who deliberately added their last name in settings must not
        lose it on every subsequent SSO login."""
        user = baker.make(User, last_name="Kept")
        suppress_last_name_for_new_users(_backend(), user, {}, is_new=False)
        user.refresh_from_db()
        self.assertEqual(user.last_name, "Kept")

    def test_none_user_is_a_no_op(self) -> None:
        suppress_last_name_for_new_users(_backend(), None, {}, is_new=True)


class MarkNewUserOnboardingTests(TestCase):
    def test_new_user_is_flagged_for_onboarding(self) -> None:
        user = baker.make(User)
        user.profile.profile_setup_complete = True
        user.profile.save(update_fields=["profile_setup_complete"])
        mark_new_user_onboarding(_backend(), user, is_new=True)
        user.profile.refresh_from_db()
        self.assertFalse(user.profile.profile_setup_complete)

    def test_existing_user_is_untouched(self) -> None:
        user = baker.make(User)
        user.profile.profile_setup_complete = True
        user.profile.save(update_fields=["profile_setup_complete"])
        mark_new_user_onboarding(_backend(), user, is_new=False)
        user.profile.refresh_from_db()
        self.assertTrue(user.profile.profile_setup_complete)


class FetchAndSaveAvatarTests(TestCase):
    """The avatar fetch must never overwrite an avatar the user already has."""

    def test_existing_avatar_is_never_overwritten_or_even_looked_up(self) -> None:
        user = baker.make(User)
        user.profile.avatar = "avatars/existing.jpg"
        user.profile.save(update_fields=["avatar"])
        with (
            patch("urbanlens.dashboard.services.avatar.AvatarService.resolve_provider_url") as mock_resolve,
            patch("urbanlens.dashboard.services.avatar.AvatarService.download") as mock_download,
        ):
            fetch_and_save_avatar(_backend(), user, {}, is_new=False)
        mock_resolve.assert_not_called()
        mock_download.assert_not_called()
        user.profile.refresh_from_db()
        self.assertEqual(user.profile.avatar.name, "avatars/existing.jpg")

    def test_avatar_is_saved_when_profile_has_none(self) -> None:
        user = baker.make(User)
        with (
            patch("urbanlens.dashboard.services.avatar.AvatarService.resolve_provider_url", return_value="https://example.com/a.jpg"),
            patch("urbanlens.dashboard.services.avatar.AvatarService.download", return_value=b"\xff\xd8fakejpegbytes"),
        ):
            fetch_and_save_avatar(_backend(), user, {}, is_new=True)
        user.profile.refresh_from_db()
        self.assertTrue(user.profile.avatar)

    def test_unresolvable_provider_url_is_a_no_op(self) -> None:
        user = baker.make(User)
        with (
            patch("urbanlens.dashboard.services.avatar.AvatarService.resolve_provider_url", return_value=None),
            patch("urbanlens.dashboard.services.avatar.AvatarService.download") as mock_download,
        ):
            fetch_and_save_avatar(_backend(), user, {}, is_new=True)
        mock_download.assert_not_called()
        user.profile.refresh_from_db()
        self.assertFalse(user.profile.avatar)

    def test_failed_download_saves_nothing(self) -> None:
        user = baker.make(User)
        with (
            patch("urbanlens.dashboard.services.avatar.AvatarService.resolve_provider_url", return_value="https://example.com/a.jpg"),
            patch("urbanlens.dashboard.services.avatar.AvatarService.download", return_value=None),
        ):
            fetch_and_save_avatar(_backend(), user, {}, is_new=True)
        user.profile.refresh_from_db()
        self.assertFalse(user.profile.avatar)


class SaveDiscordSocialLinkTests(TestCase):
    def test_discord_login_stores_the_handle(self) -> None:
        from urbanlens.dashboard.models.social_link.model import SocialLink

        user = baker.make(User)
        save_discord_social_link(_backend("discord"), user, {"username": "urbex_ada"})
        link = SocialLink.objects.get(profile=user.profile, platform="discord")
        self.assertEqual(link.handle, "urbex_ada")

    def test_handle_change_on_discord_updates_the_stored_link(self) -> None:
        from urbanlens.dashboard.models.social_link.model import SocialLink

        user = baker.make(User)
        save_discord_social_link(_backend("discord"), user, {"username": "old_handle"})
        save_discord_social_link(_backend("discord"), user, {"username": "new_handle"})
        links = SocialLink.objects.filter(profile=user.profile, platform="discord")
        self.assertEqual([link.handle for link in links], ["new_handle"])

    def test_non_discord_backend_is_a_no_op(self) -> None:
        from urbanlens.dashboard.models.social_link.model import SocialLink

        user = baker.make(User)
        save_discord_social_link(_backend("google-oauth2"), user, {"username": "should_not_store"})
        self.assertFalse(SocialLink.objects.filter(profile=user.profile).exists())

    def test_missing_username_does_not_remove_an_existing_link(self) -> None:
        from urbanlens.dashboard.models.social_link.model import SocialLink

        user = baker.make(User)
        save_discord_social_link(_backend("discord"), user, {"username": "keep_me"})
        save_discord_social_link(_backend("discord"), user, {})
        self.assertEqual(SocialLink.objects.get(profile=user.profile, platform="discord").handle, "keep_me")


class GenerateSsoUsernameTests(TestCase):
    def test_returning_user_keeps_their_username(self) -> None:
        user = baker.make(User, username="already_here")
        result = generate_sso_username(_backend("discord"), user, {"username": "provider_name"}, {})
        self.assertEqual(result, {"username": "already_here"})

    def test_discord_handle_is_preferred_when_free(self) -> None:
        result = generate_sso_username(_backend("discord"), None, {"username": "fresh_handle"}, {})
        self.assertEqual(result, {"username": "fresh_handle"})

    def test_google_email_local_part_is_preferred_when_free(self) -> None:
        result = generate_sso_username(_backend("google-oauth2"), None, {}, {"email": "ada.lovelace@example.com"})
        assert result is not None
        self.assertEqual(result["username"], "ada_lovelace")

    def test_taken_handle_falls_back_to_a_generated_name(self) -> None:
        baker.make(User, username="taken_handle")
        result = generate_sso_username(_backend("discord"), None, {"username": "taken_handle"}, {})
        assert result is not None
        self.assertNotEqual(result["username"], "taken_handle")
        self.assertTrue(result["username"])

    def test_unknown_backend_falls_back_to_a_generated_name(self) -> None:
        result = generate_sso_username(_backend("github"), None, {"username": "whoever"}, {})
        assert result is not None
        self.assertTrue(result["username"])


class SanitizeSsoUsernameTests(SimpleTestCase):
    """Edge cases of the provider-handle normalizer (pure function)."""

    def test_email_uses_local_part_with_dots_converted(self) -> None:
        self.assertEqual(_sanitize_sso_username("ada.lovelace@gmail.com"), "ada_lovelace")

    def test_special_characters_collapse_to_single_underscores(self) -> None:
        self.assertEqual(_sanitize_sso_username("cool--user!!name"), "cool_user_name")

    def test_too_short_after_sanitizing_returns_none(self) -> None:
        self.assertIsNone(_sanitize_sso_username("a!"))

    def test_overlong_handle_is_trimmed_to_thirty_characters(self) -> None:
        result = _sanitize_sso_username("x" * 40)
        assert result is not None
        self.assertEqual(len(result), 30)

    def test_trimmed_handle_never_ends_with_an_underscore(self) -> None:
        # "a_" * 20 collapses to itself (40 chars); the 30-char cut lands on an
        # underscore, which must be stripped rather than kept as a trailing "_".
        result = _sanitize_sso_username("a_" * 20)
        assert result is not None
        self.assertFalse(result.endswith("_"))
        self.assertLessEqual(len(result), 30)

    def test_all_symbols_returns_none(self) -> None:
        self.assertIsNone(_sanitize_sso_username("!!!@@@###"))
