from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("dashboard", "0060_profileemail_promote_on_verify"),
    ]

    operations = [
        migrations.AddField(
            model_name="profile",
            name="username_key",
            field=models.TextField(blank=True, db_default="", default=""),
        ),
    ]
