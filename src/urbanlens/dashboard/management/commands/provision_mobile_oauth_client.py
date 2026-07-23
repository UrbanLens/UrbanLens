"""Provision (or update) the first-party native app's OAuth2 application.

The mobile/desktop app is a *public* OAuth2 client (RFC 8252): it cannot keep
a secret, so it authenticates with PKCE only, and its ``client_id`` is not
sensitive - it's baked into the shipped app. This command exists so every
environment (dev VM, production, a self-hosted install) can provision the
exact same registration reproducibly instead of hand-creating it in the
admin: idempotent on ``client_id``, correcting drifted fields on re-run.

Redirect URIs registered by default:

- ``urbanlens://oauth/callback`` - the app's custom scheme (Android/iOS).
  The scheme must stay in ``OAUTH2_PROVIDER["ALLOWED_REDIRECT_URI_SCHEMES"]``.
- ``http://127.0.0.1/callback`` - desktop loopback; django-oauth-toolkit
  matches loopback IPs port-insensitively per RFC 8252 §7.3, so the desktop
  app may bind any free port.
"""

from __future__ import annotations

from django.core.management.base import BaseCommand
from oauth2_provider.models import get_application_model

DEFAULT_CLIENT_ID = "urbanlens-mobile"
DEFAULT_NAME = "UrbanLens Mobile"
DEFAULT_REDIRECT_URIS = (
    "urbanlens://oauth/callback",
    "http://127.0.0.1/callback",
)


class Command(BaseCommand):
    """Create or update the native app's public OAuth2 client registration."""

    help = "Provision the first-party native app's OAuth2 application (public client, PKCE, idempotent on client_id)."

    def add_arguments(self, parser):
        parser.add_argument("--client-id", default=DEFAULT_CLIENT_ID, help=f"Stable public client id (default: {DEFAULT_CLIENT_ID}).")
        parser.add_argument("--name", default=DEFAULT_NAME, help=f"Display name (default: {DEFAULT_NAME}).")
        parser.add_argument(
            "--redirect-uri",
            action="append",
            dest="redirect_uris",
            help="Redirect URI to register (repeatable). Defaults to the app's custom scheme + desktop loopback.",
        )

    def handle(self, *args, **options):
        application_model = get_application_model()
        redirect_uris = options["redirect_uris"] or list(DEFAULT_REDIRECT_URIS)

        application, created = application_model.objects.update_or_create(
            client_id=options["client_id"],
            defaults={
                "name": options["name"],
                "client_type": application_model.CLIENT_PUBLIC,
                "authorization_grant_type": application_model.GRANT_AUTHORIZATION_CODE,
                "redirect_uris": " ".join(redirect_uris),
                # A public client's secret is never used; blank it so nothing
                # ever mistakes this registration for a confidential client.
                "client_secret": "",
                "user": None,
                "skip_authorization": False,
            },
        )

        self.stdout.write(f"{'Created' if created else 'Updated'} OAuth2 application '{application.name}'.")
        self.stdout.write(f"  client_id:     {application.client_id}")
        self.stdout.write(f"  client_type:   {application.client_type} (PKCE required globally)")
        self.stdout.write(f"  grant:         {application.authorization_grant_type}")
        self.stdout.write(f"  redirect_uris: {application.redirect_uris}")
        self.stdout.write("  endpoints:     /oauth/authorize/  /oauth/token/  /oauth/revoke_token/")
