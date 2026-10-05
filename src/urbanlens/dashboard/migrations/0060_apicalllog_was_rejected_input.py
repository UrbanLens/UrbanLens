"""Flag the ApiCallLog rows written for a call refused because its input could not return data."""

from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("dashboard", "0059_provider_health"),
    ]

    operations = [
        migrations.AddField(
            model_name="apicalllog",
            name="was_rejected_input",
            field=models.BooleanField(db_default=False, default=False, help_text="True if this entry records a call refused before it was made because its input could not return data (services.core.input_validation)."),
        ),
    ]
