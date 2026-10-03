"""Drop the cached rows of the removed "Reference Documents" panel (P225); nothing reads them now."""

from django.db import migrations

NEARBY_REFERENCE_DOCUMENTS = "redata_reference_documents_nearby"


def drop_nearby_reference_documents(apps, schema_editor):
    LocationCache = apps.get_model("dashboard", "LocationCache")
    LocationCache.objects.filter(source=NEARBY_REFERENCE_DOCUMENTS).delete()


class Migration(migrations.Migration):
    dependencies = [
        ("dashboard", "0041_location_slug_remint"),
    ]

    operations = [
        migrations.RunPython(drop_nearby_reference_documents, migrations.RunPython.noop),
    ]
