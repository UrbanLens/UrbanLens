from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("dashboard", "0067_release_stale_verified_primary_emails")]

    operations = [
        migrations.AddConstraint(
            model_name="profile",
            constraint=models.UniqueConstraint(
                condition=models.Q(("verified_primary_email", ""), _negated=True),
                fields=("verified_primary_email",),
                name="uniq_profile_verified_primary_email",
            ),
        ),
    ]
