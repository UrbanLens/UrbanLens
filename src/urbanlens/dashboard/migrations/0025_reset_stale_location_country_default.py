"""Reset the stale "United States" default on Location.country.

Every Location row's ``country`` was set to the model's old hardcoded
``default="United States"`` and never overwritten by geocoding (the reverse
geocoding step only ever populated street/city/state/zip - see
``pin_edit._ensure_location_address``). That made non-US pins display
"United States" in the map sidebar. Blanking it here lets the sidebar hide
the field until it's correctly backfilled (going forward, new geocode calls
populate real country data; existing rows are backfilled by the
``backfill_location_country`` management command).
"""

from django.db import migrations


def reset_stale_country(apps, schema_editor):
    Location = apps.get_model("dashboard", "Location")
    Location.objects.filter(country="United States").update(country="")


class Migration(migrations.Migration):
    dependencies = [
        ("dashboard", "0024_alter_location_country_default"),
    ]

    operations = [
        migrations.RunPython(reset_stale_country, migrations.RunPython.noop, elidable=True),
    ]
