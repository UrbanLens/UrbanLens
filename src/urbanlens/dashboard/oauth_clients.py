"""Registration constants for the first-party native app's OAuth2 client.

Deliberately model-free and import-free so a data migration can import it
directly: migration modules must not reach into ``django.conf.settings`` for
values that shape rows, and must not import real model classes at all. Keeping
the constants here (rather than in the management command that also uses them)
means the migration and the command provision byte-identical registrations.

The mobile/desktop app is a *public* OAuth2 client (RFC 8252): it cannot keep a
secret, so it authenticates with PKCE only, and its ``client_id`` is not
sensitive - it ships inside the app binary.
"""

from __future__ import annotations

#: Stable, non-secret public client id, baked into the shipped app.
FIRST_PARTY_CLIENT_ID = "urbanlens-mobile"

#: Display name shown on the OAuth consent screen.
FIRST_PARTY_CLIENT_NAME = "UrbanLens Mobile"

#: Where the authorization code is delivered:
#:
#: - ``urbanlens://oauth/callback`` - the app's custom scheme (Android/iOS).
#:   The scheme must stay in ``OAUTH2_PROVIDER["ALLOWED_REDIRECT_URI_SCHEMES"]``.
#: - ``http://127.0.0.1/callback`` - desktop loopback; django-oauth-toolkit
#:   matches loopback IPs port-insensitively per RFC 8252 section 7.3, so the
#:   desktop app may bind any free port.
FIRST_PARTY_REDIRECT_URIS = (
    "urbanlens://oauth/callback",
    "http://127.0.0.1/callback",
)

# The literal values of oauth2_provider.models.AbstractApplication.CLIENT_PUBLIC
# and .GRANT_AUTHORIZATION_CODE. Duplicated as literals because the historical
# model a migration gets from apps.get_model() carries only fields - class
# attributes like those constants are not reconstructed - so a migration cannot
# reference them through the model it is given.
CLIENT_TYPE_PUBLIC = "public"
GRANT_AUTHORIZATION_CODE = "authorization-code"
