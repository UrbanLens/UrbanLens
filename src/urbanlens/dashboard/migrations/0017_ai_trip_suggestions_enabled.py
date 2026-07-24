# Adds the site-wide "Trip suggestions" AI feature toggle (services.trip_ai_suggestions).

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("dashboard", "0016_boundary_voting"),
    ]

    operations = [
        migrations.AddField(
            model_name="sitesettings",
            name="ai_trip_suggestions_enabled",
            field=models.BooleanField(
                default=True,
                help_text=(
                    "Allow AI to suggest pins worth adding to a trip and a "
                    "drive/weather/vote-aware activity order. Only ever sees "
                    "pins every trip member already has and members' "
                    "external-sharing preferences are honored."
                ),
                verbose_name="Trip suggestions",
            ),
        ),
    ]
