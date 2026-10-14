"""
Core records usage in units only, so the dollar estimate on each API call and the public-page switch that went with it are dropped.

A plugin that reports on spend carries the recorded estimates over before this runs.
"""

from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("dashboard", "0080_hand_cost_tracking_to_plugin"),
    ]

    operations = [
        migrations.RemoveField(
            model_name="apicalllog",
            name="cost_estimate",
        ),
        migrations.RemoveField(
            model_name="sitesettings",
            name="public_costs_page_enabled",
        ),
        migrations.AlterField(
            model_name="apicalllog",
            name="input_tokens",
            field=models.IntegerField(blank=True, help_text="Prompt tokens, as the provider reported them or else counted locally. Null for a call that is not an AI call, or one that returned nothing to count.", null=True),
        ),
        migrations.AlterField(
            model_name="apicalllog",
            name="output_tokens",
            field=models.IntegerField(blank=True, help_text="Completion tokens, as the provider reported them or else counted locally. Null for a call that is not an AI call, or one that returned nothing to count.", null=True),
        ),
        migrations.AlterField(
            model_name="apicalllog",
            name="calls",
            field=models.PositiveIntegerField(
                db_default=1,
                default=1,
                help_text="How many calls this row stands for: 1, except for a service whose calls are tallied and rolled up once a minute (services.core.call_tally), where it is every call that minute with the same outcome. Its response_ms is their mean.",
            ),
        ),
    ]
