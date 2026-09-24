from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("dashboard", "0070_profile_avatar_index")]

    operations = [
        migrations.AddIndex(
            model_name="image",
            index=models.Index(fields=["profile", "quota_exempt_reason"], include=["file_size"], name="idxdb_image_quota_usage"),
        ),
        migrations.RemoveIndex(model_name="image", name="idxdb_image_profile_quota"),
    ]
