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
from urbanlens.dashboard.services.auth.api_keys import authenticate_api_key, generate_api_key, record_api_key_usage


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

    def test_non_htmx_create_reveals_the_key_once_on_the_redirected_settings_page(self) -> None:
        """The default (non-htmx) flow redirects to the settings page, which does
        the actual reveal - the session flash must survive that redirect and
        still be gone by the next render."""
        create_response = self.client.post(reverse("settings.security.api_keys.create"), {"name": "Zapier"})
        self.assertEqual(create_response.status_code, 302)
        api_key = ApiKey.objects.get(user=self.user)

        first_view = self.client.get(reverse("settings.view"))
        self.assertContains(first_view, api_key.prefix)

        second_view = self.client.get(reverse("settings.view"))
        self.assertNotContains(second_view, api_key.prefix)


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
        revoke_url = reverse("settings.security.api_keys.revoke", args=[self.api_key.pk])
        # Confirm the button is really there beforehand - otherwise a template
        # regression that never renders a revoke button at all would still
        # pass the post-revoke assertion below for the wrong reason.
        before = self.client.get(reverse("settings.view"))
        self.assertContains(before, revoke_url)

        response = self.client.post(revoke_url, HTTP_HX_REQUEST="true")
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, revoke_url, count=0)


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
        self.assertContains(response, reverse("external_api:pins"))

    def test_page_shows_recent_activity_only_after_the_key_is_used(self) -> None:
        # A whoami/pins path would be a false-positive match here: the usage
        # docs example above always renders those same URLs regardless of
        # this key's actual activity. Use a distinctive endpoint so the
        # assertion can only be satisfied by the real per-key activity block.
        endpoint = "/dashboard/api/external/v1/distinctive-test-endpoint/"
        api_key, _raw_key = generate_api_key(self.user, "Zapier")

        before = self.client.get(reverse("settings.view"))
        self.assertNotContains(before, "Recent activity")
        self.assertNotContains(before, endpoint)

        record_api_key_usage(api_key, endpoint)
        after = self.client.get(reverse("settings.view"))
        self.assertContains(after, "Recent activity")
        self.assertContains(after, endpoint)

    def test_page_omits_activity_block_for_a_key_with_no_usage(self) -> None:
        generate_api_key(self.user, "Unused App")
        response = self.client.get(reverse("settings.view"))
        self.assertNotContains(response, "Recent activity")
