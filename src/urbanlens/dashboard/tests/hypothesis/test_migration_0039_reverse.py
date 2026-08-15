"""Migration 0039's reverse really decrypts - a noop reverse was silent corruption.

``migrate dashboard 0038`` with the old ``RunPython.noop`` reverse succeeded
while leaving ciphertext in columns the pre-0039 code reads as plaintext.
The real reverse depends on two properties this module pins:

- Fernet ciphertext always begins ``gAAAA`` (version byte 0x80, base64'd) -
  the discriminator that lets the reverse skip rows that were still plaintext.
- ``get_prep_value``/``from_db_value`` round-trip exactly, so decrypt-in-place
  restores the original bytes.
"""

from __future__ import annotations

import importlib
from string import printable

from hypothesis import given, strategies as st

from urbanlens.core.tests.testcase import SimpleTestCase
from urbanlens.dashboard.models.fields import EncryptedTextField

_migration = importlib.import_module("urbanlens.dashboard.migrations.0039_encrypt_contact_and_note_fields")


class Migration0039ReverseTests(SimpleTestCase):
    @given(st.text(alphabet=printable, min_size=1, max_size=200))
    def test_encrypt_decrypt_round_trips_and_ciphertext_is_discriminable(self, plaintext: str) -> None:
        field = EncryptedTextField()
        ciphertext = field.get_prep_value(plaintext)
        self.assertTrue(ciphertext.startswith("gAAAA"), "the reverse's LIKE 'gAAAA%' discriminator would miss this token")
        self.assertEqual(field.from_db_value(ciphertext, None, None), plaintext)

    def test_the_migration_wires_the_real_reverse(self) -> None:
        run_python_ops = [op for op in _migration.Migration.operations if type(op).__name__ == "RunPython"]
        self.assertEqual(len(run_python_ops), 1)
        self.assertIs(run_python_ops[0].reverse_code, _migration.decrypt_existing_contact_and_note_fields)

    def test_forward_and_reverse_cover_the_same_columns(self) -> None:
        """The shared _ENCRYPTED_COLUMNS constant is what makes drift impossible - pin its size against the AlterFields."""
        alter_fields = [op for op in _migration.Migration.operations if type(op).__name__ == "AlterField"]
        self.assertEqual(len(_migration._ENCRYPTED_COLUMNS), len(alter_fields))

    def test_migration_0007_wires_its_token_decrypt_reverse_too(self) -> None:
        """0007 encrypts credential tokens with the same in-place pattern; its rollback must decrypt, not noop."""
        migration_0007 = importlib.import_module("urbanlens.dashboard.migrations.0007_pinshare_bundled_with_markup_map_removed_flags")
        token_ops = [op for op in migration_0007.Migration.operations if type(op).__name__ == "RunPython" and op.code is migration_0007.encrypt_existing_tokens]
        self.assertEqual(len(token_ops), 1)
        self.assertIs(token_ops[0].reverse_code, migration_0007.decrypt_existing_tokens)
