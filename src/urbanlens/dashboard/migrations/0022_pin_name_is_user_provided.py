"""Track whether pin names were explicitly provided by users."""

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("dashboard", "0021_site_settings_signup_restricted"),
    ]

    operations = [
        migrations.AddField(
            model_name="pin",
            name="name_is_user_provided",
            field=models.BooleanField(
                default=False,
                help_text="Prevents external API name refreshes from overwriting a user-entered pin name.",
            ),
        ),
    ]
