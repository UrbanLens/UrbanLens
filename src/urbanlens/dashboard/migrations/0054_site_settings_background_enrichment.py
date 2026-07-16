import django.core.validators
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("dashboard", "0053_profile_pin_detail_map_height"),
    ]

    operations = [
        migrations.AddField(
            model_name="sitesettings",
            name="enrichment_enabled",
            field=models.BooleanField(
                default=True,
                help_text="Proactively backfill official names, aliases, addresses, and boundaries for all pins and wikis in the background, within each API's configured rate limits.",
                verbose_name="Background enrichment",
            ),
        ),
        migrations.AddField(
            model_name="sitesettings",
            name="enrichment_start_hour",
            field=models.IntegerField(
                default=0,
                help_text="UTC hour (0-23) the daily enrichment window opens. Set start and end to the same value to allow enrichment at any hour.",
                validators=[
                    django.core.validators.MinValueValidator(0),
                    django.core.validators.MaxValueValidator(23),
                ],
                verbose_name="Enrichment window start (UTC hour)",
            ),
        ),
        migrations.AddField(
            model_name="sitesettings",
            name="enrichment_end_hour",
            field=models.IntegerField(
                default=0,
                help_text="UTC hour (0-23) the daily enrichment window closes. May be earlier than the start hour to wrap past midnight (e.g. 22 to 4).",
                validators=[
                    django.core.validators.MinValueValidator(0),
                    django.core.validators.MaxValueValidator(23),
                ],
                verbose_name="Enrichment window end (UTC hour)",
            ),
        ),
        migrations.AddField(
            model_name="sitesettings",
            name="enrichment_buffer_percent",
            field=models.IntegerField(
                default=10,
                help_text="Percentage of every API limit kept in reserve for organic traffic spikes. Background enrichment never spends into this buffer.",
                validators=[
                    django.core.validators.MinValueValidator(0),
                    django.core.validators.MaxValueValidator(90),
                ],
                verbose_name="Enrichment rate-limit buffer (%)",
            ),
        ),
        migrations.AddField(
            model_name="sitesettings",
            name="enrichment_max_per_service_per_run",
            field=models.IntegerField(
                default=10,
                help_text="Maximum locations enriched per API service in one hourly run, even when the service's rate limit would allow more - keeps bursts against generous APIs polite.",
                validators=[
                    django.core.validators.MinValueValidator(1),
                    django.core.validators.MaxValueValidator(500),
                ],
                verbose_name="Enrichment max items per service per run",
            ),
        ),
    ]
