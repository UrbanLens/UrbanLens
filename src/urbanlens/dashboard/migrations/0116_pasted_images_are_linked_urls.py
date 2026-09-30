"""An image downloaded from an address a user gave was stored as an upload (P178); it is a linked URL."""

from django.db import migrations


def mark_linked(apps, schema_editor):
    Image = apps.get_model("dashboard", "Image")
    Image.objects.filter(media_source_key="external_url", source="upload").update(source="linked_url")


class Migration(migrations.Migration):
    dependencies = [
        ("dashboard", "0115_remote_image_copies"),
    ]

    operations = [
        migrations.RunPython(mark_linked, migrations.RunPython.noop),
    ]
