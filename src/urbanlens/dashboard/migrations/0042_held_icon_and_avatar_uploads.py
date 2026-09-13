from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("dashboard", "0041_image_embedded_keywords"),
    ]

    operations = [
        migrations.AddField(
            model_name="achievement",
            name="custom_icon_upload",
            field=models.CharField(blank=True, default="", max_length=255),
        ),
        migrations.AddField(
            model_name="label",
            name="custom_icon_upload",
            field=models.CharField(blank=True, default="", max_length=255),
        ),
        migrations.AddField(
            model_name="pin",
            name="custom_icon_upload",
            field=models.CharField(blank=True, default="", max_length=255),
        ),
        migrations.AddField(
            model_name="profile",
            name="avatar_upload",
            field=models.CharField(blank=True, default="", max_length=255),
        ),
    ]
