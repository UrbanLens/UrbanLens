from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("dashboard", "0110_sitesettings_capacity_limits")]

    operations = [
        migrations.RemoveIndex(model_name="image", name="idxdb_image_pending_created"),
        migrations.AddIndex(
            model_name="image",
            index=models.Index(
                condition=models.Q(("pending_scan", True), ("upload_failed_at__isnull", True)),
                fields=["created"],
                name="idxdb_image_pending_created",
            ),
        ),
    ]
