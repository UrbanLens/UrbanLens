from cryptography.fernet import InvalidToken
from django.conf import settings
import django.core.validators
from django.db import migrations, models
import django.db.models.constraints
import django.db.models.functions.text

import urbanlens.dashboard.models.fields
from urbanlens.dashboard.models.fields import EncryptedTextField, _fernet


_COLUMNS = [
    ("dashboard_friendinvitation", "message"),
    ("dashboard_safety_contact_defaults", "label"),
    ("dashboard_profiles", "additional_preferences"),
    ("dashboard_profiles", "exploring_with_others_preference_other"),
    ("dashboard_profiles", "friend_request_preference_other"),
    ("dashboard_profiles", "meetup_preference_other"),
    ("dashboard_profiles", "photo_sharing_preference_other"),
    ("dashboard_profiles", "photo_tagging_preference_other"),
    ("dashboard_profiles", "photo_taking_preference_other"),
    ("dashboard_profiles", "photo_usage_preference_other"),
]

_field = EncryptedTextField()


def _0048__decrypt_column(cursor, table: str, column: str) -> None:
    """Decrypt every Fernet-encrypted value in ``table.column`` in place.

    Mirrors ``_decrypt_column`` in 0039. A value not shaped like a Fernet token
    (they always begin ``gAAAA`` - a 0x80 version byte, base64'd) is left
    untouched: it was written before the forward pass ran, or the forward pass
    never reached it, and re-processing it would corrupt real plaintext. A
    token-shaped value that no configured key can decrypt raises, aborting - and
    rolling back - the reverse rather than writing garbage into a column the
    pre-0048 code reads as plaintext.
    """
    cursor.execute(f"SELECT id, {column} FROM {table} WHERE {column} LIKE 'gAAAA%%'")
    for pk, ciphertext in cursor.fetchall():
        plaintext = _field.from_db_value(ciphertext, None, None)
        cursor.execute(
            f"UPDATE {table} SET {column} = %s WHERE id = %s", [plaintext, pk]
        )


def _0048_decrypt_existing_preference_fields(apps, schema_editor) -> None:
    """Real reverse for the in-place encryption above.

    This was ``RunPython.noop`` until 2026-08-19, on the reasoning that a
    reverse would have to decrypt under whatever key is active at rollback time
    and getting that wrong writes garbage. But noop is the *worse* half of that
    trade: ``migrate dashboard 0047`` then **succeeds** while leaving ciphertext
    in columns the pre-0048 code reads as plaintext - silent corruption that
    reports success, rather than a failure anyone can act on.

    The project settled the question in ``docs/DATA_ENCRYPTION.md`` ("Migration
    rollbacks decrypt", 2026-08-15) two days before this migration landed, and
    0007 and 0039 already implement it: decrypt properly, and abort the whole
    rollback if any value cannot be decrypted under the configured keys. 0048
    was the one file contradicting a written rule.
    """
    with schema_editor.connection.cursor() as cursor:
        for table, column in _COLUMNS:
            _0048__decrypt_column(cursor, table, column)


def _0048__encrypt_column(cursor, table: str, column: str) -> None:
    """Encrypt every existing non-empty value in ``table.column`` in place.

    Mirrors ``_encrypt_column`` in 0039 (and 0007 before it). Uses a raw cursor
    rather than the ORM so it is unaffected by the historical model's field
    type, and therefore by operation order relative to the ``AlterField``s.
    Without this pass, every pre-existing row would raise ``InvalidToken`` on
    its first read after deploy - the retrofit hazard documented in
    docs/DATA_ENCRYPTION.md.
    """
    cursor.execute(
        f"SELECT id, {column} FROM {table} WHERE {column} IS NOT NULL AND {column} != ''"
    )
    for pk, stored in cursor.fetchall():
        if _already_encrypted(stored):
            continue
        cursor.execute(
            f"UPDATE {table} SET {column} = %s WHERE id = %s",
            [_field.get_prep_value(stored), pk],
        )


def _already_encrypted(value: str) -> bool:
    """True when ``value`` is already ciphertext this install can read.

    Makes the pass idempotent. The 0007/0039 versions of this helper were not:
    running one of them a second time encrypts the ciphertext again, and the
    ORM's single decrypt then yields the inner ciphertext instead of the
    plaintext - silent, permanent-looking corruption. Django applies a migration
    once, so that was latent rather than live, but a squash replay or a manual
    re-run of the function is enough to trigger it, and skipping already-readable
    values costs one decrypt attempt per row.

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


def _0048_encrypt_existing_preference_fields(apps, schema_editor) -> None:
    """Encrypt the pre-existing plaintext values for every column in ``_COLUMNS``."""
    with schema_editor.connection.cursor() as cursor:
        for table, column in _COLUMNS:
            _0048__encrypt_column(cursor, table, column)


class Migration(migrations.Migration):
    dependencies = [
        ("dashboard", "0030_v0_7_0"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]
    operations = [
        migrations.AddIndex(
            model_name="image",
            index=models.Index(
                fields=["profile", "quota_exempt_reason"],
                name="idxdb_image_profile_quota",
            ),
        ),
        migrations.AddIndex(
            model_name="floorplan",
            index=models.Index(
                fields=["place", "profile", "valid_from"],
                name="idx_floorplan_place_owner_date",
            ),
        ),
        migrations.AddIndex(
            model_name="floorplanmarker",
            index=models.Index(
                fields=["connector_id"], name="idx_floorplan_marker_connector"
            ),
        ),
        migrations.AddIndex(
            model_name="floorplanwall",
            index=models.Index(
                fields=["floor", "kind"], name="idx_floorplan_wall_kind"
            ),
        ),
        migrations.AddConstraint(
            model_name="label",
            constraint=models.UniqueConstraint(
                django.db.models.functions.text.Lower("name"),
                models.F("profile"),
                models.F("kind"),
                name="uq_label_profile_name_kind_ci",
                nulls_distinct=False,
            ),
        ),
        migrations.AddConstraint(
            model_name="tripcalendarlink",
            constraint=models.UniqueConstraint(
                condition=models.Q(("google_event_id", ""), _negated=True),
                fields=("profile", "google_event_id"),
                name="db_tcl_profile_event_unique",
            ),
        ),
        migrations.AddConstraint(
            model_name="pinlink",
            constraint=models.UniqueConstraint(
                models.F("pin"),
                django.db.models.functions.text.MD5("url"),
                name="db_plink_pin_url_unique",
            ),
        ),
        migrations.AddConstraint(
            model_name="wikilink",
            constraint=models.UniqueConstraint(
                models.F("wiki"),
                django.db.models.functions.text.MD5("url"),
                name="db_wlink_wiki_url_unique",
            ),
        ),
        migrations.AddConstraint(
            model_name="floorplanfloor",
            constraint=models.UniqueConstraint(
                deferrable=django.db.models.constraints.Deferrable["DEFERRED"],
                fields=("floorplan", "level"),
                name="floorplan_floor_unique_level",
            ),
        ),
        migrations.AlterField(
            model_name="friendinvitation",
            name="message",
            field=urbanlens.dashboard.models.fields.EncryptedTextField(
                blank=True,
                fail_soft=True,
                max_length=1000,
                null=True,
                validators=[django.core.validators.MaxLengthValidator(1000)],
            ),
        ),
        migrations.AlterField(
            model_name="emergencycontactdefault",
            name="label",
            field=urbanlens.dashboard.models.fields.EncryptedTextField(
                blank=True,
                default="",
                fail_soft=True,
                max_length=150,
                validators=[django.core.validators.MaxLengthValidator(150)],
            ),
        ),
        migrations.AlterField(
            model_name="profile",
            name="additional_preferences",
            field=urbanlens.dashboard.models.fields.EncryptedTextField(
                blank=True,
                default="",
                fail_soft=True,
                help_text="Anything else you'd like other users to know about interacting with you.",
                max_length=1000,
                validators=[django.core.validators.MaxLengthValidator(1000)],
            ),
        ),
        migrations.AlterField(
            model_name="profile",
            name="exploring_with_others_preference_other",
            field=urbanlens.dashboard.models.fields.EncryptedTextField(
                blank=True,
                default="",
                fail_soft=True,
                max_length=255,
                validators=[django.core.validators.MaxLengthValidator(255)],
            ),
        ),
        migrations.AlterField(
            model_name="profile",
            name="friend_request_preference_other",
            field=urbanlens.dashboard.models.fields.EncryptedTextField(
                blank=True,
                default="",
                fail_soft=True,
                max_length=255,
                validators=[django.core.validators.MaxLengthValidator(255)],
            ),
        ),
        migrations.AlterField(
            model_name="profile",
            name="meetup_preference_other",
            field=urbanlens.dashboard.models.fields.EncryptedTextField(
                blank=True,
                default="",
                fail_soft=True,
                max_length=255,
                validators=[django.core.validators.MaxLengthValidator(255)],
            ),
        ),
        migrations.AlterField(
            model_name="profile",
            name="photo_sharing_preference_other",
            field=urbanlens.dashboard.models.fields.EncryptedTextField(
                blank=True,
                default="",
                fail_soft=True,
                max_length=255,
                validators=[django.core.validators.MaxLengthValidator(255)],
            ),
        ),
        migrations.AlterField(
            model_name="profile",
            name="photo_tagging_preference_other",
            field=urbanlens.dashboard.models.fields.EncryptedTextField(
                blank=True,
                default="",
                fail_soft=True,
                max_length=255,
                validators=[django.core.validators.MaxLengthValidator(255)],
            ),
        ),
        migrations.AlterField(
            model_name="profile",
            name="photo_taking_preference_other",
            field=urbanlens.dashboard.models.fields.EncryptedTextField(
                blank=True,
                default="",
                fail_soft=True,
                max_length=255,
                validators=[django.core.validators.MaxLengthValidator(255)],
            ),
        ),
        migrations.AlterField(
            model_name="profile",
            name="photo_usage_preference_other",
            field=urbanlens.dashboard.models.fields.EncryptedTextField(
                blank=True,
                default="",
                fail_soft=True,
                max_length=255,
                validators=[django.core.validators.MaxLengthValidator(255)],
            ),
        ),
        migrations.RunPython(
            code=_0048_encrypt_existing_preference_fields,
            reverse_code=_0048_decrypt_existing_preference_fields,
        ),
    ]
