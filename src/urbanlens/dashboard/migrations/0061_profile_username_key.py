from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("dashboard", "0060_profileemail_promote_on_verify"),
    ]

    operations = [
        migrations.AddField(
            model_name="profile",
            name="username_key",
            field=models.CharField(blank=True, db_default="", default="", max_length=150),
        ),
        migrations.AddIndex(
            model_name="profile",
            index=models.Index(fields=["username_key"], name="idxdb_profile_username_key"),
        ),
    ]
