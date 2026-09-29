from django.db import migrations


def refuse_to_drop_url_only_overlays(apps, schema_editor):
    # An overlay's pasted image is downloaded when it's submitted; one still drawing only from image_url must be downloaded, never deleted.
    MapImageOverlay = apps.get_model("dashboard", "MapImageOverlay")
    url_only = list(MapImageOverlay.objects.filter(image__isnull=True, tile_url_template="").values_list("pk", flat=True))
    if url_only:
        raise RuntimeError(
            f"{len(url_only)} map overlay(s) draw only from an external image_url, which the next migration removes: {url_only[:20]}. "
            "Download each image into the overlay (services.map.image_overlays.image_from_external_url) before migrating."
        )
    MapImageOverlay.objects.filter(image__isnull=False).exclude(tile_url_template="").update(tile_url_template="")


class Migration(migrations.Migration):
    dependencies = [("dashboard", "0095_data_retention_settings")]

    operations = [migrations.RunPython(refuse_to_drop_url_only_overlays, migrations.RunPython.noop)]
