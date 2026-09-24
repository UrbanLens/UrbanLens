from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("dashboard", "0095_drop_overlays_without_stored_source")]

    operations = [
        migrations.RemoveField(model_name="mapimageoverlay", name="image_url"),
        migrations.AddConstraint(
            model_name="mapimageoverlay",
            constraint=models.CheckConstraint(
                condition=models.Q(models.Q(("image__isnull", False), ("tile_url_template", "")), models.Q(("image__isnull", True), models.Q(("tile_url_template", ""), _negated=True)), _connector="OR"),
                name="db_overlay_one_source",
            ),
        ),
    ]
