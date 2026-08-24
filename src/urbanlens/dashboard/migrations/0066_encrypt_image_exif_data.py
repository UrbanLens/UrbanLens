"""Encrypt ``Image.exif_data``, and move the column from ``jsonb`` to ``text``.

Different from the fields encrypted in 0007/0039/0048: those are user-authored
content with a plaintext original the user could retype. This column is now the
*only* copy. The upload pipeline strips the EXIF block out of the stored file
(``services.media.images.downscale_stored_image``), so what the photo used to
carry - camera make, model and serial, and the coordinates unless the uploader
opted out of location - lives here and nowhere else. That is what makes it worth
encrypting and what makes ``fail_soft=True`` necessary: an ``Image`` row loads on
every gallery page, so a key mismatch has to degrade this one field rather than
break the site, and the row must stay recoverable.

The column type changes because ciphertext is opaque. ``jsonb`` would keep
advertising indexing and containment queries that encryption has already taken
away; nothing filters on this column's contents, and after this it cannot.

The forward pass encrypts with a plain ``EncryptedTextField``, not the new
``EncryptedJSONField``: after the ``AlterField`` the column already holds JSON
*text*, and ``EncryptedJSONField.get_prep_value`` would ``json.dumps`` it a
second time, storing a JSON string containing JSON. Reads still come back
correctly through the JSON field, which decrypts to that text and parses it.
"""

from cryptography.fernet import InvalidToken
from django.db import migrations

import urbanlens.dashboard.models.fields
from urbanlens.dashboard.models.fields import EncryptedTextField, _fernet

#: Encrypts/decrypts the raw JSON *text* the column holds after the AlterField.
#: Deliberately not fail_soft: a reverse that cannot decrypt must abort rather
#: than write garbage into a column the pre-0066 code parses as JSON.
_field = EncryptedTextField()

_TABLE = "dashboard_images"
_COLUMN = "exif_data"


def _already_encrypted(value: str) -> bool:
    """True when ``value`` is already ciphertext this install can read.

    Keeps the pass idempotent, as in 0048 - re-encrypting ciphertext yields a
    value whose single decrypt returns the inner ciphertext, which looks like
    permanent corruption.

    Args:
        value: The raw stored column value.

    Returns:
        True when the value decrypts under some configured key.
    """
    try:
        _fernet().decrypt(value.encode())
    except (InvalidToken, UnicodeEncodeError):
        return False
    return True


def encrypt_existing_exif_data(apps, schema_editor) -> None:
    """Encrypt every pre-existing EXIF snapshot in place.

    Raw cursor rather than the ORM, as in 0039/0048, so the pass is unaffected
    by the historical model's field type and by its order relative to the
    ``AlterField``. Without it every pre-existing row would read as an
    undecryptable value on first load after deploy.
    """
    with schema_editor.connection.cursor() as cursor:
        cursor.execute(f"SELECT id, {_COLUMN} FROM {_TABLE} WHERE {_COLUMN} IS NOT NULL AND {_COLUMN} != ''")  # noqa: S608 # nosec B608 - identifiers are module constants, not user input
        for pk, stored in cursor.fetchall():
            if _already_encrypted(stored):
                continue
            cursor.execute(f"UPDATE {_TABLE} SET {_COLUMN} = %s WHERE id = %s", [_field.get_prep_value(stored), pk])  # noqa: S608 # nosec B608 - identifiers are module constants, not user input


def decrypt_existing_exif_data(apps, schema_editor) -> None:
    """Real reverse: decrypt back to plaintext JSON text before the column returns to jsonb.

    Per "Migration rollbacks decrypt" in docs/DATA_ENCRYPTION.md. A value not
    shaped like a Fernet token (they begin ``gAAAA``) is left alone - it was
    written before the forward pass, and re-processing it would corrupt real
    plaintext. A token-shaped value no key can decrypt raises, aborting and
    rolling back the reverse rather than leaving something the pre-0066 code
    would try to parse as JSON.

    Runs before the ``AlterField`` is undone, because Django reverses operations
    in reverse order - so the column is still ``text`` here.
    """
    with schema_editor.connection.cursor() as cursor:
        cursor.execute(f"SELECT id, {_COLUMN} FROM {_TABLE} WHERE {_COLUMN} LIKE 'gAAAA%%'")  # noqa: S608 # nosec B608 - identifiers are module constants, not user input
        for pk, ciphertext in cursor.fetchall():
            cursor.execute(f"UPDATE {_TABLE} SET {_COLUMN} = %s WHERE id = %s", [_field.from_db_value(ciphertext, None, None), pk])  # noqa: S608 # nosec B608 - identifiers are module constants, not user input


class Migration(migrations.Migration):

    dependencies = [
        ("dashboard", "0065_image_attachments_and_source_urls"),
    ]

    operations = [
        migrations.AlterField(
            model_name="image",
            name="exif_data",
            field=urbanlens.dashboard.models.fields.EncryptedJSONField(blank=True, fail_soft=True, null=True),
        ),
        migrations.RunPython(encrypt_existing_exif_data, decrypt_existing_exif_data),
    ]
