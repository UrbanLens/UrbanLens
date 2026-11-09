"""
Backfill ``Pin.subject_place`` from ``Location.place``.

Deliberately not atomic: each chunk is its own set-based UPDATE over a primary-key range, so no lock is held across the whole table.
Only pins with no subject are touched, so re-running (or running alongside live traffic that already sets it) is safe.
"""

from django.db import migrations

CHUNK = 5000


def backfill(apps, schema_editor):
    Pin = apps.get_model("dashboard", "Pin")
    connection = schema_editor.connection
    table = Pin._meta.db_table  # noqa: SLF001
    location_table = apps.get_model("dashboard", "Location")._meta.db_table  # noqa: SLF001
    with connection.cursor() as cursor:
        cursor.execute(f"SELECT MIN(id), MAX(id) FROM {table}")  # noqa: S608 - table names come from model metadata
        low, high = cursor.fetchone()
        if low is None:
            return
        start = low
        while start <= high:
            cursor.execute(
                f"UPDATE {table} AS pin SET subject_place_id = location.place_id "  # noqa: S608
                f"FROM {location_table} AS location "
                "WHERE pin.location_id = location.id AND location.place_id IS NOT NULL "
                "AND pin.subject_place_id IS NULL AND pin.id >= %s AND pin.id < %s",
                [start, start + CHUNK],
            )
            start += CHUNK


class Migration(migrations.Migration):
    atomic = False

    dependencies = [
        ("dashboard", "0006_pin_subject_place"),
    ]

    operations = [
        migrations.RunPython(backfill, migrations.RunPython.noop),
    ]
