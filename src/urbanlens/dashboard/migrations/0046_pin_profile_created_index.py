from django.db import migrations, models

#: Built outside a transaction so a large pin table is not locked against writes. Dropped first, which also removes the
#: invalid index a cancelled concurrent build leaves under its name.
_NAME = "idxdb_pin_pfile_created"


def _build(apps, schema_editor):
    quote = schema_editor.quote_name
    table = apps.get_model("dashboard", "pin")._meta.db_table
    schema_editor.execute(f"DROP INDEX CONCURRENTLY IF EXISTS {quote(_NAME)}")
    schema_editor.execute(f"CREATE INDEX CONCURRENTLY {quote(_NAME)} ON {quote(table)} ({quote('profile_id')}, {quote('created')})")


def _drop(apps, schema_editor):
    schema_editor.execute(f"DROP INDEX CONCURRENTLY IF EXISTS {schema_editor.quote_name(_NAME)}")


class Migration(migrations.Migration):
    atomic = False

    dependencies = [
        ("dashboard", "0045_group_key_envelope_outlives_profile"),
    ]

    operations = [
        migrations.SeparateDatabaseAndState(
            state_operations=[migrations.AddIndex(model_name="pin", index=models.Index(fields=["profile", "created"], name=_NAME))],
            database_operations=[migrations.RunPython(_build, _drop)],
        ),
    ]
