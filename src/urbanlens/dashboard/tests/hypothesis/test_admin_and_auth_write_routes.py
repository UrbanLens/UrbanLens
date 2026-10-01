"""The developer toolbar's admin gate, and the pre-login passkey options route."""

from __future__ import annotations

import json
from unittest import mock

from django.contrib.auth.models import Permission, User
from django.urls import reverse
from model_bakery import baker
from webauthn.helpers import bytes_to_base64url

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.controllers import account as account_controllers
from urbanlens.dashboard.models.account import WebAuthnCredential
from urbanlens.dashboard.models.profile.model import GuidanceLevel, Profile, ThemeChoice
from urbanlens.dashboard.services.auth.webauthn import SESSION_AUTHENTICATION_CHALLENGE

_DEV_ENV = "urbanlens.dashboard.models.site_settings.model.SiteSettings.is_development_environment"


class DevToolbarRouteTests(TestCase):
    """Each route changes only the caller's own profile, and only for a site admin on a development site."""

    def setUp(self) -> None:
        super().setUp()
        baker.make(User)  # absorbs the bootstrap site-admin promotion
        self.admin = baker.make(User)
        self.admin.user_permissions.add(Permission.objects.get(codename="view_site_admin"))
        self.member = baker.make(User)
        for user in (self.admin, self.member):
            Profile.objects.filter(user=user).update(
                theme_mode=ThemeChoice.LIGHT,
                map_dark_mode=ThemeChoice.LIGHT,
                guidance_level=GuidanceLevel.NONE,
                welcome_onboarding_complete=True,
            )

    def _profile_state(self, user: User) -> tuple:
        return (
            Profile.objects.filter(user=user)
            .values_list("theme_mode", "map_dark_mode", "guidance_level", "welcome_onboarding_complete")
            .get()
        )

    def _post_all(self, *, dev: bool) -> dict[str, int]:
        with mock.patch(_DEV_ENV, return_value=dev):
            return {
                name: self.client.post(reverse(name)).status_code
                for name in (
                    "dev_toolbar.toggle_theme",
                    "dev_toolbar.toggle_map_dark_mode",
                    "dev_toolbar.reset_onboarding",
                )
            }

    def test_an_admin_on_a_development_site_changes_their_own_profile(self) -> None:
        self.client.force_login(self.admin)

        statuses = self._post_all(dev=True)

        self.assertEqual(set(statuses.values()), {204}, statuses)
        self.assertEqual(
            self._profile_state(self.admin), (ThemeChoice.DARK, ThemeChoice.DARK, GuidanceLevel.ALL, False)
        )
        self.assertEqual(
            self._profile_state(self.member), (ThemeChoice.LIGHT, ThemeChoice.LIGHT, GuidanceLevel.NONE, True)
        )

    def test_a_non_admin_is_refused_and_nothing_changes(self) -> None:
        self.client.force_login(self.member)

        statuses = self._post_all(dev=True)

        self.assertEqual(set(statuses.values()), {403}, statuses)
        self.assertEqual(
            self._profile_state(self.member), (ThemeChoice.LIGHT, ThemeChoice.LIGHT, GuidanceLevel.NONE, True)
        )

    def test_an_admin_on_a_non_development_site_is_refused(self) -> None:
        self.client.force_login(self.admin)

        statuses = self._post_all(dev=False)

        self.assertEqual(set(statuses.values()), {403}, statuses)
        self.assertEqual(
            self._profile_state(self.admin), (ThemeChoice.LIGHT, ThemeChoice.LIGHT, GuidanceLevel.NONE, True)
        )

    def test_anonymous_is_refused(self) -> None:
        """403 rather than a login redirect: ``raise_exception`` covers the login check as well as the permission."""
        with mock.patch(_DEV_ENV, return_value=True):
            for name in (
                "dev_toolbar.toggle_theme",
                "dev_toolbar.toggle_map_dark_mode",
                "dev_toolbar.reset_onboarding",
                "dev_toolbar.clear_session",
            ):
                self.assertEqual(self.client.post(reverse(name)).status_code, 403, name)

    def test_the_toolbar_has_a_map_dark_mode_button(self) -> None:
        """Jess, 2026-09-30: the route had no button."""
        self.client.force_login(self.admin)
        with mock.patch(
            "urbanlens.dashboard.models.site_settings.model.SiteSettings.show_dev_admin_features", return_value=True
        ):
            page = self.client.get(reverse("faq")).content.decode()

        button = page[page.index('id="dev-toolbar-map-dark-toggle"') :]
        button = button[: button.index("</button>")]
        self.assertIn(f'hx-post="{reverse("dev_toolbar.toggle_map_dark_mode")}"', button)
        self.assertIn('data-map-dark-mode="light"', button)

    def test_clear_session_logs_the_admin_out_on_a_development_site(self) -> None:
        self.client.force_login(self.admin)

        with mock.patch(_DEV_ENV, return_value=True):
            response = self.client.post(reverse("dev_toolbar.clear_session"))

        self.assertEqual(response.status_code, 204)
        self.assertNotIn("_auth_user_id", self.client.session)

    def test_clear_session_is_refused_to_a_non_admin_and_to_production(self) -> None:
        self.client.force_login(self.member)
        with mock.patch(_DEV_ENV, return_value=True):
            self.assertEqual(self.client.post(reverse("dev_toolbar.clear_session")).status_code, 403)
        self.assertEqual(self.client.session.get("_auth_user_id"), str(self.member.pk))

        self.client.force_login(self.admin)
        with mock.patch(_DEV_ENV, return_value=False):
            self.assertEqual(self.client.post(reverse("dev_toolbar.clear_session")).status_code, 403)
        self.assertEqual(self.client.session.get("_auth_user_id"), str(self.admin.pk))


class LoginTwoFactorOptionsRouteTests(TestCase):
    """Only the user mid-login in *this* session gets a challenge, scoped to their own login passkeys."""

    def setUp(self) -> None:
        super().setUp()
        baker.make(User)  # absorbs the bootstrap site-admin promotion
        self.user = baker.make(User, is_active=True)
        self.other = baker.make(User, is_active=True)
        self.login_key = baker.make(
            WebAuthnCredential, user=self.user, credential_id=b"mine-login", is_login_factor=True
        )
        self.unlock_key = baker.make(
            WebAuthnCredential, user=self.user, credential_id=b"mine-unlock", is_login_factor=False
        )
        self.others_key = baker.make(WebAuthnCredential, user=self.other, credential_id=b"theirs", is_login_factor=True)
        self.url = reverse("login.2fa.options")

    def _pending(self, user: User) -> None:
        session = self.client.session
        session[account_controllers._WEBAUTHN_PENDING_USER_KEY] = user.pk
        session.save()

    def test_offers_only_the_pending_users_login_passkeys_and_stores_a_challenge(self) -> None:
        self._pending(self.user)

        response = self.client.post(self.url)

        self.assertEqual(response.status_code, 200)
        allowed = {entry["id"] for entry in json.loads(response.content)["allowCredentials"]}
        self.assertEqual(allowed, {bytes_to_base64url(b"mine-login")})
        self.assertIn(SESSION_AUTHENTICATION_CHALLENGE, self.client.session)

    def test_no_sign_in_in_progress_is_400_and_stores_no_challenge(self) -> None:
        response = self.client.post(self.url)

        self.assertEqual(response.status_code, 400)
        self.assertNotIn(SESSION_AUTHENTICATION_CHALLENGE, self.client.session)

    def test_a_pending_user_with_no_login_passkey_is_400(self) -> None:
        bare = baker.make(User, is_active=True)
        self._pending(bare)

        response = self.client.post(self.url)

        self.assertEqual(response.status_code, 400)
        self.assertNotIn(SESSION_AUTHENTICATION_CHALLENGE, self.client.session)

    def test_a_deactivated_pending_user_is_400(self) -> None:
        self._pending(self.user)
        User.objects.filter(pk=self.user.pk).update(is_active=False)

        response = self.client.post(self.url)

        self.assertEqual(response.status_code, 400)
        self.assertNotIn(SESSION_AUTHENTICATION_CHALLENGE, self.client.session)
