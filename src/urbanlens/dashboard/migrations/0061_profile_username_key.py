from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("dashboard", "0059_friend_invitation_invitee_and_delivered_sends"),
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
