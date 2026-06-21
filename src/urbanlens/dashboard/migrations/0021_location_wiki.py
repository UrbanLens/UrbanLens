import django.contrib.gis.db.models.fields
from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):
    dependencies = [
        ("dashboard", "0020_pinvisit"),
    ]

    operations = [
        # ── bounding_box on Location ────────────────────────────────────────
        migrations.AddField(
            model_name="location",
            name="bounding_box",
            field=django.contrib.gis.db.models.fields.PolygonField(
                blank=True,
                geography=True,
                null=True,
                srid=4326,
            ),
        ),
        # ── LocationEdit table ──────────────────────────────────────────────
        migrations.CreateModel(
            name="LocationEdit",
            fields=[
                ("id", models.AutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("created", models.DateTimeField(auto_now_add=True)),
                ("updated", models.DateTimeField(auto_now=True)),
                ("changes", models.JSONField()),
                ("reverted", models.BooleanField(default=False)),
                ("editor", models.ForeignKey(
                    blank=True,
                    null=True,
                    on_delete=django.db.models.deletion.SET_NULL,
                    related_name="location_edits",
                    to="dashboard.profile",
                )),
                ("location", models.ForeignKey(
                    on_delete=django.db.models.deletion.CASCADE,
                    related_name="edits",
                    to="dashboard.location",
                )),
                ("reverted_by", models.ForeignKey(
                    blank=True,
                    null=True,
                    on_delete=django.db.models.deletion.SET_NULL,
                    related_name="reverts",
                    to="dashboard.locationedit",
                )),
            ],
            options={
                "db_table": "dashboard_location_edits",
                "ordering": ["-created"],
                "get_latest_by": "created",
                "abstract": False,
            },
        ),
        migrations.AddIndex(
            model_name="locationedit",
            index=models.Index(fields=["location"], name="dashboard_le_location_idx"),
        ),
        migrations.AddIndex(
            model_name="locationedit",
            index=models.Index(fields=["location", "created"], name="dashboard_le_location_created_idx"),
        ),
    ]
