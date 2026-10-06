"""Record each keyword source that did not answer for a photo, and when to ask it again (P323)."""

from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):
    dependencies = [
        ("dashboard", "0064_rate_limit_rows_take_0_9_0_defaults"),
    ]

    operations = [
        migrations.CreateModel(
            name="ImageKeywordRetry",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("created", models.DateTimeField(auto_now_add=True)),
                ("updated", models.DateTimeField(auto_now=True)),
                ("source", models.CharField(max_length=50)),
                ("attempts", models.PositiveSmallIntegerField(db_default=0, default=0)),
                ("retry_at", models.DateTimeField(blank=True, null=True)),
                ("first_failed_at", models.DateTimeField(blank=True, null=True)),
                ("image", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="keyword_retries", to="dashboard.image")),
            ],
            options={
                "db_table": "dashboard_image_keyword_retries",
                "abstract": False,
                "indexes": [models.Index(condition=models.Q(("retry_at__isnull", False)), fields=["source", "retry_at"], name="idx_image_keyword_retry_due")],
                "constraints": [models.UniqueConstraint(fields=("image", "source"), name="uniq_image_keyword_retry")],
            },
        ),
    ]
