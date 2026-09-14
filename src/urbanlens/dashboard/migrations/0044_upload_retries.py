from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("dashboard", "0043_held_upload_partial_indexes")]
    operations = [
        migrations.CreateModel(
            name="UploadRetry",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("created", models.DateTimeField(auto_now_add=True)),
                ("updated", models.DateTimeField(auto_now=True)),
                ("target", models.CharField(max_length=100)),
                ("object_id", models.PositiveBigIntegerField()),
                ("name", models.CharField(max_length=255)),
                ("attempts", models.PositiveIntegerField(default=0)),
                ("next_attempt_at", models.DateTimeField()),
                ("last_error", models.CharField(blank=True, default="", max_length=255)),
                ("gone_since", models.DateTimeField(blank=True, null=True)),
                ("admin_notified_at", models.DateTimeField(blank=True, null=True)),
            ],
            options={
                "db_table": "dashboard_upload_retries",
                "ordering": ["next_attempt_at"],
                "abstract": False,
            },
        ),
        migrations.AddField(
            model_name="sitesettings",
            name="notify_stuck_uploads_email",
            field=models.BooleanField(default=True, help_text="Email the admin notification address when an upload has kept failing for a day while storage accepts others.", verbose_name="Stuck uploads (email)"),
        ),
        migrations.AddField(
            model_name="sitesettings",
            name="notify_stuck_uploads_gotify",
            field=models.BooleanField(default=False, help_text="Send a Gotify push notification when an upload has kept failing for a day while storage accepts others.", verbose_name="Stuck uploads (Gotify)"),
        ),
        migrations.AddConstraint(
            model_name="uploadretry",
            constraint=models.UniqueConstraint(fields=("target", "object_id"), name="uq_uploadretry_target_object"),
        ),
        migrations.AddIndex(
            model_name="uploadretry",
            index=models.Index(fields=["next_attempt_at"], name="idxdb_uploadretry_next"),
        ),
    ]
