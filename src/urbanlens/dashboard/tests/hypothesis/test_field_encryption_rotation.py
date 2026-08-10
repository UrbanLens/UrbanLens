"""Tests for ``EncryptedTextField`` key handling and ``rotate_field_encryption``.

The behaviour under test is what makes a key change survivable rather than
destructive: values are written under the active key but readable under any
retired key still listed, and the management command rewrites everything so the
retired key can finally be dropped. Regression cover for the incident class
already visible in this codebase - see the five separate ``InvalidToken``
self-heals (Immich/Flickr/Google Photos/Google Calendar/TOTP), each added after
a production 500 caused by exactly this.
"""

from __future__ import annotations

from contextlib import contextmanager
from io import StringIO
from typing import TYPE_CHECKING

from cryptography.fernet import InvalidToken
from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import SimpleTestCase, override_settings
from model_bakery import baker
import pytest

if TYPE_CHECKING:
    from collections.abc import Iterator

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.models.fields import EncryptedTextField, encryption_keys, reset_encryption_keys
from urbanlens.dashboard.models.profile.model import Profile
from urbanlens.UrbanLens.settings.app import settings as app_settings

OLD_KEY = "old-field-encryption-key-for-tests"
NEW_KEY = "new-field-encryption-key-for-tests"


@contextmanager
def using_keys(active: str | None, fallbacks: list[str] | None = None) -> Iterator[None]:
    """Run the block with a specific field-encryption key set.

    Args:
        active: Value for ``field_encryption_key``; None falls back to SECRET_KEY.
        fallbacks: Retired keys that should still decrypt.

    Yields:
        None, with the key cache reset on both entry and exit.
    """
    original_active = app_settings.field_encryption_key
    original_fallbacks = app_settings.field_encryption_key_fallbacks
    app_settings.field_encryption_key = active
    app_settings.field_encryption_key_fallbacks = fallbacks or []
    reset_encryption_keys()
    try:
        yield
    finally:
        app_settings.field_encryption_key = original_active
        app_settings.field_encryption_key_fallbacks = original_fallbacks
        reset_encryption_keys()


class EncryptionKeyOrderTests(SimpleTestCase):
    """``encryption_keys`` decides what can be read and what values are written under."""

    def test_active_key_is_first(self) -> None:
        with using_keys(NEW_KEY, [OLD_KEY]):
            self.assertEqual(encryption_keys()[0], NEW_KEY)

    def test_secret_key_is_always_a_fallback(self) -> None:
        # The property that makes setting UL_FIELD_ENCRYPTION_KEY for the first
        # time non-destructive: existing rows were encrypted under SECRET_KEY.
        with override_settings(SECRET_KEY="the-django-secret"), using_keys(NEW_KEY):  # noqa: S106 - test fixture, not a real credential
            self.assertIn("the-django-secret", encryption_keys())

    def test_keys_are_deduplicated(self) -> None:
        with override_settings(SECRET_KEY=OLD_KEY), using_keys(OLD_KEY, [OLD_KEY]):
            self.assertEqual(encryption_keys(), [OLD_KEY])


class MultiKeyDecryptionTests(SimpleTestCase):
    """A value written under one key stays readable while that key is still listed."""

    def _encrypt_under(self, key: str, plaintext: str) -> str:
        with using_keys(key):
            return EncryptedTextField().get_prep_value(plaintext)

    def _decrypt(self, field: EncryptedTextField, ciphertext: str) -> str | None:
        return field.from_db_value(ciphertext, None, None)

    def test_round_trips_under_a_single_key(self) -> None:
        with using_keys(NEW_KEY):
            field = EncryptedTextField()
            self.assertEqual(self._decrypt(field, field.get_prep_value("hello")), "hello")

    def test_old_value_still_readable_while_old_key_is_a_fallback(self) -> None:
        ciphertext = self._encrypt_under(OLD_KEY, "written under the old key")
        with using_keys(NEW_KEY, [OLD_KEY]):
            self.assertEqual(self._decrypt(EncryptedTextField(), ciphertext), "written under the old key")

    def test_old_value_unreadable_once_old_key_is_dropped(self) -> None:
        ciphertext = self._encrypt_under(OLD_KEY, "written under the old key")
        with using_keys(NEW_KEY), pytest.raises(InvalidToken):
            self._decrypt(EncryptedTextField(), ciphertext)

    def test_writes_always_use_the_active_key(self) -> None:
        with using_keys(NEW_KEY, [OLD_KEY]):
            ciphertext = EncryptedTextField().get_prep_value("fresh value")
        # Readable under the new key alone - i.e. it was not written under the fallback.
        with using_keys(NEW_KEY):
            self.assertEqual(EncryptedTextField().from_db_value(ciphertext, None, None), "fresh value")


class FailSoftTests(SimpleTestCase):
    """Undecryptable *content* degrades; undecryptable *credentials* still raise."""

    def _undecryptable(self) -> str:
        with using_keys(OLD_KEY):
            return EncryptedTextField().get_prep_value("unrecoverable")

    def test_credential_field_raises(self) -> None:
        ciphertext = self._undecryptable()
        with using_keys(NEW_KEY), pytest.raises(InvalidToken):
            EncryptedTextField().from_db_value(ciphertext, None, None)

    def test_fail_soft_field_returns_default_instead_of_raising(self) -> None:
        ciphertext = self._undecryptable()
        with using_keys(NEW_KEY):
            field = EncryptedTextField(blank=True, default="", fail_soft=True)
            with self.assertLogs("urbanlens.dashboard.models.fields", level="ERROR"):
                self.assertEqual(field.from_db_value(ciphertext, None, None), "")

    def test_fail_soft_nullable_field_returns_none(self) -> None:
        ciphertext = self._undecryptable()
        with using_keys(NEW_KEY):
            field = EncryptedTextField(null=True, blank=True, fail_soft=True)
            with self.assertLogs("urbanlens.dashboard.models.fields", level="ERROR"):
                self.assertIsNone(field.from_db_value(ciphertext, None, None))

    def test_fail_soft_survives_deconstruct(self) -> None:
        # Without this the flag would not reach migrations and would silently
        # revert to hard-fail on any regenerated field state.
        _name, _path, _args, kwargs = EncryptedTextField(fail_soft=True).deconstruct()
        self.assertTrue(kwargs["fail_soft"])
        self.assertNotIn("fail_soft", EncryptedTextField().deconstruct()[3])


class RotateFieldEncryptionCommandTests(TestCase):
    """The command rewrites stored ciphertext so a retired key can be dropped."""

    def test_rotation_makes_the_old_key_unnecessary(self) -> None:
        with using_keys(OLD_KEY):
            profile: Profile = baker.make(Profile)
            Profile.objects.filter(pk=profile.pk).update(bio="written under the old key")

        # New key active, old key retained so the existing row still reads.
        with using_keys(NEW_KEY, [OLD_KEY]):
            self.assertEqual(Profile.objects.get(pk=profile.pk).bio, "written under the old key")
            call_command("rotate_field_encryption", stdout=StringIO())

        # Old key dropped entirely - the value survives only because it was rewritten.
        with using_keys(NEW_KEY):
            self.assertEqual(Profile.objects.get(pk=profile.pk).bio, "written under the old key")

    def test_dry_run_reports_without_rewriting(self) -> None:
        with using_keys(OLD_KEY):
            profile: Profile = baker.make(Profile)
            Profile.objects.filter(pk=profile.pk).update(bio="unchanged")

        with using_keys(NEW_KEY, [OLD_KEY]):
            output = StringIO()
            call_command("rotate_field_encryption", "--dry-run", stdout=output)
            self.assertIn("Would re-encrypt", output.getvalue())

        # The real check that nothing was written: dropping the old key now
        # leaves the value unreadable, which would not be true had it rotated.
        with using_keys(NEW_KEY), self.assertLogs("urbanlens.dashboard.models.fields", level="ERROR"):
            self.assertIsNone(Profile.objects.get(pk=profile.pk).bio)

    def test_reports_rows_no_key_can_decrypt(self) -> None:
        with using_keys(OLD_KEY):
            profile: Profile = baker.make(Profile)
            Profile.objects.filter(pk=profile.pk).update(bio="orphaned")

        # Neither the active key nor any fallback can read this row.
        with using_keys(NEW_KEY), pytest.raises(CommandError, match="could not be decrypted"):
            call_command("rotate_field_encryption", stdout=StringIO(), stderr=StringIO())

    def test_rotation_is_safe_to_run_twice(self) -> None:
        with using_keys(OLD_KEY):
            profile: Profile = baker.make(Profile)
            Profile.objects.filter(pk=profile.pk).update(bio="idempotent")

        with using_keys(NEW_KEY, [OLD_KEY]):
            call_command("rotate_field_encryption", stdout=StringIO())
            call_command("rotate_field_encryption", stdout=StringIO())
            self.assertEqual(Profile.objects.get(pk=profile.pk).bio, "idempotent")
