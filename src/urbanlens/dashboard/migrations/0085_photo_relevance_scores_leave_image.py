"""
Drop the photo-relevance score columns from ``Image``.

The scores are a plugin's data about a photo, so they live in a table the plugin owns. Where that plugin is installed,
its first migration runs before this one and copies the columns it finds.
"""

from django.db import migrations


class Migration(migrations.Migration):
    dependencies = [
        ("dashboard", "0084_boundary_source_is_open_vocabulary"),
    ]

    operations = [
        migrations.RemoveField(model_name="image", name="redata_confidence"),
        migrations.RemoveField(model_name="image", name="redata_model_version"),
        migrations.RemoveField(model_name="image", name="redata_scored_at"),
        migrations.RemoveField(model_name="image", name="redata_scorer"),
    ]
