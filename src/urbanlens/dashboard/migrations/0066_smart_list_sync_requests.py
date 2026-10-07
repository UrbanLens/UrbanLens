"""Pins whose smart-list membership is owed a re-evaluation, drained off the request (N30, Batch 1)."""

from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):
    dependencies = [
        ("dashboard", "0065_image_keyword_retry"),
    ]

    operations = [
        migrations.CreateModel(
            name="SmartListSyncRequest",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("created", models.DateTimeField(auto_now_add=True)),
                ("updated", models.DateTimeField(auto_now=True)),
                ("pin", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="+", to="dashboard.pin")),
                ("profile", models.ForeignKey(db_index=False, on_delete=django.db.models.deletion.CASCADE, related_name="+", to="dashboard.profile")),
            ],
            options={
                "db_table": "dashboard_smart_list_sync_requests",
                "abstract": False,
                "indexes": [models.Index(fields=["profile", "id"], name="idx_smart_list_sync_profile")],
            },
        ),
    ]
