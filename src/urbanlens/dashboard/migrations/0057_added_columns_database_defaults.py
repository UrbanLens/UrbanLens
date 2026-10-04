"""Give the columns added since v0.8.0 their database defaults where they were added without one (P291).

Their ``AddField`` operations now carry ``db_default``, so an install upgrading from v0.8.0 gets it as each column is
added. A database that applied them before that has the columns without a default, and this sets it. It is the same
statement either way.
"""

from django.db import migrations

_COLUMNS = (
    ("remoteimagecopy", "thumb_file"),
    ("locationcache", "audience"),
    ("locationcache", "relevance_rule"),
    ("location", "official_name_source"),
    ("gamesessionparticipant", "departure"),
    ("triviasessionparticipant", "departure"),
    ("pinlink", "auto_source"),
    ("wikilink", "auto_source"),
    ("mediarelevance", "is_vote"),
    ("pinowner", "care_of"),
    ("wikiowner", "care_of"),
    ("notificationlog", "fold_key"),
    ("notificationlog", "fold_count"),
)


def set_database_defaults(apps, schema_editor):
    for model_name, field_name in _COLUMNS:
        model = apps.get_model("dashboard", model_name)
        field = model._meta.get_field(field_name)
        default_sql, params = schema_editor.db_default_sql(field)
        table, column = schema_editor.quote_name(model._meta.db_table), schema_editor.quote_name(field.column)
        schema_editor.execute(f"ALTER TABLE {table} ALTER COLUMN {column} SET DEFAULT {default_sql}", params)


class Migration(migrations.Migration):
    dependencies = [
        ("dashboard", "0056_link_url_hash_indexes"),
    ]

    operations = [
        migrations.RunPython(set_database_defaults, migrations.RunPython.noop),
    ]
