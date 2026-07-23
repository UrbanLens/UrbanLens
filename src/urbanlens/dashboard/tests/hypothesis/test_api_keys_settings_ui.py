"""Tests for the Settings > Security > API Keys management UI.

Mirrors the shape of passkey rename/delete and TOTP action tests in spirit:
creation must reveal the plaintext exactly once, revocation must be scoped to
the requesting user and take effect immediately (the external API can no
longer authenticate with it), and both actions must never touch another
user's keys.
"""

from __future__ import annotations

from django.contrib.auth.models import User
from django.urls import reverse
from model_bakery import baker

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.models.account.model import ApiKey
from urbanlens.dashboard.services.api_keys import authenticate_api_key, generate_api_key, record_api_key_usage


class ApiKeyCreateViewTests(TestCase):
    def setUp(self) -> None:
        baker.make(User)  # first user auto-promoted to bootstrap site admin
        self.user = baker.make(User)
        self.client.force_login(self.user)

    def test_create_persists_a_key_owned_by_the_current_user(self) -> None:
        response = self.client.post(reverse("settings.security.api_keys.create"), {"name": "Zapier"})
        self.assertEqual(response.status_code, 302)
        api_key = ApiKey.objects.get(user=self.user)
        self.assertEqual(api_key.name, "Zapier")

    def test_htmx_request_reveals_the_plaintext_key_once(self) -> None:
        response = self.client.post(
            reverse("settings.security.api_keys.create"),
            {"name": "Zapier"},
            HTTP_HX_REQUEST="true",
        )
        self.assertEqual(response.status_code, 200)
        api_key = ApiKey.objects.get(user=self.user)
        self.assertContains(response, api_key.prefix)

    def test_plaintext_key_is_not_shown_again_on_a_later_render(self) -> None:
        self.client.post(reverse("settings.security.api_keys.create"), {"name": "Zapier"}, HTTP_HX_REQUEST="true")
        api_key = ApiKey.objects.get(user=self.user)

        second_response = self.client.post(
            reverse("settings.security.api_keys.create"),
            {"name": "Second app"},
            HTTP_HX_REQUEST="true",
        )
        # The first key's identifying prefix must not leak into a later render's body.
        self.assertNotContains(second_response, api_key.prefix)


class ApiKeyRevokeViewTests(TestCase):
    def setUp(self) -> None:
        baker.make(User)  # first user auto-promoted to bootstrap site admin
        self.user = baker.make(User)
        self.client.force_login(self.user)
        self.api_key, self.raw_key = generate_api_key(self.user, "Zapier")

    def test_revoking_own_key_disables_it_immediately(self) -> None:
        response = self.client.post(reverse("settings.security.api_keys.revoke", args=[self.api_key.pk]))
        self.assertEqual(response.status_code, 302)
        self.assertIsNone(authenticate_api_key(self.raw_key))

    def test_cannot_revoke_another_users_key(self) -> None:
        other_user = baker.make(User)
        other_key, other_raw_key = generate_api_key(other_user, "Someone else's")

        self.client.post(reverse("settings.security.api_keys.revoke", args=[other_key.pk]))

        self.assertIsNotNone(authenticate_api_key(other_raw_key))
        other_key.refresh_from_db()
        self.assertFalse(other_key.is_revoked)

    def test_htmx_revoke_response_no_longer_shows_a_revoke_button_for_that_key(self) -> None:
        response = self.client.post(
            reverse("settings.security.api_keys.revoke", args=[self.api_key.pk]),
            HTTP_HX_REQUEST="true",
        )
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, reverse("settings.security.api_keys.revoke", args=[self.api_key.pk]), count=0)


class ApiKeysSettingsPageContentTests(TestCase):
    """The full settings page surfaces usage docs and per-key recent activity."""

    def setUp(self) -> None:
        baker.make(User)  # first user auto-promoted to bootstrap site admin
        self.user = baker.make(User)
        self.client.force_login(self.user)

    def test_page_shows_real_endpoint_urls_for_the_usage_example(self) -> None:
        response = self.client.get(reverse("settings.view"))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, reverse("external_api:whoami"))
        self.assertContains(response, reverse("external_api:pins.create"))

    def test_page_shows_recent_activity_for_a_key_with_usage(self) -> None:
        api_key, _raw_key = generate_api_key(self.user, "Zapier")
        record_api_key_usage(api_key, "/dashboard/api/external/v1/whoami/")

        response = self.client.get(reverse("settings.view"))

        self.assertContains(response, "/dashboard/api/external/v1/whoami/")

    def test_page_omits_activity_block_for_a_key_with_no_usage(self) -> None:
        generate_api_key(self.user, "Unused App")
        response = self.client.get(reverse("settings.view"))
        self.assertNotContains(response, "Recent activity")
