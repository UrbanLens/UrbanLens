"""Album cover/order, map-hidden photos, deduped quota, and reviewable upload issues."""

from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):
    dependencies = [
        ("dashboard", "0032_image_thumbnail"),
    ]

    operations = [
        migrations.AddField(
            model_name="image",
            name="map_hidden",
            field=models.BooleanField(default=False),
        ),
        migrations.AlterField(
            model_name="image",
            name="quota_exempt_reason",
            field=models.CharField(
                blank=True,
                choices=[
                    ("external_media", "Cached external media"),
                    ("community", "Community-valued contribution"),
                    ("shared_copy", "Copy of a shared photo"),
                    ("deduplicated", "Same file already stored for this user"),
                ],
                default="",
                max_length=20,
            ),
        ),
        migrations.CreateModel(
            name="PhotoUploadFailure",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("created", models.DateTimeField(auto_now_add=True)),
                ("updated", models.DateTimeField(auto_now=True)),
                ("filename", models.CharField(max_length=255)),
                ("error", models.TextField()),
                (
                    "status",
                    models.CharField(
                        choices=[("pending", "Pending"), ("resolved", "Resolved"), ("dismissed", "Dismissed")],
                        default="pending",
                        max_length=20,
                    ),
                ),
                (
                    "album",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="photo_upload_failures",
                        to="dashboard.album",
                    ),
                ),
                (
                    "pin",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="photo_upload_failures",
                        to="dashboard.pin",
                    ),
                ),
                (
                    "profile",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="photo_upload_failures",
                        to="dashboard.profile",
                    ),
                ),
            ],
            options={
                "db_table": "dashboard_photo_upload_failure",
            },
        ),
        migrations.CreateModel(
            name="PhotoMetadataConflict",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("created", models.DateTimeField(auto_now_add=True)),
                ("updated", models.DateTimeField(auto_now=True)),
                ("fields", models.JSONField(default=dict)),
                (
                    "status",
                    models.CharField(
                        choices=[("pending", "Pending"), ("resolved", "Resolved"), ("dismissed", "Dismissed")],
                        default="pending",
                        max_length=20,
                    ),
                ),
                (
                    "existing_image",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="metadata_conflicts_as_existing",
                        to="dashboard.image",
                    ),
                ),
                (
                    "new_image",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="metadata_conflicts_as_new",
                        to="dashboard.image",
                    ),
                ),
                (
                    "profile",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="photo_metadata_conflicts",
                        to="dashboard.profile",
                    ),
                ),
            ],
            options={
                "db_table": "dashboard_photo_metadata_conflict",
            },
        ),
        migrations.AddIndex(
            model_name="photouploadfailure",
            index=models.Index(fields=["profile", "status"], name="idx_photo_fail_profile_status"),
        ),
        migrations.AddIndex(
            model_name="photometadataconflict",
            index=models.Index(fields=["profile", "status"], name="idx_photo_meta_profile_status"),
        ),
    ]
