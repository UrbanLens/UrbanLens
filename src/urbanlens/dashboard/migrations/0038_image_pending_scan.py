from django.db import migrations, models


class Migration(migrations.Migration):
    """Async malware scan for photo/video/document uploads, mirroring Comment.pending_scan."""

    dependencies = [
        ("dashboard", "0037_image_privacy_fields"),
    ]

    operations = [
        migrations.AddField(
            model_name="image",
            name="pending_scan",
            field=models.BooleanField(default=False),
        ),
    ]
