from __future__ import annotations

from django.db import migrations, models


class Migration(migrations.Migration):
    """Add profile_setup_complete to Profile.

    Defaults to True so existing users are unaffected.  The social-auth
    pipeline sets it to False for brand-new SSO accounts so they are
    redirected to /profile/edit/ to choose their username and avatar.
    """

    dependencies = [("dashboard", "0001_initial")]

    operations = [
        migrations.AddField(
            model_name="profile",
            name="profile_setup_complete",
            field=models.BooleanField(default=True),
        ),
    ]
