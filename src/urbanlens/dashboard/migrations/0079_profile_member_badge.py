"""The opt-in badge is a member badge: it shows for anyone holding a role, not only for paying supporters."""

from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("dashboard", "0078_hand_billing_to_plugin"),
    ]

    operations = [
        migrations.RenameField(
            model_name="profile",
            old_name="show_supporter_badge",
            new_name="show_member_badge",
        ),
        migrations.AlterField(
            model_name="profile",
            name="show_member_badge",
            field=models.BooleanField(default=True, help_text="Show a small member badge next to your name when you hold a role."),
        ),
    ]
