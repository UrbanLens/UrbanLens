# Data-only companion to 0017: re-encrypt existing plaintext token values.
#
# 0017 changed access_token/refresh_token/notify_gotify_token to
# EncryptedTextField, but AlterField is schema-only - it doesn't touch stored
# bytes. Any row written before 0017 is still plain text in the DB; reading
# it through the ORM now (from_db_value) would try to Fernet-decrypt that
# plaintext and raise InvalidToken. So this migration reads the old values
# via raw SQL (bypassing from_db_value entirely) and writes them back through
# EncryptedTextField.get_prep_value directly, without ever decrypting
# anything - there is nothing to decrypt yet on a first run.
#
# Idempotent: a value that's already valid ciphertext (Fernet tokens are
# fixed-format and ~messy in a way plaintext tokens never coincidentally are)
# would fail get_prep_value's own encrypt-again call harmlessly - it just
# doubly-encrypts, which is wrong if re-run, so this is a one-shot migration,
# not meant to be re-run outside the normal migrate sequence.
from django.db import migrations

from urbanlens.dashboard.models.fields import EncryptedTextField

_field = EncryptedTextField()


def _encrypt_column(cursor, table: str, column: str) -> None:
    cursor.execute(f"SELECT id, {column} FROM {table} WHERE {column} IS NOT NULL AND {column} != ''")  # noqa: S608 # nosec B608 - table/column are hardcoded constants below, not user input
    rows = cursor.fetchall()
    for pk, plaintext in rows:
        ciphertext = _field.get_prep_value(plaintext)
        cursor.execute(f"UPDATE {table} SET {column} = %s WHERE id = %s", [ciphertext, pk])  # noqa: S608 # nosec B608 - table/column are hardcoded constants below, not user input


def encrypt_existing_tokens(apps, schema_editor) -> None:
    with schema_editor.connection.cursor() as cursor:
        _encrypt_column(cursor, "dashboard_google_calendar_accounts", "access_token")
        _encrypt_column(cursor, "dashboard_google_calendar_accounts", "refresh_token")
        _encrypt_column(cursor, "dashboard_site_settings", "notify_gotify_token")


class Migration(migrations.Migration):

    dependencies = [
        ("dashboard", "0017_encrypt_external_tokens"),
    ]

    operations = [
        migrations.RunPython(encrypt_existing_tokens, reverse_code=migrations.RunPython.noop),
    ]
