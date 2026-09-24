from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("dashboard", "0059_friend_invitation_invitee_and_delivered_sends"),
    ]

    operations = [
        migrations.AddField(
            model_name="profileemail",
            name="promote_on_verify",
            field=models.BooleanField(default=False),
        ),
    ]
