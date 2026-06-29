from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("dashboard", "0018_profile_places_sources"),
    ]

    operations = [
        migrations.CreateModel(
            name="ApiRateLimit",
            fields=[
                ("id", models.AutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("created", models.DateTimeField(auto_now_add=True)),
                ("updated", models.DateTimeField(auto_now=True)),
                ("service", models.CharField(
                    help_text="Internal service identifier (e.g. 'nps', 'google_places').",
                    max_length=50,
                    unique=True,
                )),
                ("display_name", models.CharField(
                    help_text="Human-readable service name shown in the admin UI.",
                    max_length=100,
                )),
                ("enabled", models.BooleanField(
                    default=True,
                    help_text="Master toggle. When disabled, all calls to this service are blocked.",
                )),
                ("calls_per_minute", models.IntegerField(
                    blank=True,
                    help_text="Maximum calls allowed per rolling minute. Leave blank for no per-minute limit.",
                    null=True,
                )),
                ("calls_per_day", models.IntegerField(
                    blank=True,
                    help_text="Maximum calls allowed per calendar day (UTC). Leave blank for no daily limit.",
                    null=True,
                )),
                ("usa_only", models.BooleanField(
                    default=False,
                    help_text=(
                        "Skip API calls for coordinates outside the United States. "
                        "Enable for USA-centric services (NPS, LoopNet, Library of Congress, etc.)."
                    ),
                )),
                ("notes", models.TextField(
                    blank=True,
                    help_text="Optional admin notes (e.g. free-tier limits, billing thresholds).",
                )),
            ],
            options={
                "verbose_name": "API Rate Limit",
                "verbose_name_plural": "API Rate Limits",
                "db_table": "dashboard_api_rate_limit",
                "ordering": ["display_name"],
                "abstract": False,
            },
        ),
        migrations.CreateModel(
            name="ApiCallLog",
            fields=[
                ("id", models.AutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("created", models.DateTimeField(auto_now_add=True)),
                ("updated", models.DateTimeField(auto_now=True)),
                ("service", models.CharField(
                    db_index=True,
                    help_text="Service identifier matching ApiRateLimit.service.",
                    max_length=50,
                )),
                ("endpoint", models.TextField(
                    blank=True,
                    help_text="URL or endpoint path called.",
                )),
                ("success", models.BooleanField(
                    default=True,
                    help_text="False if the call raised an exception or returned a non-2xx status.",
                )),
                ("response_ms", models.IntegerField(
                    blank=True,
                    help_text="Round-trip response time in milliseconds.",
                    null=True,
                )),
                ("was_rate_limited", models.BooleanField(
                    default=False,
                    help_text="True if this entry records a call that was blocked by rate limiting.",
                )),
                ("was_geo_filtered", models.BooleanField(
                    default=False,
                    help_text="True if this entry records a call that was skipped due to geography filtering.",
                )),
            ],
            options={
                "verbose_name": "API Call Log",
                "verbose_name_plural": "API Call Logs",
                "db_table": "dashboard_api_call_log",
                "ordering": ["-created"],
                "abstract": False,
            },
        ),
        migrations.AddIndex(
            model_name="apicalllog",
            index=models.Index(fields=["service", "created"], name="api_log_service_created_idx"),
        ),
    ]
