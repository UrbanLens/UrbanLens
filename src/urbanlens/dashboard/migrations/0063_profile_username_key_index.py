from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("dashboard", "0062_backfill_username_key_and_gmail_alias"),
    ]

    operations = [
        migrations.AddIndex(
            model_name="profile",
            index=models.Index(fields=["username_key"], name="idxdb_profile_username_key"),
        ),
    ]
