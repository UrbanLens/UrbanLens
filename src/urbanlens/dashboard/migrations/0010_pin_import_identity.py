"""Record the identities importers gave each pin, so a re-import never needs a proximity snap to stay idempotent. The backfill is the next migration, so these locks commit first."""

from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):
    dependencies = [("dashboard", "0009_backfill_pin_subject_place")]

    operations = [
        migrations.CreateModel(
            name="PinImportKey",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("created", models.DateTimeField(auto_now_add=True)),
                ("updated", models.DateTimeField(auto_now=True)),
                ("source", models.CharField(choices=[("google_takeout", "Google Takeout"), ("json_archive", "UrbanLens JSON archive")], max_length=20)),
                ("key", models.CharField(max_length=255)),
                ("pin", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="import_keys", to="dashboard.pin")),
                ("profile", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="+", to="dashboard.profile")),
            ],
            options={"db_table": "dashboard_pin_import_keys", "abstract": False},
        ),
        migrations.AddConstraint(
            model_name="pinimportkey",
            constraint=models.UniqueConstraint(fields=("profile", "source", "key"), name="db_pin_import_key_unique_per_profile"),
        ),
    ]
