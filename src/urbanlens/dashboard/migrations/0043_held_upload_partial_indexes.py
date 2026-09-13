from django.db import migrations, models

#: (model, column, index). Built outside a transaction so a large pin table is not locked against writes. Django records
#: this migration only once every index is built, so an interrupted deploy runs all of them again: each is dropped first,
#: which also removes the invalid index a cancelled concurrent build leaves under its name.
_INDEXES = (
    ("pin", "custom_icon_upload", "idxdb_pin_held_icon"),
    ("label", "custom_icon_upload", "idxdb_label_held_icon"),
    ("achievement", "custom_icon_upload", "idxdb_achv_held_icon"),
    ("profile", "avatar_upload", "idxdb_profile_held_avatar"),
)


def _build(apps, schema_editor):
    quote = schema_editor.quote_name
    for model_name, column, name in _INDEXES:
        table = apps.get_model("dashboard", model_name)._meta.db_table
        schema_editor.execute(f"DROP INDEX CONCURRENTLY IF EXISTS {quote(name)}")
        schema_editor.execute(f"CREATE INDEX CONCURRENTLY {quote(name)} ON {quote(table)} ({quote(column)}) WHERE NOT ({quote(column)} = '')")


def _drop(apps, schema_editor):
    for _model_name, _column, name in _INDEXES:
        schema_editor.execute(f"DROP INDEX CONCURRENTLY IF EXISTS {schema_editor.quote_name(name)}")


class Migration(migrations.Migration):
    atomic = False

    dependencies = [
        ("dashboard", "0042_held_icon_and_avatar_uploads"),
    ]

    operations = [
        migrations.SeparateDatabaseAndState(
            state_operations=[
                migrations.AddIndex(
                    model_name=model_name,
                    index=models.Index(condition=models.Q((column, ""), _negated=True), fields=[column], name=name),
                )
                for model_name, column, name in _INDEXES
            ],
            database_operations=[migrations.RunPython(_build, _drop)],
        ),
    ]
