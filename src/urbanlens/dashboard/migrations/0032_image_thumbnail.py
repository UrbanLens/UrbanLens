from django.db import migrations, models

import urbanlens.dashboard.models.images.model


class Migration(migrations.Migration):
    """Small grid thumbnails so album/gallery pages don't decode full-size files."""

    dependencies = [
        ("dashboard", "0031_v0_7_0_indexes"),
    ]

    operations = [
        migrations.AddField(
            model_name="image",
            name="thumbnail",
            field=models.ImageField(
                blank=True,
                max_length=255,
                null=True,
                upload_to=urbanlens.dashboard.models.images.model.pin_image_thumbnail_path,
            ),
        ),
    ]
