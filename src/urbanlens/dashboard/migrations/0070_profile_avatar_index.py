from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("dashboard", "0069_emailsendlog_email_type_label")]

    operations = [
        migrations.AddIndex(
            model_name="profile",
            index=models.Index(
                condition=models.Q(("avatar__isnull", False), models.Q(("avatar", ""), _negated=True)),
                fields=["avatar"],
                name="idxdb_profile_avatar",
            ),
        ),
    ]
