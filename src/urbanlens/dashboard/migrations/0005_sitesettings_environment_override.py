"""Add environment_override to SiteSettings."""

from __future__ import annotations

from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("dashboard", "0004_theme_contact"),
    ]

    operations = [
        migrations.AddField(
            model_name="sitesettings",
            name="environment_override",
            field=models.CharField(
                choices=[
                    ("default", "Default (from environment variable)"),
                    ("production", "Production"),
                    ("development", "Development"),
                    ("testing", "Testing"),
                ],
                default="default",
                help_text=(
                    "Override the deployment environment. "
                    "Default uses the UL_ENVIRONMENT variable (or local when unset)."
                ),
                max_length=20,
                verbose_name="Environment",
            ),
        ),
    ]
