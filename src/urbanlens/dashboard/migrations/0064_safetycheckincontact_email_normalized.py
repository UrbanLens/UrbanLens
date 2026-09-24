from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("dashboard", "0063_profile_username_key_index")]

    operations = [
        migrations.AddField(
            model_name="safetycheckincontact",
            name="email_normalized",
            field=models.CharField(blank=True, default="", max_length=254),
        ),
    ]
