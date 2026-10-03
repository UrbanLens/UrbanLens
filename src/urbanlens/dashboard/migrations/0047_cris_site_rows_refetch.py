"""Drop CRIS rows cached under the old site choice, so each is fetched again (P234).

A site-scope row listed only the buildings inside one site record's boundary, at most 40, and that record could be a
neighbouring district. A row whose district excludes the point names a neighbour as the place's site.
"""

from django.db import migrations

CRIS_SOURCE = "cris_building_usn"


def drop_stale_cris_rows(apps, schema_editor):
    LocationCache = apps.get_model("dashboard", "LocationCache")
    rows = LocationCache.objects.filter(source=CRIS_SOURCE)
    rows.filter(data__site_scope=True).delete()
    rows.filter(data__district__contains_point=False).delete()


class Migration(migrations.Migration):
    dependencies = [
        ("dashboard", "0046_location_cache_drop_historic_registers"),
    ]

    operations = [
        migrations.RunPython(drop_stale_cris_rows, migrations.RunPython.noop),
    ]
