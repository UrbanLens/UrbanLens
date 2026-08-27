"""The data pass in migration 0066, exercised against rows that predate it.

A fresh test database has no pre-0066 rows, so migrating it proves the schema
change applies and nothing else. What actually matters in production is the
backfill: every existing snapshot is plaintext JSON text the moment the column
becomes ``text``, and if the pass misses one, that row reads as undecryptable
forever after.

The functions are called directly rather than through ``migrate``, so the state
before and after each pass can be inspected - and so the idempotency guard and
the reverse are covered, both of which are silent-corruption risks rather than
loud ones.
"""

from __future__ import annotations

import importlib
import json
from types import SimpleNamespace

from django.db import connection
from model_bakery import baker

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.models.images.model import Image

#: A migration module name starts with a digit, so it is not a legal identifier
#: and cannot be reached with an ordinary import statement.
_0066_module = importlib.import_module("urbanlens.dashboard.migrations.0030_v0_7_0")

_PLAINTEXT = {"Make": "ACME Cameras", "GPSInfo": {"1": "N"}}


def _schema_editor() -> SimpleNamespace:
    """The only attribute the migration functions touch."""
    return SimpleNamespace(connection=connection)


def _write_raw(pk: int, value: str | None) -> None:
    with connection.cursor() as cursor:
        cursor.execute("UPDATE dashboard_images SET exif_data = %s WHERE id = %s", [value, pk])


def _read_raw(pk: int) -> str | None:
    with connection.cursor() as cursor:
        cursor.execute("SELECT exif_data FROM dashboard_images WHERE id = %s", [pk])
        return cursor.fetchone()[0]


class EncryptExistingExifDataTests(TestCase):
    def setUp(self) -> None:
        self.image = baker.make(Image, image=None, exif_data=None)
        # Exactly what the AlterField leaves behind for a pre-existing row.
        _write_raw(self.image.pk, json.dumps(_PLAINTEXT))

    def test_a_plaintext_row_is_encrypted(self) -> None:
        _0066_module._0066_encrypt_existing_exif_data(None, _schema_editor())

        stored = _read_raw(self.image.pk)
        self.assertTrue(stored.startswith("gAAAA"), "the pre-existing row was left in plaintext")
        self.assertNotIn("ACME Cameras", stored)

    def test_the_row_reads_back_through_the_field(self) -> None:
        """The point of the pass: no row reads as undecryptable after deploy."""
        _0066_module._0066_encrypt_existing_exif_data(None, _schema_editor())

        self.image.refresh_from_db()
        self.assertEqual(self.image.exif_data, _PLAINTEXT)

    def test_running_it_twice_does_not_double_encrypt(self) -> None:
        """Re-encrypting ciphertext decrypts to ciphertext - corruption that looks permanent."""
        _0066_module._0066_encrypt_existing_exif_data(None, _schema_editor())
        after_once = _read_raw(self.image.pk)

        _0066_module._0066_encrypt_existing_exif_data(None, _schema_editor())

        self.assertEqual(_read_raw(self.image.pk), after_once)
        self.image.refresh_from_db()
        self.assertEqual(self.image.exif_data, _PLAINTEXT)

    def test_a_null_row_is_left_alone(self) -> None:
        other = baker.make(Image, image=None, exif_data=None)

        _0066_module._0066_encrypt_existing_exif_data(None, _schema_editor())

        self.assertIsNone(_read_raw(other.pk))

    def test_the_reverse_puts_the_plaintext_back(self) -> None:
        """A rollback must leave text the pre-0066 code can parse as JSON."""
        _0066_module._0066_encrypt_existing_exif_data(None, _schema_editor())

        _0066_module._0066_decrypt_existing_exif_data(None, _schema_editor())

        self.assertEqual(json.loads(_read_raw(self.image.pk)), _PLAINTEXT)

    def test_the_reverse_leaves_untokenised_values_untouched(self) -> None:
        """A row written after a partial forward pass is real plaintext - not ours to touch."""
        _write_raw(self.image.pk, json.dumps({"Make": "written later"}))

        _0066_module._0066_decrypt_existing_exif_data(None, _schema_editor())

        self.assertEqual(json.loads(_read_raw(self.image.pk)), {"Make": "written later"})
