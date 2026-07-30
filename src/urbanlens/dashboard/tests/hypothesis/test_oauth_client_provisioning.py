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
from urbanlens.dashboard.oauth_clients import FIRST_PARTY_CLIENT_ID, FIRST_PARTY_CLIENT_NAME, FIRST_PARTY_REDIRECT_URIS

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

    def test_client_secret_is_stored_blank_and_unhashed(self) -> None:
        """A public client's secret must stay literally empty.

        ``ClientSecretField.pre_save`` hashes the secret whenever
        ``hash_client_secret`` is True (its default). Hashing ``""`` means
        ``identify_hasher("")`` raises, the except branch runs, and
        ``make_password("")`` is stored - a *valid* hash of the empty string,
        which a confidential-client check would accept as a correct secret.
        """
        _provision()
        application = Application.objects.get(client_id=DEFAULT_CLIENT_ID)
        self.assertFalse(application.hash_client_secret)
        self.assertEqual(application.client_secret, "")


class FirstPartyClientMigrationTests(TestCase):
    """The 0013 data migration provisions the same registration at migrate time.

    The row these assertions read was created by the migration during test
    database setup, not by any code in this test - so a regression in the
    migration surfaces here even though the management command still works.
    """

    def test_migration_created_the_first_party_client(self) -> None:
        """A fresh database has the registration the shipped app expects."""
        application = Application.objects.get(client_id=FIRST_PARTY_CLIENT_ID)
        self.assertEqual(application.name, FIRST_PARTY_CLIENT_NAME)
        self.assertEqual(application.client_type, Application.CLIENT_PUBLIC)
        self.assertEqual(application.authorization_grant_type, Application.GRANT_AUTHORIZATION_CODE)
        self.assertIsNone(application.user)
        self.assertFalse(application.skip_authorization)

    def test_migrated_client_secret_is_blank_and_unhashed(self) -> None:
        """The migration sets hash_client_secret=False, so "" stays "".

        Without it the stored value would be ``make_password("")`` - a hash a
        presented empty secret would verify against.
        """
        application = Application.objects.get(client_id=FIRST_PARTY_CLIENT_ID)
        self.assertIs(application.hash_client_secret, False)
        self.assertEqual(application.client_secret, "")

    def test_migrated_redirect_uris_match_the_shared_constants(self) -> None:
        """Migration and management command provision byte-identical redirects."""
        application = Application.objects.get(client_id=FIRST_PARTY_CLIENT_ID)
        self.assertEqual(application.redirect_uris, " ".join(FIRST_PARTY_REDIRECT_URIS))

    def test_command_and_migration_agree_on_the_client_id(self) -> None:
        """Both provision the same registration rather than two competing rows."""
        self.assertEqual(DEFAULT_CLIENT_ID, FIRST_PARTY_CLIENT_ID)
        self.assertEqual(Application.objects.filter(client_id=FIRST_PARTY_CLIENT_ID).count(), 1)
