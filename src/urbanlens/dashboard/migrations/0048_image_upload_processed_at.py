from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("dashboard", "0047_image_copied_from_image_copied_from_label_and_more"),
    ]

    operations = [
        migrations.AddField(
            model_name="image",
            name="upload_processed_at",
            field=models.DateTimeField(blank=True, null=True),
        ),
    ]
