"""Tests for the Settings > Security > API Keys management UI.

Mirrors the shape of passkey rename/delete and TOTP action tests in spirit:
creation must reveal the plaintext exactly once, revocation must be scoped to
the requesting user and take effect immediately (the external API can no
longer authenticate with it), and both actions must never touch another
user's keys.
"""

from __future__ import annotations

import re

from django.contrib.auth.models import User
from django.urls import reverse
from model_bakery import baker

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.models.account.model import ApiKey
from urbanlens.dashboard.services.auth.api_keys import (
    API_KEYS_PAGE_SIZE,
    authenticate_api_key,
    generate_api_key,
    record_api_key_usage,
    revoke_api_key,
)


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


class ApiKeyListPaginationTests(TestCase):
    """P69: the key list grows forever, because revoking never removes a row.

    Revoked keys are shown on purpose - ``revoke_all_api_keys``' docstring says
    so, and an owner needs to see that a key went away - which is exactly why
    the list cannot be trimmed and has to page instead.
    """

    def setUp(self) -> None:
        baker.make(User)
        self.user = baker.make(User)
        self.client.force_login(self.user)

    def _keys(self, count: int, *, revoked: bool = False) -> list[ApiKey]:
        made = []
        for index in range(count):
            api_key, _raw = generate_api_key(self.user, f"key-{index:03d}")
            if revoked:
                revoke_api_key(self.user, api_key.pk)
            made.append(api_key)
        return made

    def _listed(self, response) -> list[str]:
        """The key names this response actually rendered, newest first.

        By name rather than by counting list items: the API-key list reuses the
        passkey list's classes, so a count would also pick up the Security
        section's passkeys on a full settings page.
        """
        return re.findall(r"<strong>(key-\d{3}|the-one-that-still-works)</strong>", response.content.decode())

    def test_the_settings_page_renders_only_one_page_of_keys(self) -> None:
        self._keys(API_KEYS_PAGE_SIZE + 4)

        response = self.client.get(reverse("settings.view"))

        self.assertEqual(self._listed(response), [f"key-{index:03d}" for index in range(API_KEYS_PAGE_SIZE + 3, 3, -1)])

    def test_the_rest_are_reachable_on_the_next_page(self) -> None:
        self._keys(API_KEYS_PAGE_SIZE + 4)

        response = self.client.get(reverse("settings.security.api_keys.section"), {"api_keys_page": 2})

        self.assertEqual(response.status_code, 200)
        self.assertEqual(self._listed(response), [f"key-{index:03d}" for index in range(3, -1, -1)])

    def test_pagination_links_target_the_section_and_its_own_parameter(self) -> None:
        # The partial is rendered by the whole settings page as well as by its
        # own view, so a link built from request.path would point at
        # /dashboard/settings/. `page` is avoided because the settings page
        # carries other paginated sections.
        self._keys(API_KEYS_PAGE_SIZE + 4)

        response = self.client.get(reverse("settings.view"))

        self.assertContains(response, f"{reverse('settings.security.api_keys.section')}?api_keys_page=2")

    def test_active_keys_come_before_revoked_ones(self) -> None:
        # Created oldest-first, so newest-first ordering alone would push the
        # only working key onto page two behind a page of dead ones - which is
        # the one key its owner came to the page to manage.
        still_working, _raw = generate_api_key(self.user, "the-one-that-still-works")
        self._keys(API_KEYS_PAGE_SIZE, revoked=True)

        response = self.client.get(reverse("settings.view"))

        self.assertEqual(self._listed(response)[0], still_working.name)

    def test_revoked_keys_sink_but_stay_newest_first_among_themselves(self) -> None:
        # Ordering on revoked_at itself would sort the dead keys by when they
        # were revoked, oldest first - the least interesting one at the top.
        self._keys(2)
        revoked_first, _one = generate_api_key(self.user, "key-100")
        revoked_second, _two = generate_api_key(self.user, "key-101")
        revoke_api_key(self.user, revoked_second.pk)
        revoke_api_key(self.user, revoked_first.pk)

        response = self.client.get(reverse("settings.security.api_keys.section"))

        self.assertEqual(self._listed(response), ["key-001", "key-000", "key-101", "key-100"])

    def test_another_users_keys_are_never_listed(self) -> None:
        stranger = baker.make(User)
        generate_api_key(stranger, "not yours")
        self._keys(2)

        response = self.client.get(reverse("settings.security.api_keys.section"))

        self.assertNotContains(response, "not yours")

    def test_the_section_view_requires_login(self) -> None:
        self.client.logout()

        response = self.client.get(reverse("settings.security.api_keys.section"))

        self.assertEqual(response.status_code, 302)
