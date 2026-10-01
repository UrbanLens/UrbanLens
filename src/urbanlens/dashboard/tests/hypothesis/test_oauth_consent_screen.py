"""Tests for the styled OAuth2 consent screen (``oauth2_provider/authorize.html``)."""

from __future__ import annotations

from django.conf import settings
from django.contrib.auth.models import User
from django.urls import reverse
from model_bakery import baker

from urbanlens.core.tests.oauth import first_party_application
from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.oauth_clients import FIRST_PARTY_CLIENT_ID, FIRST_PARTY_REDIRECT_URIS
from urbanlens.dashboard.tests.hypothesis.test_security_headers import parse_csp


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


class ConsentScreenFormActionTests(TestCase):
    """The consent form's POST answers with a redirect to the client, which ``form-action`` governs too.

    Chrome refuses a form submission whose redirect leaves ``form-action``, so a consent page sent
    with the site's ``'self'``-only policy strands every native sign-in at the Authorize button.
    """

    def setUp(self) -> None:
        first_party_application()
        self.client.force_login(baker.make(User))
        self.params = {
            "response_type": "code",
            "client_id": FIRST_PARTY_CLIENT_ID,
            "scope": "profile:read",
            "state": "xyz",
            "code_challenge": "a" * 43,
            "code_challenge_method": "S256",
        }

    def _form_action(self, redirect_uri: str) -> list[str]:
        self.assertTrue(settings.CSP_ENFORCE, "the enforcing header is the one a browser acts on")
        response = self.client.get(reverse("oauth2_provider:authorize"), {**self.params, "redirect_uri": redirect_uri})
        self.assertEqual(response.status_code, 200)
        return parse_csp(response.headers["Content-Security-Policy"])["form-action"]

    def test_a_custom_scheme_client_is_admitted_by_scheme(self) -> None:
        self.assertIn("urbanlens:", self._form_action("urbanlens://oauth/callback"))

    def test_a_loopback_client_is_admitted_with_the_port_it_asked_for(self) -> None:
        """RFC 8252 loopback clients pick a port per sign-in; the registered URI has none."""
        self.assertIn("http://127.0.0.1:53123", self._form_action("http://127.0.0.1:53123/callback"))

    def test_the_rest_of_the_policy_is_untouched(self) -> None:
        sources = self._form_action("urbanlens://oauth/callback")

        self.assertIn("'self'", sources)
        self.assertNotIn("*", sources)

    def test_an_unregistered_redirect_is_not_admitted(self) -> None:
        response = self.client.get(
            reverse("oauth2_provider:authorize"), {**self.params, "redirect_uri": "https://evil.example/cb"}
        )

        self.assertNotIn("evil.example", response.headers.get("Content-Security-Policy", ""))

    def test_other_pages_keep_the_site_policy(self) -> None:
        self.client.get(
            reverse("oauth2_provider:authorize"), {**self.params, "redirect_uri": "urbanlens://oauth/callback"}
        )

        response = self.client.get("/health/")

        self.assertNotIn("urbanlens:", parse_csp(response.headers["Content-Security-Policy"])["form-action"])
