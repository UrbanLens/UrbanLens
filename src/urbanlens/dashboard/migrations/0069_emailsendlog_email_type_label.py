from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("dashboard", "0068_profile_verified_primary_email_unique")]

    operations = [
        migrations.AlterField(
            model_name="emailsendlog",
            name="email_type",
            field=models.CharField(
                choices=[
                    ("join_invite", "Friend invitation"),
                    ("visit_invite", "Visit participant invitation"),
                    ("email_verification", "Email verification"),
                    ("trip_invite", "Trip invitation"),
                ],
                max_length=20,
            ),
        ),
    ]
