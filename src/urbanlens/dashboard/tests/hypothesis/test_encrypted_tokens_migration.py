"""Tests for the GoogleCalendarAccount/SiteSettings token-encryption migration.

Migration 0017 changes access_token/refresh_token/notify_gotify_token to
EncryptedTextField; migration 0018 is the companion data migration that
re-encrypts rows written before 0017 (raw SQL still holds plaintext at that
point - AlterField never touches stored bytes). These tests exercise
``encrypt_existing_tokens`` directly against rows seeded with raw SQL
(simulating pre-migration data), verifying the ORM can read the original
plaintext back afterwards.

The migration module's name starts with a digit (``0018_...``), so it can't
be imported with a normal ``from ... import`` statement - ``importlib`` is
used instead, the same mechanism Django's own migration executor relies on.
"""

from __future__ import annotations

from importlib import import_module

from django.db import connection
from model_bakery import baker

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.models.calendar_sync.model import GoogleCalendarAccount
from urbanlens.dashboard.models.site_settings.model import SiteSettings

# The original 0018_encrypt_external_tokens_data module was folded into the
# v0.4.0+ squash - encrypt_existing_tokens now lives in the squashed module.
_migration = import_module("urbanlens.dashboard.migrations.0007_pinshare_bundled_with_squashed_0037_markup_map_removed_flags")
encrypt_existing_tokens = _migration.encrypt_existing_tokens


class _FakeSchemaEditor:
    """Minimal stand-in exposing the one attribute encrypt_existing_tokens needs."""

    connection = connection


class EncryptExistingTokensTests(TestCase):
    """encrypt_existing_tokens re-encrypts plaintext rows in place, once."""

    def test_calendar_account_tokens_are_encrypted_and_readable(self) -> None:
        account = baker.make(GoogleCalendarAccount)
        with connection.cursor() as cursor:
            cursor.execute(
                "UPDATE dashboard_google_calendar_accounts SET access_token = %s, refresh_token = %s WHERE id = %s",
                ["plain-access", "plain-refresh", account.pk],
            )

        encrypt_existing_tokens(apps=None, schema_editor=_FakeSchemaEditor())

        account.refresh_from_db()
        self.assertEqual(account.access_token, "plain-access")
        self.assertEqual(account.refresh_token, "plain-refresh")

        with connection.cursor() as cursor:
            cursor.execute("SELECT access_token FROM dashboard_google_calendar_accounts WHERE id = %s", [account.pk])
            (raw,) = cursor.fetchone()
        self.assertNotEqual(raw, "plain-access")

    def test_site_settings_gotify_token_is_encrypted_and_readable(self) -> None:
        settings_row = SiteSettings.get_current()
        with connection.cursor() as cursor:
            cursor.execute("UPDATE dashboard_site_settings SET notify_gotify_token = %s WHERE id = %s", ["plain-gotify", settings_row.pk])

        encrypt_existing_tokens(apps=None, schema_editor=_FakeSchemaEditor())

        settings_row.refresh_from_db()
        self.assertEqual(settings_row.notify_gotify_token, "plain-gotify")

    def test_blank_tokens_are_left_alone(self) -> None:
        account = baker.make(GoogleCalendarAccount)
        with connection.cursor() as cursor:
            cursor.execute("UPDATE dashboard_google_calendar_accounts SET refresh_token = '' WHERE id = %s", [account.pk])

        encrypt_existing_tokens(apps=None, schema_editor=_FakeSchemaEditor())

        account.refresh_from_db()
        self.assertEqual(account.refresh_token, "")
