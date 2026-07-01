"""Add external official names to pins and locations."""

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("dashboard", "0023_pin_sharing"),
    ]

    operations = [
        migrations.AddField(
            model_name="location",
            name="official_name",
            field=models.CharField(blank=True, max_length=255, null=True),
        ),
        migrations.AddField(
            model_name="pin",
            name="official_name",
            field=models.CharField(blank=True, max_length=255, null=True),
        ),
        migrations.AddIndex(
            model_name="location",
            index=models.Index(fields=["official_name"], name="dashboard_l_offici_1cbd21_idx"),
        ),
    ]
