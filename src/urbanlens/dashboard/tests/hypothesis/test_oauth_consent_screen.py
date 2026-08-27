"""Tests for the styled OAuth2 consent screen (``oauth2_provider/authorize.html``).

Before this, the page was django-oauth-toolkit's unstyled default - loading a
dead Bootstrap 2 CDN link - and it is the only user-visible gate before a
client is granted a scope like ``messages:*`` against someone's encrypted
mailbox. See ``docs/notes/mobile_app_notes.md`` Part 7.
"""

from __future__ import annotations

from django.contrib.auth.models import User
from django.urls import reverse
from model_bakery import baker

from urbanlens.core.tests.oauth import first_party_application
from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.oauth_clients import FIRST_PARTY_CLIENT_ID, FIRST_PARTY_REDIRECT_URIS


class ConsentScreenTests(TestCase):
    """The real first-party client's authorize flow renders the styled template."""

    def setUp(self) -> None:
        # The client row is created by a data migration, which a TransactionTestCase
        # elsewhere in the suite truncates - see core/tests/oauth.py.
        first_party_application()
        self.user = baker.make(User)
        self.client.force_login(self.user)
        self.authorize_url = reverse("oauth2_provider:authorize")
        self.params = {
            "response_type": "code",
            "client_id": FIRST_PARTY_CLIENT_ID,
            "redirect_uri": FIRST_PARTY_REDIRECT_URIS[0],
            "scope": "profile:read pins:read",
            "state": "xyz",
            "code_challenge": "a" * 43,
            "code_challenge_method": "S256",
        }

    def test_renders_the_app_theme_not_the_toolkit_default(self) -> None:
        """The page uses the site's own auth shell, not the bundled Bootstrap 2 template."""
        response = self.client.get(self.authorize_url, self.params)

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "UrbanLens Mobile")
        self.assertContains(response, "auth-card")
        self.assertNotContains(response, "netdna.bootstrapcdn.com")

    def test_lists_the_requested_scope_descriptions(self) -> None:
        response = self.client.get(self.authorize_url, self.params)

        self.assertContains(response, "oauth-consent-scope-list")
        self.assertContains(response, "Read your profile UUID")

    def test_allow_grants_and_redirects_to_the_app_callback(self) -> None:
        get_response = self.client.get(self.authorize_url, self.params)
        self.assertContains(get_response, 'name="allow"')

        post_body = dict(self.params)
        post_body["allow"] = "Authorize"
        response = self.client.post(self.authorize_url, post_body)

        self.assertEqual(response.status_code, 302)
        self.assertTrue(response["Location"].startswith(FIRST_PARTY_REDIRECT_URIS[0]))

    def test_cancel_redirects_with_access_denied(self) -> None:
        post_body = dict(self.params)  # no "allow" key - matches the Cancel button's submission

        response = self.client.post(self.authorize_url, post_body)

        self.assertEqual(response.status_code, 302)
        self.assertIn("error=access_denied", response["Location"])

    def test_invalid_client_renders_the_styled_error_branch(self) -> None:
        """An authorize request naming an unknown client hits the {% if error %} branch."""
        bad_params = dict(self.params)
        bad_params["client_id"] = "not-a-real-client"

        response = self.client.get(self.authorize_url, bad_params)

        self.assertContains(response, "Authorization error", status_code=400)
        self.assertContains(response, "auth-card", status_code=400)
