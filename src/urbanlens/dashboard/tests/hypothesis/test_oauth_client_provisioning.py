"""Tests for the first-party native app's OAuth2 client provisioning command.

The command must be idempotent (every environment runs it repeatedly), the
registration must be a public+PKCE client, and the registered redirect URIs
must actually validate the way the app's platforms need: exact match for the
custom scheme, port-insensitive loopback (RFC 8252) for desktop.
"""

from __future__ import annotations

from io import StringIO

from django.core.management import call_command
from oauth2_provider.models import get_application_model

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.management.commands.provision_mobile_oauth_client import DEFAULT_CLIENT_ID

Application = get_application_model()


def _provision(*args: str) -> str:
    out = StringIO()
    call_command("provision_mobile_oauth_client", *args, stdout=out)
    return out.getvalue()


class ProvisionMobileOauthClientTests(TestCase):
    """provision_mobile_oauth_client creates the public-client registration."""

    def test_creates_a_public_pkce_client_with_default_redirects(self) -> None:
        output = _provision()
        application = Application.objects.get(client_id=DEFAULT_CLIENT_ID)
        self.assertEqual(application.client_type, Application.CLIENT_PUBLIC)
        self.assertEqual(application.authorization_grant_type, Application.GRANT_AUTHORIZATION_CODE)
        self.assertIn("urbanlens://oauth/callback", application.redirect_uris)
        self.assertIn("http://127.0.0.1/callback", application.redirect_uris)
        self.assertFalse(application.skip_authorization)
        self.assertIn(DEFAULT_CLIENT_ID, output)

    def test_rerunning_updates_in_place_instead_of_duplicating(self) -> None:
        _provision()
        Application.objects.filter(client_id=DEFAULT_CLIENT_ID).update(redirect_uris="https://drifted.example.com/cb")
        _provision()
        self.assertEqual(Application.objects.filter(client_id=DEFAULT_CLIENT_ID).count(), 1)
        application = Application.objects.get(client_id=DEFAULT_CLIENT_ID)
        self.assertIn("urbanlens://oauth/callback", application.redirect_uris)
        self.assertNotIn("drifted.example.com", application.redirect_uris)

    def test_custom_scheme_redirect_is_allowed(self) -> None:
        _provision()
        application = Application.objects.get(client_id=DEFAULT_CLIENT_ID)
        self.assertTrue(application.redirect_uri_allowed("urbanlens://oauth/callback"))

    def test_desktop_loopback_redirect_matches_any_port(self) -> None:
        """RFC 8252 §7.3: the desktop app binds a random free port at auth time."""
        _provision()
        application = Application.objects.get(client_id=DEFAULT_CLIENT_ID)
        self.assertTrue(application.redirect_uri_allowed("http://127.0.0.1:53123/callback"))

    def test_unregistered_redirect_is_rejected(self) -> None:
        _provision()
        application = Application.objects.get(client_id=DEFAULT_CLIENT_ID)
        self.assertFalse(application.redirect_uri_allowed("https://evil.example.com/callback"))
        self.assertFalse(application.redirect_uri_allowed("urbanlens://other/callback"))

    def test_custom_redirect_uri_option_replaces_defaults(self) -> None:
        _provision("--redirect-uri", "urbanlens://alt/callback")
        application = Application.objects.get(client_id=DEFAULT_CLIENT_ID)
        self.assertEqual(application.redirect_uris, "urbanlens://alt/callback")
