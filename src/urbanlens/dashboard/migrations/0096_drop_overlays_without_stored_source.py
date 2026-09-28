from django.db import migrations


def drop_url_only_overlays(apps, schema_editor):
    # A url-only overlay can't be materialized here (no network in a migration), and the column it drew from is removed next.
    MapImageOverlay = apps.get_model("dashboard", "MapImageOverlay")
    MapImageOverlay.objects.filter(image__isnull=True, tile_url_template="").delete()
    MapImageOverlay.objects.filter(image__isnull=False).exclude(tile_url_template="").update(tile_url_template="")


class Migration(migrations.Migration):
    dependencies = [("dashboard", "0095_data_retention_settings")]

    operations = [migrations.RunPython(drop_url_only_overlays, migrations.RunPython.noop)]
