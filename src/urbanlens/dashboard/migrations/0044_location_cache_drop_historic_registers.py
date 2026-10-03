"""Drop the cached Historic Registers rows, which predate keeping each row's reference number and position (P228, P230)."""

from django.db import migrations

HISTORIC_REGISTERS = "redata_historic_registers"


def drop_historic_registers(apps, schema_editor):
    LocationCache = apps.get_model("dashboard", "LocationCache")
    LocationCache.objects.filter(source=HISTORIC_REGISTERS).delete()


class Migration(migrations.Migration):
    dependencies = [
        ("dashboard", "0043_link_auto_source"),
    ]

    operations = [
        migrations.RunPython(drop_historic_registers, migrations.RunPython.noop),
    ]
