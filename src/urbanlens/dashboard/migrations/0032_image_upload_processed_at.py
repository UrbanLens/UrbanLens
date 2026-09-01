from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("dashboard", "0031_v0_7_0_indexes"),
    ]

    operations = [
        migrations.AddField(
            model_name="image",
            name="upload_processed_at",
            field=models.DateTimeField(blank=True, null=True),
        ),
    ]
