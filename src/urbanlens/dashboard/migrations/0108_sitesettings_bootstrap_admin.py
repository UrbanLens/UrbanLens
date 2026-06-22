"""Add bootstrap admin tracking fields to SiteSettings."""

from __future__ import annotations

from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("dashboard", "0107_profile_remember_map_position"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.AddField(
            model_name="sitesettings",
            name="bootstrap_admin_onboarding_complete",
            field=models.BooleanField(
                default=False,
                help_text="True once the bootstrap admin has visited the site admin settings page.",
            ),
        ),
        migrations.AddField(
            model_name="sitesettings",
            name="bootstrap_admin_user",
            field=models.ForeignKey(
                blank=True,
                help_text="The first user created on this site; receives site admin and a one-time setup redirect.",
                null=True,
                on_delete=models.SET_NULL,
                related_name="+",
                to=settings.AUTH_USER_MODEL,
            ),
        ),
    ]
