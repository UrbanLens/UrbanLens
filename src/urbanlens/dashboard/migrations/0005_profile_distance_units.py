# Generated for the Memories distance-units preference.

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("dashboard", "0004_rename_instance_uuid_sitesettings_uuid_and_more"),
    ]

    operations = [
        migrations.AddField(
            model_name="profile",
            name="distance_units",
            field=models.CharField(
                blank=True,
                choices=[("km", "Kilometers"), ("mi", "Miles")],
                help_text="Unit used for distances and travel stats. Defaults to your region.",
                max_length=4,
                null=True,
            ),
        ),
    ]
