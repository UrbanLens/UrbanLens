"""Test helper for the first-party OAuth2 client registration.

The ``urbanlens-mobile`` ``Application`` row is created by a data migration
(``dashboard/migrations/0010_v0_6_0.py``). Reading it back with a bare
``Application.objects.get(...)`` couples a test to migration-created data, and
Django only guarantees that data for ``TestCase`` - a ``TransactionTestCase``
truncates every table on teardown and restores migration data only when
``serialized_rollback`` is set, which nothing here sets.

The suite has ~31 ``TransactionTestCase``/``transaction=True`` tests, so the row
is destroyed the first time one of them runs. With ``--reuse-db`` (what
``CLAUDE.md`` recommends for iterating) it never comes back, and every later run
against that database fails with ``Application.DoesNotExist`` - a message that
reads like a product bug rather than a poisoned fixture.

Going through here instead makes each test provide what it needs.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from oauth2_provider.models import get_application_model

from urbanlens.dashboard.oauth_clients import (
    CLIENT_TYPE_PUBLIC,
    FIRST_PARTY_CLIENT_ID,
    FIRST_PARTY_CLIENT_NAME,
    FIRST_PARTY_REDIRECT_URIS,
    GRANT_AUTHORIZATION_CODE,
)

if TYPE_CHECKING:
    from django.db.models import Model


def first_party_application() -> Model:
    """Return the first-party OAuth2 ``Application``, creating it if absent.

    Field-for-field identical to what the data migration writes, including
    ``hash_client_secret=False`` - see that migration for why blanking the
    secret without it would store a *valid* hash of the empty string.

    Returns:
        The ``urbanlens-mobile`` Application row.
    """
    application, _ = get_application_model().objects.get_or_create(
        client_id=FIRST_PARTY_CLIENT_ID,
        defaults={
            "name": FIRST_PARTY_CLIENT_NAME,
            "client_type": CLIENT_TYPE_PUBLIC,
            "authorization_grant_type": GRANT_AUTHORIZATION_CODE,
            "redirect_uris": " ".join(FIRST_PARTY_REDIRECT_URIS),
            "client_secret": "",
            "hash_client_secret": False,
            "user": None,
            "skip_authorization": False,
        },
    )
    return application
