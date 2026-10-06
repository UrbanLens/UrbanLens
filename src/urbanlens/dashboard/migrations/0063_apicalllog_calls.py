"""Let one ApiCallLog row stand for a minute of a tallied service's calls (services.core.call_tally)."""

from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("dashboard", "0062_markupmap_bearing"),
    ]

    operations = [
        migrations.AddField(
            model_name="apicalllog",
            name="calls",
            field=models.PositiveIntegerField(
                db_default=1,
                default=1,
                help_text="How many calls this row stands for: 1, except for a service whose calls are tallied and rolled up once a minute (services.core.call_tally), where it is every call that minute with the same outcome. Its response_ms is their mean and its cost_estimate their total.",
            ),
        ),
    ]
