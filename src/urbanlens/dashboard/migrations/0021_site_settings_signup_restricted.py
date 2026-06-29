"""Add signup_restricted field to SiteSettings for invite-only mode."""

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("dashboard", "0020_alter_apicalllog_id_alter_apiratelimit_calls_per_day_and_more"),
    ]

    operations = [
        migrations.AddField(
            model_name="sitesettings",
            name="signup_restricted",
            field=models.BooleanField(
                default=False,
                help_text=(
                    "When enabled, new accounts cannot be created via the public sign-up page. "
                    "Only users invited by an existing member can join."
                ),
                verbose_name="Restrict sign-ups (invite-only)",
            ),
        ),
    ]
