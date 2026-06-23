from __future__ import annotations

from django.db import migrations, models


class Migration(migrations.Migration):
    """Add app_title to SiteSettings for instance branding."""

    dependencies = [
        ("dashboard", "0003_sitesettings_instance_uuid_pin_updated_idx"),
    ]

    operations = [
        migrations.AddField(
            model_name="sitesettings",
            name="app_title",
            field=models.CharField(default="UrbanLens", max_length=100, verbose_name="App title"),
        ),
    ]
