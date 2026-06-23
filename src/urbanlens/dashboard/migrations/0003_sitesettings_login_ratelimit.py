from __future__ import annotations

import django.core.validators
from django.db import migrations, models


class Migration(migrations.Migration):
    """Add login rate-limiting fields to SiteSettings.

    ``login_max_attempts`` controls how many consecutive failed logins are
    permitted before the account is locked.  ``login_lockout_minutes``
    controls how long the lockout lasts.  Both default to sensible values so
    existing deployments get reasonable protection without any admin action.
    """

    dependencies = [
        ("dashboard", "0002_pin_is_private"),
    ]

    operations = [
        migrations.AddField(
            model_name="sitesettings",
            name="login_max_attempts",
            field=models.IntegerField(
                default=5,
                help_text=(
                    "Maximum number of consecutive failed login attempts before an account is "
                    "temporarily locked. Set to 0 to disable rate limiting."
                ),
                verbose_name="Max failed login attempts",
                validators=[
                    django.core.validators.MinValueValidator(0),
                    django.core.validators.MaxValueValidator(100),
                ],
            ),
        ),
        migrations.AddField(
            model_name="sitesettings",
            name="login_lockout_minutes",
            field=models.IntegerField(
                default=15,
                help_text="How many minutes a locked account must wait before login attempts are accepted again.",
                verbose_name="Lockout duration (minutes)",
                validators=[
                    django.core.validators.MinValueValidator(1),
                    django.core.validators.MaxValueValidator(1440),
                ],
            ),
        ),
        migrations.AddConstraint(
            model_name="sitesettings",
            constraint=models.CheckConstraint(
                condition=models.Q(login_max_attempts__gte=0),
                name="login_max_attempts_gte_0",
            ),
        ),
        migrations.AddConstraint(
            model_name="sitesettings",
            constraint=models.CheckConstraint(
                condition=models.Q(login_lockout_minutes__gte=1),
                name="login_lockout_minutes_gte_1",
            ),
        ),
    ]
