"""Provision the first-party native app's public OAuth2 client registration.

Doing this in a migration (as well as in the ``provision_mobile_oauth_client``
management command) means every environment - dev VM, CI, production, a
self-hosted install - has the registration the shipped app expects immediately
after ``migrate``, with no manual admin step to forget.
"""

from __future__ import annotations

from django.db import migrations

from urbanlens.dashboard.oauth_clients import (
    CLIENT_TYPE_PUBLIC,
    FIRST_PARTY_CLIENT_ID,
    FIRST_PARTY_CLIENT_NAME,
    FIRST_PARTY_REDIRECT_URIS,
    GRANT_AUTHORIZATION_CODE,
)


def create_first_party_client(apps, schema_editor) -> None:
    """Create or correct the ``urbanlens-mobile`` OAuth2 application row.

    Args:
        apps: The historical app registry for this migration state.
        schema_editor: The active schema editor (unused - data-only migration).
    """
    application = apps.get_model("oauth2_provider", "Application")
    application.objects.update_or_create(
        client_id=FIRST_PARTY_CLIENT_ID,
        defaults={
            "name": FIRST_PARTY_CLIENT_NAME,
            "client_type": CLIENT_TYPE_PUBLIC,
            "authorization_grant_type": GRANT_AUTHORIZATION_CODE,
            "redirect_uris": " ".join(FIRST_PARTY_REDIRECT_URIS),
            # A public client's secret is never used; blank it so nothing
            # mistakes this registration for a confidential client.
            "client_secret": "",
            # Load-bearing, not cosmetic. ClientSecretField.pre_save hashes the
            # secret whenever this is True (its default), and hashing "" means
            # identify_hasher("") raises ValueError, the except branch runs, and
            # make_password("") is stored - a *valid* hash of the empty string.
            # A confidential-client check would then accept client_secret="" as
            # correct. Setting False stores the empty string verbatim, which no
            # secret can ever match.
            "hash_client_secret": False,
            # Not owned by any one user - this is the vendor's own app.
            "user": None,
            # First-party or not, the consent screen still names the scopes
            # being requested; skipping it would let a scope be granted with no
            # user-visible step at all.
            "skip_authorization": False,
        },
    )


class Migration(migrations.Migration):
    """Data migration registering the first-party mobile OAuth2 client."""

    dependencies = [
        ("dashboard", "0012_pin_merge_suggestion"),
        # 0012 was written while 0011 was still another session's untracked
        # work, so it depends on 0010 and left 0011 as a second leaf of the
        # graph. Both are committed now, so depending on each merges the two
        # leaves back into one - without this, migrate refuses to run at all
        # ("multiple leaf nodes in the migration graph").
        ("dashboard", "0011_sitesettings_boundary_cache_days"),
        # The migration that adds Application.hash_client_secret, which the
        # defaults above set explicitly.
        ("oauth2_provider", "0009_add_hash_client_secret"),
    ]

    operations = [
        # Irreversible on purpose: reversing would have to delete the
        # application row, cascading away every access and refresh token issued
        # against it and silently signing out every install of the app. A
        # rollback of unrelated schema is not worth that, and re-running this
        # migration forward is idempotent anyway.
        migrations.RunPython(create_first_party_client, migrations.RunPython.noop),
    ]
