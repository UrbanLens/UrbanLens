"""Add reddit field to Profile."""

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("dashboard", "0008_profile_privacy_settings"),
    ]

    operations = [
        migrations.AddField(
            model_name="profile",
            name="reddit",
            field=models.CharField(blank=True, max_length=100, null=True),
        ),
    ]
